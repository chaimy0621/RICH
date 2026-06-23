"""One-shot Discord daily report for RICH — replaces the removed email summary.

Reads daily_picks from data/cache.db (read-only) and posts a single plain-text
message to a Discord webhook. No imports from any core RICH module.

Usage:
    py discord_daily_report.py            # post to DISCORD_WEBHOOK_URL
    py discord_daily_report.py --dry-run  # print the message, send nothing

Env vars:
    DISCORD_WEBHOOK_URL   required unless --dry-run is set
"""
from __future__ import annotations

import argparse
import os
import sqlite3
import sys
from pathlib import Path

import requests

_DB_PATH = Path(__file__).resolve().parent / "data" / "cache.db"
_WHY_MAX_CHARS = 140


def _connect_ro(db_path: Path) -> sqlite3.Connection:
    if not db_path.exists():
        sys.exit(f"ERROR: database not found: {db_path}")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _fmt(val, spec: str = ".1f") -> str:
    if val is None:
        return "n/a"
    try:
        return format(float(val), spec)
    except (TypeError, ValueError):
        return "n/a"


def _state(row: sqlite3.Row) -> str:
    held = bool(row["is_held"])
    cd = bool(row["in_cooldown"])
    if held and cd:
        return "held+cd"
    if held:
        return "held"
    if cd:
        return "cd"
    return ""


def _latest_run_date(conn: sqlite3.Connection) -> str | None:
    # "Today" = most recent run stored in the DB, never the system clock,
    # so the report stays correct when run after midnight or on a rerun day.
    row = conn.execute("SELECT MAX(run_date) AS d FROM daily_picks").fetchone()
    return row["d"]


def _pick_row(conn: sqlite3.Connection, run_date: str) -> sqlite3.Row | None:
    # Multi-run days store one is_daily_pick=1 row per run with no timestamp
    # column; highest rowid = most recently inserted = latest run wins.
    return conn.execute(
        "SELECT * FROM daily_picks"
        " WHERE run_date = ? AND is_daily_pick = 1"
        " ORDER BY rowid DESC LIMIT 1",
        (run_date,),
    ).fetchone()


def _top5_rows(conn: sqlite3.Connection, run_date: str) -> list[sqlite3.Row]:
    # Same multi-run caveat: ranks 1-5 can appear once per run, so keep only
    # the latest row per rank.
    return conn.execute(
        "SELECT * FROM daily_picks"
        " WHERE rowid IN ("
        "   SELECT MAX(rowid) FROM daily_picks"
        "   WHERE run_date = ? AND recommendation_rank BETWEEN 1 AND 5"
        "   GROUP BY recommendation_rank"
        " )"
        " ORDER BY recommendation_rank",
        (run_date,),
    ).fetchall()


def _why_line(pick: sqlite3.Row) -> str:
    why = pick["short_reason"] or pick["top_drivers"] or "n/a"
    why = " ".join(str(why).split())
    if len(why) > _WHY_MAX_CHARS:
        why = why[: _WHY_MAX_CHARS - 1].rstrip() + "…"
    return why


def build_message(conn: sqlite3.Connection) -> str:
    run_date = _latest_run_date(conn)
    if run_date is None:
        sys.exit("ERROR: daily_picks is empty — nothing to report.")

    pick = _pick_row(conn, run_date)
    top5 = _top5_rows(conn, run_date)

    lines: list[str] = []
    lines.append(f"RICH — {run_date} (MYT)")

    if pick is not None:
        lines.append(
            f"Pick: {pick['symbol']}"
            f"  |  Score {_fmt(pick['total_score'])}"
            f"  Q {_fmt(pick['quality_score'])}"
            f"  G {_fmt(pick['growth_score'])}"
            f"  VR {_fmt(pick['value_risk_score'])}"
        )
        price = _fmt(pick["pick_price"], ".2f")
        price_str = f"${price}" if price != "n/a" else "n/a"
        lines.append(
            f"Valuation: {pick['valuation_label'] or 'n/a'}"
            f"  |  Price: {price_str}"
        )
        lines.append(f"Why: {_why_line(pick)}")
    else:
        lines.append("Pick: none")
        lines.append("Reason: no candidate passed all gates today.")

    lines.append("")

    if top5:
        lines.append("Top 5 today:")
        for r in top5:
            state = _state(r)
            lines.append(
                f"#{r['recommendation_rank']} {r['symbol']}"
                f"  {_fmt(r['total_score'])}"
                f"  Q {_fmt(r['quality_score'])}"
                f"  G {_fmt(r['growth_score'])}"
                f"  VR {_fmt(r['value_risk_score'])}"
                + (f"  {state}" if state else "")
            )
    else:
        lines.append("No top-5 rows stored for today.")

    return "\n".join(lines)


def send_to_discord(webhook_url: str, message: str) -> None:
    resp = requests.post(webhook_url, json={"content": message}, timeout=15)
    if resp.status_code not in (200, 204):
        sys.exit(
            f"ERROR: Discord webhook returned HTTP {resp.status_code}: "
            f"{resp.text[:300]}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description="Post the RICH daily report to Discord.")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the message to stdout instead of posting",
    )
    args = parser.parse_args()

    webhook_url = os.environ.get("DISCORD_WEBHOOK_URL")
    if not webhook_url and not args.dry_run:
        sys.exit(
            "ERROR: DISCORD_WEBHOOK_URL is not set. "
            "Set it, or use --dry-run to preview the message."
        )

    conn = _connect_ro(_DB_PATH)
    try:
        message = build_message(conn)
    finally:
        conn.close()

    if args.dry_run:
        print(message)
        return 0

    send_to_discord(webhook_url, message)
    print(f"Posted RICH daily report ({len(message)} chars) to Discord.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
