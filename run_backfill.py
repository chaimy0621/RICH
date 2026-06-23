"""
Backfill historical pipeline runs for a date range.

Usage:
  python run_backfill.py --start 2026-02-01 --end 2026-05-23
  python run_backfill.py --start 2026-02-01 --end 2026-05-23 --dry-run
  python run_backfill.py --start 2026-02-01 --end 2026-05-23 --delay 3.0

Skips any date that already has an is_daily_pick=1 row in daily_picks.
Marks every row written by this script with is_backfill=1.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from datetime import date

import pandas_market_calendars as mcal

import pipeline
from config import CACHE_DB


def _trading_days(start: date, end: date) -> list[date]:
    nyse = mcal.get_calendar("NYSE")
    schedule = nyse.schedule(start_date=start.isoformat(), end_date=end.isoformat())
    return [ts.date() for ts in schedule.index]


def _already_run(d: date) -> bool:
    try:
        conn = sqlite3.connect(CACHE_DB)
        row = conn.execute(
            "SELECT 1 FROM daily_picks WHERE run_date=? AND is_daily_pick=1",
            (d.isoformat(),),
        ).fetchone()
        conn.close()
        return row is not None
    except Exception:
        return False


def _mark_backfill(d: date) -> None:
    conn = sqlite3.connect(CACHE_DB)
    conn.execute(
        "UPDATE daily_picks SET is_backfill=1 WHERE run_date=?",
        (d.isoformat(),),
    )
    conn.commit()
    conn.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="Backfill RICH pipeline for a date range.")
    parser.add_argument("--start",   required=True, help="Start date YYYY-MM-DD (inclusive)")
    parser.add_argument("--end",     required=True, help="End date YYYY-MM-DD (inclusive)")
    parser.add_argument("--dry-run", action="store_true", help="Print plan without running")
    parser.add_argument("--delay",   type=float, default=2.0,
                        help="Seconds to sleep between runs (default 2.0)")
    args = parser.parse_args()

    start = date.fromisoformat(args.start)
    end   = date.fromisoformat(args.end)
    days  = _trading_days(start, end)

    print(f"Trading days in range: {len(days)}  ({start} to {end})")

    ran = skipped = failed = 0

    for d in days:
        if _already_run(d):
            print(f"[SKIP] {d}")
            skipped += 1
            continue

        if args.dry_run:
            print(f"[DRY]  {d}")
            ran += 1
            continue

        try:
            pipeline.run(persist=True, max_workers=6, run_date=d)
            _mark_backfill(d)
            print(f"[RUN]  {d}")
            ran += 1
        except Exception as exc:
            print(f"[FAIL] {d} — {exc}")
            failed += 1

        time.sleep(args.delay)

    print(f"\nDone: ran={ran} skipped={skipped} failed={failed}")


if __name__ == "__main__":
    main()
