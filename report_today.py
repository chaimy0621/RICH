"""Render today's daily pick as a compact one-screen report.

Reads daily_picks WHERE run_date=<today MYT> AND is_daily_pick=1,
re-fetches current P/E + profile for valuation context, prints to stdout.

Usage:
    py report_today.py                    # today (MYT)
    py report_today.py --date 2026-04-25  # specific run date
"""
from __future__ import annotations

import argparse
import sys
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from config import COOLDOWN_DAYS, TIMEZONE
from data import storage
from data.fetcher import FMPClient


def pe_band(pe: float | None) -> str:
    if pe is None or pe <= 0:
        return "n/a"
    if pe < 12:
        return "cheap"
    if pe < 25:
        return "fair"
    if pe < 40:
        return "expensive"
    return "very expensive"


def size_bucket(mcap: float | None) -> str:
    if mcap is None:
        return "?"
    b = mcap / 1e9
    if b < 10:
        return f"small (${b:.1f}B)"
    if b < 200:
        return f"mid (${b:.0f}B)"
    return f"large (${b/1000:.2f}T)"


def margin_of_safety_label(total_score: float, value_risk_score: float) -> str:
    """Coarse MoS proxy: blends overall score with value/risk sleeve."""
    if value_risk_score >= 70 and total_score >= 80:
        return "high"
    if value_risk_score >= 50 and total_score >= 65:
        return "medium"
    return "low"


def cooldown_status(run_date: str) -> str:
    rd = date.fromisoformat(run_date)
    until = rd + timedelta(days=COOLDOWN_DAYS)
    return f"will be in cooldown until {until.isoformat()} ({COOLDOWN_DAYS}d)"


def get_today_pick(today_iso: str) -> dict | None:
    rows = storage.fetch_run(today_iso)
    for r in rows:
        if r.get("is_daily_pick") == 1:
            return r
    return None


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--date", default=None,
                        help="Run date YYYY-MM-DD (default: today MYT)")
    args = parser.parse_args()

    target = args.date or datetime.now(ZoneInfo(TIMEZONE)).date().isoformat()
    pick = get_today_pick(target)
    if not pick:
        print(f"No daily pick recorded for {target} (MYT).")
        print("Run `py run_daily.py` first, or pass --date <YYYY-MM-DD>.")
        return 1

    sym = pick["symbol"]
    client = FMPClient()
    try:
        prof = client.profile(sym)
    except Exception:
        prof = {}
    try:
        ratios = client.ratios(sym, years=1)
        cur_pe = (ratios[0] or {}).get("priceToEarningsRatio") if ratios else None
    except Exception:
        cur_pe = None

    price = prof.get("price")
    mcap = prof.get("mktCap") or prof.get("marketCap")
    exch = prof.get("exchange") or prof.get("exchangeShortName") or "?"
    sector = prof.get("sector") or "?"

    band = pe_band(cur_pe)
    mos = margin_of_safety_label(pick["total_score"], pick["value_risk_score"])

    bar = "=" * 64
    print(bar)
    print(f"  RICH DAILY RECOMMENDATION  -  {pick['run_date']} (MYT)")
    print(bar)
    print(f"  {sym:6} {pick['company_name'] or ''}")
    tscore     = pick["total_score"]
    tscore_adj = pick.get("total_score_adj")
    rank_orig  = pick.get("recommendation_rank", "?")
    rank_adj   = pick.get("recommendation_rank_with_valuation", "?")
    adj_str    = f"   total_score_adj: {tscore_adj:.2f}/100" if tscore_adj is not None else ""
    print(f"  total_score: {tscore:>5}/100{adj_str}"
          + (f"   price: ${price}" if price else ""))
    print(f"  rank: {rank_orig} (original)   {rank_adj} (with valuation adj)")
    print()
    print("  Sleeves:")
    print(f"    Quality      {pick['quality_score']:>5}/100")
    print(f"    Growth       {pick['growth_score']:>5}/100")
    print(f"    Value/Risk   {pick['value_risk_score']:>5}/100")
    print()
    print(f"  Top drivers     : {pick['top_drivers']}")
    print(f"  Bottom drivers  : {pick['bottom_drivers']}")
    print()
    print("  Valuation:")
    pe_str = f"{cur_pe:.1f}" if isinstance(cur_pe, (int, float)) else "n/a"
    print(f"    current P/E       : {pe_str}   band: {band}")
    print(f"    margin of safety  : {mos}")
    print()
    print("  Valuation overlay (shadow — not applied to ranking):")
    v_label = pick.get("valuation_label", "n/a")
    if v_label and v_label != "n/a":
        smpe = f"{pick['sector_median_pe']:.1f}" if pick.get("sector_median_pe") is not None else "n/a"
        fvpe = f"{pick['fair_value_pe']:.1f}"    if pick.get("fair_value_pe")    is not None else "n/a"
        pvs  = f"{pick['pe_vs_sector']:.2f}x"   if pick.get("pe_vs_sector")     is not None else "n/a"
        vadj = pick.get("valuation_adj", 0.0)
        print(f"    sector median P/E : {smpe}")
        print(f"    fair value P/E    : {fvpe}   ({pvs} of fair)")
        print(f"    valuation label   : {v_label}")
        print(f"    valuation adj     : {vadj:+.2f} pts")
    else:
        print("    n/a (no usable P/E in stored run)")
    print()
    print("  Background:")
    print(f"    exchange          : {exch}   sector: {sector}")
    print(f"    size              : {size_bucket(mcap)}")
    print(f"    history available : {pick['historical_years_available']} years")
    print(f"    cooldown          : {cooldown_status(pick['run_date'])}")
    if pick.get("sector_concentration_warning"):
        print("    sector conc.      : WARNING - 2+ of today's top-5 share a sector "
              "(warn only; nothing excluded)")
    print()
    print(f"  Reason: {pick['short_reason']}")
    print(bar)
    return 0


if __name__ == "__main__":
    sys.exit(main())
