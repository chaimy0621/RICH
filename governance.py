"""Read-only governance report for RICH shadow signals.

Usage:
    python governance.py          # print table to stdout
    python governance.py --json   # print JSON to stdout

Zero DB writes. Zero FMP calls. Safe to run at any time.
"""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from datetime import date
from pathlib import Path

from config import CACHE_DB, SIGNAL_GOVERNANCE

TODAY = date.today().isoformat()


def _db_columns(path: Path) -> set[str]:
    """Return the set of column names currently in daily_picks."""
    with sqlite3.connect(path) as conn:
        rows = conn.execute("PRAGMA table_info(daily_picks)").fetchall()
    return {r[1] for r in rows}


def _query_signal(path: Path, col: str) -> dict:
    """Return null_rate, obs_count, first_seen for a shadow column (live picks only)."""
    with sqlite3.connect(path) as conn:
        row = conn.execute(
            f"""
            SELECT
                COUNT(*)                                           AS total,
                SUM(CASE WHEN "{col}" IS NULL THEN 1 ELSE 0 END)  AS nulls,
                MIN(CASE WHEN "{col}" IS NOT NULL THEN run_date END) AS first_seen
            FROM daily_picks
            WHERE is_backfill IS NULL OR is_backfill = 0
            """,
        ).fetchone()
    total, nulls, first_seen = row
    null_rate = (nulls / total) if total else None
    return {
        "obs_count": total or 0,
        "null_rate": null_rate,
        "first_seen": first_seen,
    }


def build_report(path: Path = CACHE_DB) -> list[dict]:
    if not path.exists():
        return []

    existing_cols = _db_columns(path)
    rows = []
    for signal, meta in SIGNAL_GOVERNANCE.items():
        if signal in existing_cols:
            db_info = _query_signal(path, signal)
        else:
            db_info = {"obs_count": 0, "null_rate": None, "first_seen": None}

        gate = meta["gate_date"]
        overdue = bool(gate and gate < TODAY and meta["status"] == "shadow")

        rows.append({
            "signal": signal,
            "sleeve": meta["sleeve"],
            "status": meta["status"],
            "null_pct": (
                f"{db_info['null_rate'] * 100:.0f}%"
                if db_info["null_rate"] is not None else "n/a"
            ),
            "obs": db_info["obs_count"],
            "first_seen": db_info["first_seen"] or meta.get("shadow_since", "—"),
            "gate_date": gate or "parked",
            "flag": "OVERDUE" if overdue else "",
            # raw for JSON
            "_null_rate": db_info["null_rate"],
            "_first_seen_db": db_info["first_seen"],
            "_overdue": overdue,
            "_meta": meta,
        })
    return rows


def print_table(rows: list[dict]) -> None:
    if not rows:
        print("No signals registered or DB not found.")
        return

    header = f"{'Signal':<38} {'Sleeve':<12} {'Status':<8} {'Null%':>6} {'Obs':>5}  {'First Seen':>12}  {'Gate Date':>12}  Flag"
    print(header)
    print("-" * len(header))
    for r in rows:
        flag_str = f"  *** {r['flag']} ***" if r["flag"] else ""
        print(
            f"{r['signal']:<38} {r['sleeve']:<12} {r['status']:<8} "
            f"{r['null_pct']:>6} {r['obs']:>5}  {r['first_seen']:>12}  "
            f"{r['gate_date']:>12}{flag_str}"
        )

    overdue_count = sum(1 for r in rows if r["_overdue"])
    if overdue_count:
        print(f"\n{overdue_count} signal(s) past gate date with no promotion decision.")
    else:
        print("\nAll signals within gate window.")


def main() -> None:
    parser = argparse.ArgumentParser(description="RICH shadow signal governance report")
    parser.add_argument("--json", action="store_true", help="Output as JSON")
    parser.add_argument("--db", default=None, help="Override DB path")
    args = parser.parse_args()

    path = Path(args.db) if args.db else CACHE_DB
    rows = build_report(path)

    if args.json:
        output = []
        for r in rows:
            output.append({
                "signal": r["signal"],
                "sleeve": r["sleeve"],
                "status": r["status"],
                "null_rate": r["_null_rate"],
                "obs_count": r["obs"],
                "first_seen": r["_first_seen_db"] or r["_meta"].get("shadow_since"),
                "gate_date": r["_meta"]["gate_date"],
                "overdue": r["_overdue"],
                "promotion_criteria": r["_meta"]["promotion_criteria"],
                "notes": r["_meta"]["notes"],
            })
        print(json.dumps(output, indent=2))
    else:
        print_table(rows)


if __name__ == "__main__":
    main()
