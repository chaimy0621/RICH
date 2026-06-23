#!/usr/bin/env python3
"""
sync_moomoo.py - RICH Phase 1: read-only Moomoo portfolio sync.

Standalone, additive, zero-coupling utility. Reads account assets and open
positions from a running Moomoo OpenD gateway and snapshots them into
data/cache.db (two NEW tables), then joins the latest position snapshot to the
latest RICH daily_picks rows by symbol.

READ-ONLY BY DESIGN: this script never places, modifies, or cancels an order.
It only *queries* Moomoo (accinfo / positions) and writes its own snapshot
tables. It imports no RICH module and touches no existing RICH table.
No changes to pipeline.py, scoring.py, screener.py, run_daily.py, or the DB
schema owned by data/storage.py.

Usage:
    python sync_moomoo.py             # sync snapshots to DB, then print join report
    python sync_moomoo.py --dry-run   # fetch + preview only; NO DB writes
    python sync_moomoo.py --report    # skip Moomoo; print join report from existing DB

Requires: moomoo-api (OpenD running on 127.0.0.1:11111) and stdlib sqlite3.
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import sqlite3
import time
from datetime import datetime

from moomoo import OpenSecTradeContext, TrdMarket, TrdEnv, SecurityFirm, RET_OK

# --- Connection / account config -------------------------------------------
# Moomoo OpenD gateway (confirmed reachable on this host).
OPEND_HOST = "127.0.0.1"
OPEND_PORT = 11111

# Adjust to match YOUR Moomoo entity and the market you trade:
#   SECURITY_FIRM : FUTUMY (Malaysia), FUTUINC (US), FUTUSG (Singapore),
#                   FUTUSECURITIES (Hong Kong), FUTUAU, FUTUJP, FUTUCA ...
#   TRD_MARKET    : US (RICH is US-equities), HK, MY ...
#   TRD_ENV       : REAL (live account) or SIMULATE (paper)
SECURITY_FIRM = SecurityFirm.FUTUMY
TRD_MARKET = TrdMarket.US
TRD_ENV = TrdEnv.REAL
QUERY_CURRENCY = "USD"   # accinfo valuation currency; positions valued the same

# --- DB ---------------------------------------------------------------------
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "cache.db")

SCHEMA_ACCOUNT = """
CREATE TABLE IF NOT EXISTS moomoo_account_snapshots (
    snapshot_ts          TEXT NOT NULL,   -- ISO-8601; identical for all rows of one sync run
    snapshot_date        TEXT NOT NULL,   -- YYYY-MM-DD convenience filter
    acc_id               TEXT NOT NULL,
    trd_env              TEXT,            -- REAL / SIMULATE
    trd_market           TEXT,            -- US / MY / ...
    currency             TEXT,
    total_assets         REAL,
    cash                 REAL,
    market_val           REAL,
    frozen_cash          REAL,
    avl_withdrawal_cash  REAL,
    power                REAL,
    unrealized_pl        REAL,
    realized_pl          REAL,
    raw_json             TEXT,            -- full moomoo row - audit / forward-compat backstop
    PRIMARY KEY (snapshot_ts, acc_id, currency)
);
"""

SCHEMA_POSITION = """
CREATE TABLE IF NOT EXISTS moomoo_position_snapshots (
    snapshot_ts    TEXT NOT NULL,
    snapshot_date  TEXT NOT NULL,
    acc_id         TEXT NOT NULL,
    trd_env        TEXT,
    trd_market     TEXT,
    code           TEXT NOT NULL,   -- raw moomoo code, e.g. 'US.AAPL'
    symbol         TEXT,            -- normalized bare ticker, e.g. 'AAPL' -> joins daily_picks.symbol
    stock_name     TEXT,
    qty            REAL,
    can_sell_qty   REAL,
    cost_price     REAL,
    nominal_price  REAL,            -- current/last price
    market_val     REAL,
    pl_val         REAL,            -- unrealized P/L value
    pl_ratio       REAL,            -- unrealized P/L ratio (%)
    currency       TEXT,
    raw_json       TEXT,
    PRIMARY KEY (snapshot_ts, acc_id, code)
);
"""

ACCOUNT_COLS = [
    "snapshot_ts", "snapshot_date", "acc_id", "trd_env", "trd_market", "currency",
    "total_assets", "cash", "market_val", "frozen_cash", "avl_withdrawal_cash",
    "power", "unrealized_pl", "realized_pl", "raw_json",
]
POSITION_COLS = [
    "snapshot_ts", "snapshot_date", "acc_id", "trd_env", "trd_market", "code",
    "symbol", "stock_name", "qty", "can_sell_qty", "cost_price", "nominal_price",
    "market_val", "pl_val", "pl_ratio", "currency", "raw_json",
]

# Latest position snapshot joined to each symbol's most-recent daily_picks row.
JOIN_SQL = """
WITH latest_snap AS (
    SELECT MAX(snapshot_ts) AS ts FROM moomoo_position_snapshots
),
latest_pick AS (
    SELECT symbol, MAX(run_date) AS run_date FROM daily_picks GROUP BY symbol
)
SELECT
    p.symbol,
    p.qty,
    p.market_val           AS position_mv,
    p.pl_ratio,
    dp.run_date            AS rich_last_seen,
    dp.total_score,
    dp.recommendation_rank AS rich_rank,
    dp.is_daily_pick
FROM moomoo_position_snapshots p
JOIN      latest_snap s  ON p.snapshot_ts = s.ts
LEFT JOIN latest_pick lp ON lp.symbol = p.symbol
LEFT JOIN daily_picks  dp ON dp.symbol = lp.symbol AND dp.run_date = lp.run_date
ORDER BY p.market_val DESC;
"""


# --- helpers ----------------------------------------------------------------
def _f(d, key):
    """Return d[key] coerced to float, or None for missing / blank / NaN."""
    v = d.get(key)
    if v is None or v == "" or v == "N/A":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return None if f != f else f  # f != f is True only for NaN


def _fmt(v, nd):
    """Format a number with nd decimals; '-' for None."""
    if v is None:
        return "-"
    try:
        return f"{float(v):,.{nd}f}"
    except (TypeError, ValueError):
        return str(v)


def _normalize_symbol(code: str) -> str:
    """'US.AAPL' -> 'AAPL'. Bare codes pass through unchanged."""
    return code.split(".", 1)[1] if "." in code else code


def _market_from_code(code: str) -> str:
    """Derive the real per-position market from a moomoo code prefix.

    'US.PG' -> 'US', 'MY.6742' -> 'MY', 'HK.00700' -> 'HK'. A bare code with no
    exchange prefix is treated as US. This is the actual market of each holding,
    unlike the query-level TRD_MARKET which is identical for every row.
    """
    return code.split(".", 1)[0].upper() if "." in code else "US"


# --- OpenD reachability preflight -------------------------------------------
# When the OpenD gateway is down, the moomoo client retries a refused connection
# indefinitely (one WSAECONNREFUSED every ~8s, forever), so the script hangs
# until it is killed. Probe the port ourselves first with a small fixed retry
# cap and fail fast, never entering moomoo's unbounded internal reconnect loop.
CONNECT_MAX_RETRIES = 3       # total TCP probe attempts before giving up
CONNECT_RETRY_DELAY = 1.0     # seconds between probes
CONNECT_PROBE_TIMEOUT = 2.0   # seconds per probe


def _preflight_opend(host: str = OPEND_HOST, port: int = OPEND_PORT) -> None:
    """Fail fast (SystemExit) if OpenD isn't accepting connections.

    A reachable port returns immediately and all downstream behavior is
    unchanged; an unreachable port exits non-zero after a small, bounded number
    of probes instead of hanging in moomoo's infinite reconnect loop.
    """
    last_err = None
    for attempt in range(1, CONNECT_MAX_RETRIES + 1):
        try:
            with socket.create_connection((host, port), timeout=CONNECT_PROBE_TIMEOUT):
                return  # gateway is up
        except OSError as e:
            last_err = e
            print(f"  OpenD probe {attempt}/{CONNECT_MAX_RETRIES} failed: "
                  f"{host}:{port} unreachable ({e.__class__.__name__})")
            if attempt < CONNECT_MAX_RETRIES:
                time.sleep(CONNECT_RETRY_DELAY)
    raise SystemExit(
        f"OpenD gateway not reachable at {host}:{port} after "
        f"{CONNECT_MAX_RETRIES} attempts ({last_err}). "
        f"Start Moomoo OpenD and retry. No snapshot written."
    )


# --- Moomoo read (no order APIs are ever called) ----------------------------
def fetch_snapshots(snapshot_ts: str):
    """Open a trade context, read accounts + positions, return (acc_rows, pos_rows)."""
    _preflight_opend()  # fail fast if OpenD is down; bounded, no infinite retry
    snapshot_date = snapshot_ts[:10]
    trd_ctx = OpenSecTradeContext(
        filter_trdmarket=TRD_MARKET,
        host=OPEND_HOST,
        port=OPEND_PORT,
        security_firm=SECURITY_FIRM,
    )
    accounts, positions = [], []
    try:
        ret, acc_df = trd_ctx.get_acc_list()
        if ret != RET_OK:
            raise RuntimeError(f"get_acc_list failed: {acc_df}")

        acc_records = acc_df.to_dict(orient="records")
        target = [a for a in acc_records if str(a.get("trd_env")) == str(TRD_ENV)]
        if not target:
            raise RuntimeError(
                f"No {TRD_ENV} account found for firm={SECURITY_FIRM} market={TRD_MARKET}. "
                f"get_acc_list returned: {acc_records}"
            )

        for a in target:
            acc_id = int(a.get("acc_id"))
            acc_id_str = str(acc_id)

            # --- account assets ---
            ret, df = trd_ctx.accinfo_query(
                trd_env=TRD_ENV, acc_id=acc_id, currency=QUERY_CURRENCY
            )
            if ret != RET_OK:
                raise RuntimeError(f"accinfo_query failed (acc {acc_id_str}): {df}")
            for rec in df.to_dict(orient="records"):
                accounts.append({
                    "snapshot_ts": snapshot_ts, "snapshot_date": snapshot_date,
                    "acc_id": acc_id_str, "trd_env": str(TRD_ENV),
                    "trd_market": str(TRD_MARKET), "currency": QUERY_CURRENCY,
                    "total_assets": _f(rec, "total_assets"),
                    "cash": _f(rec, "cash"),
                    "market_val": _f(rec, "market_val"),
                    "frozen_cash": _f(rec, "frozen_cash"),
                    "avl_withdrawal_cash": _f(rec, "avl_withdrawal_cash"),
                    "power": _f(rec, "power"),
                    "unrealized_pl": _f(rec, "unrealized_pl"),
                    "realized_pl": _f(rec, "realized_pl"),
                    "raw_json": json.dumps(rec, default=str, sort_keys=True),
                })

            # --- open positions ---
            ret, df = trd_ctx.position_list_query(
                trd_env=TRD_ENV, acc_id=acc_id, currency=QUERY_CURRENCY
            )
            if ret != RET_OK:
                raise RuntimeError(f"position_list_query failed (acc {acc_id_str}): {df}")
            for rec in df.to_dict(orient="records"):
                code = str(rec.get("code") or "")
                positions.append({
                    "snapshot_ts": snapshot_ts, "snapshot_date": snapshot_date,
                    "acc_id": acc_id_str, "trd_env": str(TRD_ENV),
                    "trd_market": _market_from_code(code),
                    "code": code, "symbol": _normalize_symbol(code),
                    "stock_name": rec.get("stock_name"),
                    "qty": _f(rec, "qty"),
                    "can_sell_qty": _f(rec, "can_sell_qty"),
                    "cost_price": _f(rec, "cost_price"),
                    "nominal_price": _f(rec, "nominal_price"),
                    "market_val": _f(rec, "market_val"),
                    "pl_val": _f(rec, "pl_val"),
                    "pl_ratio": _f(rec, "pl_ratio"),
                    "currency": QUERY_CURRENCY,
                    "raw_json": json.dumps(rec, default=str, sort_keys=True),
                })
    finally:
        trd_ctx.close()  # always release the OpenD connection

    return accounts, positions


# --- DB write (own tables only; INSERT OR REPLACE) --------------------------
def write_snapshots(con, accounts, positions):
    con.execute(SCHEMA_ACCOUNT)
    con.execute(SCHEMA_POSITION)
    con.executemany(
        f"INSERT OR REPLACE INTO moomoo_account_snapshots "
        f"({', '.join(ACCOUNT_COLS)}) VALUES ({', '.join(['?'] * len(ACCOUNT_COLS))})",
        [[r[c] for c in ACCOUNT_COLS] for r in accounts],
    )
    con.executemany(
        f"INSERT OR REPLACE INTO moomoo_position_snapshots "
        f"({', '.join(POSITION_COLS)}) VALUES ({', '.join(['?'] * len(POSITION_COLS))})",
        [[r[c] for c in POSITION_COLS] for r in positions],
    )
    con.commit()


# --- reporting --------------------------------------------------------------
def preview(accounts, positions):
    print(f"  [dry-run] {len(accounts)} account row(s), {len(positions)} position row(s) - NOT written")
    for a in accounts:
        print(f"    ACC {a['acc_id']} {a['currency']}: "
              f"total_assets={_fmt(a['total_assets'], 2)} "
              f"cash={_fmt(a['cash'], 2)} market_val={_fmt(a['market_val'], 2)}")
    for p in positions:
        print(f"    POS {p['symbol']:<8} mkt={p['trd_market']:<3} qty={_fmt(p['qty'], 0):>10} "
              f"market_val={_fmt(p['market_val'], 2):>14} pl%={_fmt(p['pl_ratio'], 2)}")


def print_join_report(con):
    try:
        rows = con.execute(JOIN_SQL).fetchall()
    except sqlite3.OperationalError as e:
        print(f"  (join unavailable: {e} - run a sync first)")
        return
    if not rows:
        print("  (no positions in latest snapshot)")
        return
    hdr = ["SYMBOL", "QTY", "MKT_VAL", "PL%", "RICH_SEEN", "SCORE", "RANK", "PICK"]
    print("  " + "".join(f"{h:>12}" for h in hdr))
    for sym, qty, mv, plr, seen, score, rank, pick in rows:
        pick_s = "YES" if pick == 1 else ("-" if pick is None else "no")
        cells = [
            sym or "?", _fmt(qty, 0), _fmt(mv, 2), _fmt(plr, 2),
            seen or "-", _fmt(score, 1),
            rank if rank is not None else "-", pick_s,
        ]
        print("  " + "".join(f"{str(c):>12}" for c in cells))


# Held positions (latest snapshot) whose RICH standing warrants review.
# "Last 5 trading days" = the 5 most-recent distinct run_date values in daily_picks.
REVIEW_SQL = """
WITH latest_snap AS (
    SELECT MAX(snapshot_ts) AS ts FROM moomoo_position_snapshots
),
recent5 AS (
    SELECT DISTINCT run_date FROM daily_picks ORDER BY run_date DESC LIMIT 5
),
latest_pick AS (
    SELECT symbol, MAX(run_date) AS run_date FROM daily_picks GROUP BY symbol
)
SELECT
    p.symbol, p.qty, p.market_val, p.pl_ratio,
    dp.run_date, dp.total_score, dp.recommendation_rank, dp.beneish_flag, dp.f_score,
    (lp.run_date IN (SELECT run_date FROM recent5)) AS seen_recent
FROM moomoo_position_snapshots p
JOIN      latest_snap s  ON p.snapshot_ts = s.ts
LEFT JOIN latest_pick lp ON lp.symbol = p.symbol
LEFT JOIN daily_picks  dp ON dp.symbol = lp.symbol AND dp.run_date = lp.run_date
ORDER BY p.market_val DESC;
"""


def print_review_report(con):
    """Flag held positions on RICH exit signals (Phase 5 rules). Read-only."""
    try:
        rows = con.execute(REVIEW_SQL).fetchall()
    except sqlite3.OperationalError as e:
        print(f"  (review unavailable: {e} - run a sync first)")
        return
    flagged = 0
    for sym, qty, mv, plr, seen, score, rank, beneish, fscore, recent in rows:
        flags = []
        if beneish == 1 or (fscore is not None and fscore <= 3):
            flags.append("HARD(beneish/f_score<=3)")
        if not recent:
            flags.append("GONE(>5 trading days)")
        if score is not None and score < 50:
            flags.append("DEGRADED(score<50)")
        if not flags:
            continue
        flagged += 1
        print(f"  {sym or '?':<8} qty {_fmt(qty, 0):>9}  mkt_val {_fmt(mv, 2):>13}  pl% {_fmt(plr, 2):>7}")
        print(f"           RICH seen {seen or '-'}  score {_fmt(score, 1)}  "
              f"rank {rank if rank is not None else '-'}  | {', '.join(flags)}")
    print(f"  {flagged} position(s) to review." if flagged else "  No positions to review.")


# --- entry point ------------------------------------------------------------
def main():
    ap = argparse.ArgumentParser(
        description="RICH Phase 1 - read-only Moomoo portfolio sync (no trading)."
    )
    ap.add_argument("--dry-run", action="store_true",
                    help="Fetch from Moomoo and preview only; perform NO DB writes.")
    ap.add_argument("--report", action="store_true",
                    help="Skip Moomoo; print the join report from existing DB rows.")
    ap.add_argument("--review", action="store_true",
                    help="Skip Moomoo; flag held positions on RICH exit signals (read-only).")
    args = ap.parse_args()

    if not os.path.exists(DB_PATH):
        raise SystemExit(f"DB not found: {DB_PATH}")

    # --report: pure read of the DB, no Moomoo round-trip.
    if args.report:
        con = sqlite3.connect(DB_PATH)
        try:
            print("RICH x Moomoo - latest positions vs latest daily_picks")
            print_join_report(con)
        finally:
            con.close()
        return

    # --review: Phase-5-style exit signals on held positions; pure DB read.
    if args.review:
        con = sqlite3.connect(DB_PATH)
        try:
            print("RICH x Moomoo - positions to review (Phase 5 exit signals)")
            print_review_report(con)
        finally:
            con.close()
        return

    snapshot_ts = datetime.now().isoformat(timespec="seconds")
    print(f"Moomoo sync @ {snapshot_ts}  "
          f"(env={TRD_ENV}, market={TRD_MARKET}, firm={SECURITY_FIRM})")
    accounts, positions = fetch_snapshots(snapshot_ts)
    print(f"Fetched {len(accounts)} account row(s), {len(positions)} position row(s).")

    if args.dry_run:
        preview(accounts, positions)
        return

    con = sqlite3.connect(DB_PATH)
    try:
        write_snapshots(con, accounts, positions)
        print(f"Wrote snapshot {snapshot_ts} -> {DB_PATH}")
        print("\nRICH x Moomoo - latest positions vs latest daily_picks")
        print_join_report(con)
    finally:
        con.close()


if __name__ == "__main__":
    main()
