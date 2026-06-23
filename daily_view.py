"""RICH daily viewer — read-only.

Usage:
    py daily_view.py                    # today (MYT)
    py daily_view.py --date 2026-04-27
"""
from __future__ import annotations

import argparse
import csv
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

ROOT    = Path(__file__).parent
DB      = ROOT / "data" / "cache.db"
LOG_DIR = ROOT / "logs"
METRICS = LOG_DIR / "run_metrics.csv"
TZ      = ZoneInfo("Asia/Kuala_Lumpur")
COOLDOWN_DAYS = 60

W   = 66
BAR = "=" * W
SEP = "-" * W


# ── formatters ────────────────────────────────────────────────────────────────

def _f(v, fmt=".1f") -> str:
    try:
        return format(float(v), fmt) if v is not None else "n/a"
    except (TypeError, ValueError):
        return "n/a"


def _signed(v) -> str:
    try:
        return f"{float(v):+.2f}" if v is not None else "n/a"
    except (TypeError, ValueError):
        return "n/a"


def _rank_marker(orig, adj) -> str:
    """Return arrow if adjusted rank differs from original rank."""
    try:
        diff = int(orig) - int(adj)   # positive = moved up
        if diff > 0:
            return f"  ▲+{diff}"
        if diff < 0:
            return f"  ▼{diff}"
    except (TypeError, ValueError):
        pass
    return ""


def _vlabel(label: str | None) -> str:
    if label == "deep_discount":
        return "deep_discount [**]"
    return label or "n/a"


# ── data readers ──────────────────────────────────────────────────────────────

def _db_rows(sql: str, params: tuple = ()) -> list[dict]:
    with sqlite3.connect(DB) as con:
        con.row_factory = sqlite3.Row
        return [dict(r) for r in con.execute(sql, params).fetchall()]


def get_pick(run_date: str) -> dict | None:
    rows = _db_rows(
        "SELECT * FROM daily_picks WHERE run_date=? AND is_daily_pick=1",
        (run_date,),
    )
    return rows[0] if rows else None


def get_top5(run_date: str) -> list[dict]:
    return _db_rows(
        "SELECT * FROM daily_picks WHERE run_date=? "
        "AND recommendation_rank IS NOT NULL "
        "ORDER BY recommendation_rank LIMIT 5",
        (run_date,),
    )


def last_metrics() -> dict | None:
    if not METRICS.exists():
        return None
    with open(METRICS, newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    return rows[-1] if rows else None


# ── section printers ──────────────────────────────────────────────────────────

def section_run_status(target: str) -> None:
    log_exists = (LOG_DIR / f"{target}.log").exists()
    print(f"\n  Run Status")
    print(f"  {SEP}")
    print(f"  date       : {target}")
    print(f"  log file   : {'[found]' if log_exists else '[NOT FOUND — pipeline not yet run]'}")

    m = last_metrics()
    if m is None:
        print("  metrics    : run_metrics.csv not found")
        return

    ts       = m.get("timestamp_utc", "?")
    m_date   = m.get("run_date", "?")
    elapsed  = m.get("elapsed_s", "?")
    scored   = m.get("scored", "?")
    ferr     = m.get("fetch_error_count", "?")
    ferr_pct = m.get("fetch_error_rate_pct", "?")

    stale = f"  [last run was {m_date}]" if m_date != target else ""
    print(f"  last run   : {ts}{stale}")
    print(f"  elapsed    : {elapsed}s   scored: {scored}   "
          f"fetch_err: {ferr} ({ferr_pct}%)")


def section_pick(pick: dict) -> None:
    sym      = pick.get("symbol", "?")
    name     = pick.get("company_name") or ""
    ts       = pick.get("total_score")
    ts_adj   = pick.get("total_score_adj")
    rank     = pick.get("recommendation_rank", "?")
    rank_adj = pick.get("recommendation_rank_with_valuation", "?")
    marker   = _rank_marker(rank, rank_adj)

    cd_until = "?"
    rd = pick.get("run_date", "")
    try:
        cd_until = (datetime.fromisoformat(rd) + timedelta(days=COOLDOWN_DAYS)).date().isoformat()
    except ValueError:
        pass

    print(f"\n  Daily Pick")
    print(f"  {SEP}")
    print(f"  symbol         : {sym}   {name}")
    print(f"  price          : n/a (not stored — check report_today.py for live price)")
    print(f"  total_score    : {_f(ts)}   total_score_adj : {_f(ts_adj)}")
    print(f"  rank original  : {rank}   rank adj        : {rank_adj}{marker}")
    print(f"  cooldown until : {cd_until}")


def section_valuation(pick: dict) -> None:
    label  = pick.get("valuation_label") or "n/a"
    adj    = pick.get("valuation_adj")
    fvpe   = pick.get("fair_value_pe")
    smpe   = pick.get("sector_median_pe")
    pvs    = pick.get("pe_vs_sector")
    model  = pick.get("valuation_model_version") or "?"

    print(f"\n  Valuation Overlay  (shadow — not in ranking)")
    print(f"  {SEP}")
    if label == "n/a":
        print("  n/a  (no usable P/E for this pick)")
        return
    print(f"  label          : {_vlabel(label)}")
    print(f"  valuation_adj  : {_signed(adj)} pts")
    print(f"  sector PE      : {_f(smpe)}   fair value PE : {_f(fvpe)}")
    print(f"  pe_vs_sector   : {_f(pvs, '.3f')}x")
    print(f"  model          : {model}")


def section_top5(rows: list[dict]) -> None:
    print(f"\n  Top-5")
    print(f"  {SEP}")
    if not rows:
        print("  (no rows for this date)")
        return
    hdr = (f"  {'rk':>2}  {'sym':6}  {'score':>5}  {'adj':>5}  "
           f"{'Q':>4}  {'G':>4}  {'VR':>4}  {'label':18}  {'vadj':>6}")
    print(hdr)
    print(f"  {'-' * 62}")
    for r in rows:
        label   = _vlabel(r.get("valuation_label"))[:18]
        is_pick = " <" if r.get("is_daily_pick") == 1 else ""
        print(
            f"  {r.get('recommendation_rank', '?'):>2}  "
            f"{(r.get('symbol') or ''):6}  "
            f"{_f(r.get('total_score')):>5}  "
            f"{_f(r.get('total_score_adj')):>5}  "
            f"{_f(r.get('quality_score')):>4}  "
            f"{_f(r.get('growth_score')):>4}  "
            f"{_f(r.get('value_risk_score')):>4}  "
            f"{label:18}  "
            f"{_signed(r.get('valuation_adj')):>6}"
            f"{is_pick}"
        )


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> int:
    parser = argparse.ArgumentParser(description="RICH daily viewer")
    parser.add_argument("--date", default=None,
                        help="Run date YYYY-MM-DD (default: today MYT)")
    args = parser.parse_args()
    target = args.date or datetime.now(TZ).date().isoformat()

    print(BAR)
    print(f"  RICH DAILY VIEW  —  {target} (MYT)")
    print(BAR)

    section_run_status(target)

    if not DB.exists():
        print(f"\n  [ERROR] {DB} not found.  Run `py run_daily.py` first.")
        print(f"\n{BAR}")
        return 1

    try:
        pick = get_pick(target)
    except sqlite3.OperationalError as exc:
        print(f"\n  [ERROR] DB read failed: {exc}")
        print(f"\n{BAR}")
        return 1

    if pick is None:
        print(f"\n  No daily pick for {target}.  Run `py run_daily.py` first.")
        print(f"\n{BAR}")
        return 1

    section_pick(pick)
    section_valuation(pick)
    section_top5(get_top5(target))

    print(f"\n{BAR}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
