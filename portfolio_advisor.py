"""Portfolio advisor - read-only review of the moomoo book vs RICH signals.

Run manually (no scheduler) after a `--review` or after the Daily pipeline
completes:

    python portfolio_advisor.py

READ-ONLY contract
------------------
* Places NO orders.
* The ONLY write to cache.db is appending rows to a brand-new table,
  `portfolio_advice` (created IF NOT EXISTS). No existing table, the
  dual-ranking fields, or the valuation overlay schema are touched.

Spec -> actual schema mapping (the spec used logical names; these are the
real cache.db columns, which are the source of truth):

    spec name            actual column (table)
    -------------------  -------------------------------------------
    cash_balance         cash                 (moomoo_account_snapshots)
    total_assets         total_assets         (moomoo_account_snapshots)
    avg_cost             cost_price           (moomoo_position_snapshots)
    unrealized_pnl%      pl_ratio             (moomoo_position_snapshots)  [already a %]
    latest_rich_score    total_score          (daily_picks)
    latest_rich_rank     recommendation_rank  (daily_picks)
    pick_price           pick_price           (daily_picks)

Resolved ambiguities (surfaced at runtime, not silently averaged)
-----------------------------------------------------------------
1. RANK: RICH is dual-ranked (`recommendation_rank` vs
   `recommendation_rank_with_valuation`). The spec says plain "rank", so we
   use `recommendation_rank` (the pre-valuation base rank) throughout.
2. MODULE 3 PICK: the spec keys sizing off "rank=1", but only the
   `is_daily_pick=1` row carries a populated `pick_price` (the base rank-1
   row's pick_price is NULL). Sizing is therefore keyed off `is_daily_pick=1`
   (the pick the system actually acts on); we fall back to
   `recommendation_rank=1` only if no daily pick exists, and warn that it has
   no price.
3. "TODAY": defined as MAX(run_date) present in daily_picks. If that is not
   the real MYT date (e.g. the Daily run has not finished), we run on the
   latest-available data and FLAG the staleness loudly.
4. CAPITAL: per spec, total = sum(latest position market_val) + cash. The
   account snapshot is a single REAL/US account; non-US positions (e.g. the
   MY-listed 6742) are NOT in the account row, so positions can sum above
   account.total_assets. We compute per spec and surface the reconciliation
   gap rather than hide it.
"""
from __future__ import annotations

import csv
import logging
import sqlite3
import sys
from datetime import datetime, timedelta
from math import floor
from pathlib import Path
from zoneinfo import ZoneInfo

from config import CACHE_DB, LOG_DIR, ROOT, TIMEZONE
from data.storage import connect

log = logging.getLogger("portfolio_advisor")

# --- tunable policy constants (kept explicit, per the spec) ---
HARD_EXIT_SCORE = 40.0     # latest total_score below this -> HARD_EXIT
DEGRADED_RANK = 15         # recommendation_rank above this -> DEGRADED
GONE_LOOKBACK_DAYS = 30    # absent from daily_picks this many days -> GONE_30D
OVERWEIGHT_FRAC = 0.30     # position > this fraction of book -> OVERWEIGHT
MAX_POSITION_FRAC = 0.25   # HARD CAP: no single position > this fraction of total portfolio

# --- Module 3 sizing: Taleb Barbell (Phase 1) + Half-Kelly (Phase 2) ---
FULL_CONVICTION_FRAC = 0.20   # Phase 1: FULL -> deploy up to 20% of investable cash
HALF_CONVICTION_FRAC = 0.10   # Phase 1: HALF -> deploy up to 10% of investable cash
PHASE2_MIN_SCORED = 50        # >= this many scored eval outcomes unlocks Half-Kelly
KELLY_DISCOUNT = 0.5          # Half-Kelly: multiply full-Kelly f by this

# The advisor lives in RICH but reads the shadow layer (read-only) for the
# daily verdict and the eval log. Derived from RICH's ROOT, not hardcoded.
SHADOW_ROOT = ROOT.parent / "RICH-shadow"
SHADOW_SIGNAL_MD = SHADOW_ROOT / "handoff" / "signal.md"
SHADOW_EVAL_CSV = SHADOW_ROOT / "eval" / "signal_vs_pick.csv"

PORTFOLIO_ADVICE_SCHEMA = """
CREATE TABLE IF NOT EXISTS portfolio_advice (
    run_date         TEXT NOT NULL,
    symbol           TEXT NOT NULL,
    action           TEXT NOT NULL,
    reason           TEXT,
    suggested_usd    REAL,
    suggested_shares REAL,
    PRIMARY KEY (run_date, symbol, action)
)
"""


def _setup_logging(today: str) -> None:
    """Dedicated portfolio log (logs/<YYYY-MM-DD>-portfolio.log) + stdout.

    Uses a named logger with its own handlers so it does not hijack the root
    logger that the Daily pipeline configures.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log.setLevel(logging.INFO)
    log.propagate = False
    log.handlers.clear()
    fmt = logging.Formatter("%(asctime)s %(levelname)-8s %(message)s")
    fh = logging.FileHandler(LOG_DIR / f"{today}-portfolio.log", encoding="utf-8")
    fh.setFormatter(fmt)
    sh = logging.StreamHandler(sys.stdout)
    sh.setFormatter(fmt)
    log.addHandler(fh)
    log.addHandler(sh)


def _is_us(row: sqlite3.Row) -> bool:
    """US-listed if moomoo tags trd_market='US' (non-US e.g. 6742/MY skip scoring)."""
    return (row["trd_market"] or "").upper() == "US"


def _latest_account(conn: sqlite3.Connection) -> sqlite3.Row:
    row = conn.execute(
        "SELECT * FROM moomoo_account_snapshots ORDER BY snapshot_ts DESC LIMIT 1"
    ).fetchone()
    if row is None:
        raise SystemExit("FAIL: moomoo_account_snapshots is empty - no account to advise on.")
    return row


def _latest_positions(conn: sqlite3.Connection) -> list[sqlite3.Row]:
    latest_date = conn.execute(
        "SELECT MAX(snapshot_date) FROM moomoo_position_snapshots"
    ).fetchone()[0]
    if latest_date is None:
        raise SystemExit("FAIL: moomoo_position_snapshots is empty - no positions to advise on.")
    rows = conn.execute(
        "SELECT * FROM moomoo_position_snapshots WHERE snapshot_date = ? "
        "ORDER BY market_val DESC",
        (latest_date,),
    ).fetchall()
    return rows


def _latest_pick_row(conn: sqlite3.Connection, symbol: str) -> sqlite3.Row | None:
    """Most recent daily_picks row for a symbol (latest run_date)."""
    return conn.execute(
        "SELECT run_date, total_score, recommendation_rank, "
        "recommendation_rank_with_valuation, beneish_flag, f_score "
        "FROM daily_picks WHERE symbol = ? ORDER BY run_date DESC LIMIT 1",
        (symbol,),
    ).fetchone()


def _seen_within(conn: sqlite3.Connection, symbol: str, cutoff: str) -> bool:
    return conn.execute(
        "SELECT 1 FROM daily_picks WHERE symbol = ? AND run_date >= ? LIMIT 1",
        (symbol, cutoff),
    ).fetchone() is not None


def _todays_top_pick(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The pick to size off: the latest run's is_daily_pick=1 row.

    Only the is_daily_pick row carries a populated pick_price, so there is no
    sensible rank-1 fallback (rank-1 rows have pick_price=NULL). Returns None
    on a no-pick day (cooldown / no eligible pick), which Module 3 reports
    cleanly rather than fabricating a buy.
    """
    latest_run = conn.execute("SELECT MAX(run_date) FROM daily_picks").fetchone()[0]
    if latest_run is None:
        return None
    return conn.execute(
        "SELECT * FROM daily_picks WHERE run_date = ? AND is_daily_pick = 1 LIMIT 1",
        (latest_run,),
    ).fetchone()


# --------------------------------------------------------------------------
# MODULE 1 - Capital Snapshot
# --------------------------------------------------------------------------
def module1_capital(conn, acct, positions, today):
    log.info("=" * 64)
    log.info("MODULE 1 - CAPITAL SNAPSHOT  (advice run_date %s MYT)", today)
    log.info("=" * 64)

    cash = acct["cash"] or 0.0
    invested = sum((p["market_val"] or 0.0) for p in positions)
    total = invested + cash

    log.info("Account snapshot      : %s  (%s/%s/%s)",
             acct["snapshot_ts"], acct["trd_env"], acct["trd_market"], acct["currency"])
    log.info(f"Total portfolio value : ${total:,.2f}   (sum positions + cash)")
    log.info(f"Cash available        : ${cash:,.2f}")
    log.info(f"Invested              : ${invested:,.2f}")

    # Reconciliation against the account's own total_assets (surface gaps).
    acct_total = acct["total_assets"]
    if acct_total is not None and abs(acct_total - total) > 0.01 * max(acct_total, 1):
        log.warning(
            f"RECONCILE: account.total_assets=${acct_total:,.2f} vs computed ${total:,.2f} "
            f"(>1% gap - non-US positions like 6742 sit outside the US account row).")

    log.info("-" * 64)
    log.info("%-8s %6s %12s %9s %8s %6s  %s",
             "symbol", "qty", "market_val", "u_pnl%", "score", "rank", "signal")
    per_position = {}
    for p in positions:
        sym = p["symbol"]
        mv = p["market_val"] or 0.0
        if _is_us(p):
            pick = _latest_pick_row(conn, sym)
            score = pick["total_score"] if pick else None
            rank = pick["recommendation_rank"] if pick else None
            sig = "OK"
        else:
            score = rank = None
            sig = "MANUAL_REVIEW (non-US)"
        per_position[sym] = {"row": p, "score": score, "rank": rank}
        log.info("%-8s %6.0f %12.2f %9.2f %8s %6s  %s",
                 sym, p["qty"] or 0.0, mv, p["pl_ratio"] or 0.0,
                 f"{score:.1f}" if score is not None else "-",
                 str(rank) if rank is not None else "-", sig)
    return {"cash": cash, "invested": invested, "total": total, "positions": per_position}


# --------------------------------------------------------------------------
# MODULE 2 - Exit Signals
# --------------------------------------------------------------------------
def module2_exits(conn, cap, today):
    log.info("")
    log.info("=" * 64)
    log.info("MODULE 2 - EXIT SIGNALS")
    log.info("=" * 64)

    cutoff = (datetime.strptime(today, "%Y-%m-%d") - timedelta(days=GONE_LOOKBACK_DAYS)).strftime("%Y-%m-%d")
    total = cap["total"]
    advice = []  # (symbol, action, reason)

    for sym, info in cap["positions"].items():
        p = info["row"]
        mv = p["market_val"] or 0.0

        if not _is_us(p):
            reason = f"non-US listing ({p['code']}, market={p['trd_market']}); RICH does not score it"
            log.info("%-8s MANUAL_REVIEW - %s", sym, reason)
            advice.append((sym, "MANUAL_REVIEW", reason))
            continue

        pick = _latest_pick_row(conn, sym)
        signals = []

        # HARD_EXIT: score below floor OR a Beneish manipulation flag.
        if pick is not None and pick["total_score"] is not None and pick["total_score"] < HARD_EXIT_SCORE:
            signals.append(("HARD_EXIT",
                            f"latest RICH score {pick['total_score']:.1f} < {HARD_EXIT_SCORE:.0f} "
                            f"(run {pick['run_date']})"))
        if pick is not None and pick["beneish_flag"] == 1:
            signals.append(("HARD_EXIT",
                            f"Beneish manipulation flag set (run {pick['run_date']})"))

        # DEGRADED: ranked but slipped past the cutoff.
        if pick is not None and pick["recommendation_rank"] is not None \
                and pick["recommendation_rank"] > DEGRADED_RANK:
            signals.append(("DEGRADED",
                            f"recommendation_rank {pick['recommendation_rank']} > {DEGRADED_RANK} "
                            f"(run {pick['run_date']})"))

        # GONE_30D: not seen in any pick run within the lookback window.
        if not _seen_within(conn, sym, cutoff):
            last = pick["run_date"] if pick else "never"
            signals.append(("GONE_30D",
                            f"absent from daily_picks since {cutoff} (last seen: {last})"))

        # OVERWEIGHT: concentration breach.
        frac = (mv / total) if total else 0.0
        if frac > OVERWEIGHT_FRAC:
            signals.append(("OVERWEIGHT",
                            f"{frac*100:.1f}% of book > {OVERWEIGHT_FRAC*100:.0f}% cap "
                            f"(${mv:,.0f})"))

        if not signals:
            log.info("%-8s HOLD - no exit signal "
                     "(score %s, rank %s)", sym,
                     f"{pick['total_score']:.1f}" if pick and pick['total_score'] is not None else "-",
                     str(pick['recommendation_rank']) if pick and pick['recommendation_rank'] is not None else "-")
            continue
        for action, reason in signals:
            log.info("%-8s %-13s - %s", sym, action, reason)
            advice.append((sym, action, reason))

    if not advice:
        log.info("No exit signals across the book.")
    return advice


# --------------------------------------------------------------------------
# MODULE 3 helpers — Shadow verdict + eval-log phase detection + Half-Kelly
# --------------------------------------------------------------------------
_OUTCOME_COLS = ("outcome", "result", "return_pct", "return", "pnl", "alpha")


def _shadow_verdict() -> tuple[str | None, str]:
    """Read today's Shadow verdict (FULL/HALF/SKIP) from RICH-shadow/handoff/signal.md.

    Returns (verdict_upper_or_None, reason). Missing/unreadable -> (None, why),
    which the caller treats as SKIP per spec.
    """
    try:
        text = SHADOW_SIGNAL_MD.read_text(encoding="utf-8")
    except OSError as e:
        return None, f"signal.md unreadable ({e})"
    fm = {}
    if text.startswith("---"):
        block = text.split("---", 2)[1]
        for line in block.splitlines():
            if ":" in line:
                k, _, v = line.partition(":")
                fm[k.strip()] = v.split("#", 1)[0].strip()
    action = (fm.get("action") or "").upper() or None
    # Reason: the first bullet under "## Verdict", else frontmatter summary.
    reason = ""
    lines = text.splitlines()
    for i, ln in enumerate(lines):
        if ln.strip().lower().startswith("## verdict"):
            for nxt in lines[i + 1:]:
                if nxt.strip().startswith("-"):
                    reason = nxt.strip().lstrip("- ").replace("**", "")
                    break
            break
    if not reason:
        reason = f"action={action}, rich_pick={fm.get('rich_pick')}, date={fm.get('date')}"
    return action, reason


def _eval_scored(conn: sqlite3.Connection) -> tuple[int, int, list[float]]:
    """Return (total_verdict_rows, scored_outcomes, scored_returns) from the eval log.

    Prefers a cache.db `shadow_eval_log` table if it ever exists; otherwise
    falls back to RICH-shadow/eval/signal_vs_pick.csv. The current CSV has no
    outcome column, so scored_outcomes is 0 -> Phase 1 stays active until
    outcome tracking is added.
    """
    has_table = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='shadow_eval_log'"
    ).fetchone() is not None
    if has_table:
        cols = [r[1] for r in conn.execute("PRAGMA table_info(shadow_eval_log)")]
        total = conn.execute("SELECT COUNT(*) FROM shadow_eval_log").fetchone()[0]
        oc = next((c for c in cols if c.lower() in _OUTCOME_COLS), None)
        if oc is None:
            return total, 0, []
        rows = conn.execute(f"SELECT {oc} FROM shadow_eval_log WHERE {oc} IS NOT NULL").fetchall()
        rets = [_to_float(r[0]) for r in rows]
        rets = [x for x in rets if x is not None]
        return total, len(rows), rets

    if not SHADOW_EVAL_CSV.exists():
        return 0, 0, []
    with SHADOW_EVAL_CSV.open(encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        fields = reader.fieldnames or []
        oc = next((c for c in fields if c.lower() in _OUTCOME_COLS), None)
        total = scored = 0
        rets: list[float] = []
        for row in reader:
            total += 1
            if oc and (row.get(oc) or "").strip():
                scored += 1
                v = _to_float(row[oc])
                if v is not None:
                    rets.append(v)
    return total, scored, rets


def _to_float(v) -> float | None:
    try:
        return float(str(v).replace("%", "").strip())
    except (TypeError, ValueError):
        return None


def _half_kelly(returns: list[float]) -> float:
    """Half-Kelly fraction from realized returns. 0.0 if it can't be estimated."""
    wins = [r for r in returns if r > 0]
    losses = [abs(r) for r in returns if r < 0]
    if not wins or not losses:
        return 0.0
    win_rate = len(wins) / len(returns)
    loss_rate = 1.0 - win_rate
    avg_payoff = (sum(wins) / len(wins)) / (sum(losses) / len(losses))
    if avg_payoff <= 0:
        return 0.0
    f = (win_rate * avg_payoff - loss_rate) / avg_payoff   # full Kelly
    return max(0.0, f * KELLY_DISCOUNT)


# --------------------------------------------------------------------------
# MODULE 3 - Position Sizing (Taleb Barbell Phase 1 / Half-Kelly Phase 2)
# --------------------------------------------------------------------------
def module3_sizing(conn, cap):
    log.info("")
    log.info("=" * 64)
    log.info("MODULE 3 - POSITION SIZING")
    log.info("=" * 64)
    log.info("This is a recommendation only. No orders are placed.")

    # --- Phase detection (eval-log maturity) ---
    total_rows, scored, rets = _eval_scored(conn)
    phase2 = scored >= PHASE2_MIN_SCORED
    if phase2:
        kelly = _half_kelly(rets)
        log.info("PHASE 2 - Half-Kelly sizing active (%d verdicts scored)", scored)
        log.info("PHASE 2 SIZING UNLOCKED")
        log.info("Half-Kelly fraction f = %.3f (of investable cash)", kelly)
        # Phase 2 scaling: FULL = Kelly*f (full conviction),
        # HALF = Kelly*f * 0.5 (regime uncertainty discount).
        # Revisit when eval log reaches 50 scored verdicts.
        full_frac, half_frac = kelly, kelly * 0.5
    else:
        log.info("PHASE 1 - Barbell sizing active (%d/%d scored verdicts; %d verdict rows logged)",
                 scored, PHASE2_MIN_SCORED, total_rows)
        full_frac, half_frac = FULL_CONVICTION_FRAC, HALF_CONVICTION_FRAC

    # --- Today's Shadow verdict -> conviction fraction of investable cash ---
    verdict, reason = _shadow_verdict()
    if verdict == "FULL":
        frac, label = full_frac, "FULL conviction"
    elif verdict == "HALF":
        frac, label = half_frac, "HALF conviction - reduced size"
    elif verdict == "SKIP":
        log.info("Shadow verdict: SKIP - no position today.")
        log.info("Reason: %s", reason)
        return None
    else:  # missing / unreadable -> treat as SKIP (constraint #1)
        log.warning("Shadow verdict unavailable - no sizing output (%s).", reason)
        return None

    investable_capital = cap["cash"]
    total_pv = cap["total"]
    max_single = investable_capital * frac
    log.info("Shadow verdict: %s  (%s)", verdict, label)
    log.info(f"investable_capital (cash) = ${investable_capital:,.0f}; "
             f"max_single ({frac*100:.1f}%) = ${max_single:,.0f}")

    # --- The pick to size (is_daily_pick row carries the price) ---
    pick = _todays_top_pick(conn)
    if pick is None or pick["pick_price"] is None or pick["pick_price"] <= 0:
        run = pick["run_date"] if pick else "n/a"
        log.warning("Shadow=%s but no priced RICH pick (is_daily_pick) for %s - cannot size.",
                    verdict, run)
        return (pick["symbol"] if pick else "NONE", "BUY",
                f"Shadow {verdict} but no priced daily pick ({run})", 0.0, 0.0)

    sym = pick["symbol"]
    pick_price = pick["pick_price"]
    held = cap["positions"].get(sym)
    current_holding = (held["row"]["market_val"] or 0.0) if held else 0.0

    # Hard 25%-of-total-portfolio cap, then take the binding constraint.
    room = (total_pv * MAX_POSITION_FRAC) - current_holding
    buy_amount = min(max_single, room)
    buy_shares = floor(buy_amount / pick_price) if buy_amount > 0 else 0

    if buy_shares == 0:
        if room <= 0:
            reason = "Position already at cap"
        elif max_single <= 0:
            reason = "Shadow SKIP"
        else:
            reason = "Insufficient cash"
        log.info("BUY %s: 0 shares - %s", sym, reason)
        return (sym, "BUY", reason, 0.0, 0.0)

    buy_usd = buy_shares * pick_price
    log.info(f"BUY {sym}: {buy_shares} shares @ ${pick_price:.2f} = ${buy_usd:.0f}")
    log.info(f"Conviction: {label}")
    log.info(f"Deploying {buy_amount / investable_capital * 100:.1f}% of available cash")
    log.info(f"Cash remaining after trade: ${investable_capital - buy_amount:.0f}")
    return (sym, "BUY", f"{verdict} ({label}); deploy {buy_amount/investable_capital*100:.1f}% cash",
            buy_usd, float(buy_shares))


# --------------------------------------------------------------------------
def _persist(conn, today, exit_advice, buy_advice):
    conn.execute(PORTFOLIO_ADVICE_SCHEMA)
    rows = []
    for sym, action, reason in exit_advice:
        rows.append((today, sym, action, reason, None, None))
    if buy_advice is not None:
        sym, action, reason, usd, shares = buy_advice
        rows.append((today, sym, action, reason, usd, shares))
    # Idempotent re-run: clear today's rows first so the table reflects only
    # the latest state (no stale rows from a prior same-day run).
    conn.execute("DELETE FROM portfolio_advice WHERE run_date = ?", (today,))
    conn.executemany(
        "INSERT OR REPLACE INTO portfolio_advice "
        "(run_date, symbol, action, reason, suggested_usd, suggested_shares) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        rows,
    )
    log.info("")
    log.info("Persisted %d advice row(s) to portfolio_advice (run_date %s).", len(rows), today)
    return len(rows)


def main() -> int:
    today = datetime.now(ZoneInfo(TIMEZONE)).strftime("%Y-%m-%d")
    _setup_logging(today)
    log.info("RICH portfolio_advisor - READ-ONLY (no orders; appends to portfolio_advice only)")

    try:
        with connect(CACHE_DB) as conn:
            # Tolerate the concurrent Daily pipeline holding a write lock.
            conn.execute("PRAGMA busy_timeout = 30000")

            acct = _latest_account(conn)
            positions = _latest_positions(conn)

            # Staleness flags (fail loud, do not block).
            pos_date = positions[0]["snapshot_date"]
            latest_run = conn.execute("SELECT MAX(run_date) FROM daily_picks").fetchone()[0]
            if pos_date != today:
                log.warning("STALE: latest position snapshot is %s, not today (%s).", pos_date, today)
            if acct["snapshot_date"] != today:
                log.warning("STALE: latest account snapshot is %s, not today (%s).", acct["snapshot_date"], today)
            if latest_run != today:
                log.warning("STALE: latest daily_picks run_date is %s, not today (%s) - "
                            "Daily pipeline may not have finished; sizing uses the latest available run.",
                            latest_run, today)

            cap = module1_capital(conn, acct, positions, today)
            exit_advice = module2_exits(conn, cap, today)
            buy_advice = module3_sizing(conn, cap)
            n = _persist(conn, today, exit_advice, buy_advice)

        log.info("Done. %d recommendation(s). Log: logs/%s-portfolio.log", n, today)
        return 0
    except sqlite3.OperationalError as e:
        log.error("DB error (is the Daily pipeline mid-write? busy_timeout exceeded): %s", e)
        return 1


if __name__ == "__main__":
    sys.exit(main())
