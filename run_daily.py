"""Manual daily entrypoint. Scheduler is intentionally NOT installed yet.

Run by hand 2-3 times across different days/time windows to observe FETCH_ERROR
stability (target <10%). Only after stability is confirmed do we wire Task
Scheduler. Each run appends one row to logs/run_metrics.csv for audit.

Usage:
    py run_daily.py
"""
from __future__ import annotations
import subprocess, sys, pathlib
check = subprocess.run(
    [sys.executable, str(pathlib.Path(__file__).parent / "scripts/rich_check.py")],
    capture_output=True, text=True
)
if check.returncode != 0:
    print(check.stdout); print(check.stderr); sys.exit("rich_check FAILED — pipeline aborted")

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import TIMEZONE, CACHE_DB
from data.storage import connect
from logging_utils import append_run_metrics, setup_logging
from pipeline import run


def write_top5_log(run_date) -> Path:
    """Sidecar for the Shadow layer: today's Top 5 as plain text in RICH\\logs,
    so Shadow reads it via its existing logs read-only access — cache.db stays
    out of Shadow's reach. One line per rank:
        rank|symbol|score|rank_delta|valuation_label|gap_pct
    rank_delta = today's recommendation_rank - yesterday's (NEGATIVE = moved up;
    NEW if unranked yesterday).  gap_pct = (pe_vs_sector - 1) * 100, whole %.
    """
    rd = run_date.isoformat() if hasattr(run_date, "isoformat") else str(run_date)
    with connect(CACHE_DB) as c:
        today = c.execute(
            "SELECT recommendation_rank rk, symbol, total_score, valuation_label, "
            "pe_vs_sector FROM daily_picks WHERE run_date=? "
            "AND recommendation_rank BETWEEN 1 AND 5 ORDER BY recommendation_rank",
            (rd,)).fetchall()
        yday = c.execute("SELECT MAX(run_date) FROM daily_picks WHERE run_date<?",
                         (rd,)).fetchone()[0]
        yrank = {r["symbol"]: r["rk"] for r in c.execute(
            "SELECT symbol, recommendation_rank rk FROM daily_picks "
            "WHERE run_date=? AND recommendation_rank IS NOT NULL", (yday,))} if yday else {}
    lines = []
    for r in today:
        yr = yrank.get(r["symbol"])
        delta = "NEW" if yr is None else str(r["rk"] - yr)
        gap = "" if r["pe_vs_sector"] is None else f"{(r['pe_vs_sector'] - 1) * 100:.0f}"
        lines.append(f"{r['rk']}|{r['symbol']}|{r['total_score']:.1f}|{delta}|"
                     f"{r['valuation_label'] or 'n/a'}|{gap}")
    out = Path(__file__).parent / "logs" / f"{rd}-top5.txt"
    out.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return out


def main() -> int:
    setup_logging()
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    log = logging.getLogger("run_daily")
    log.info("=== RICH manual run %s (MYT) ===", today.isoformat())
    try:
        result = run(run_date=today, persist_full_universe=True)
    except Exception:
        log.exception("pipeline failed")
        return 1

    csv_path = append_run_metrics(result)
    pick = result.get("daily_pick")
    log.info("daily pick: %s  metrics appended to %s",
             pick["symbol"] if pick else "<none>", csv_path)

    # Shadow-facing Top 5 sidecar (logs/<date>-top5.txt). Auxiliary artifact —
    # never fail the daily pipeline if it can't be written.
    try:
        log.info("wrote top5 sidecar: %s", write_top5_log(today))
    except Exception:
        log.warning("top5 sidecar write failed (non-fatal)", exc_info=True)

    return 0


if __name__ == "__main__":
    sys.exit(main())
