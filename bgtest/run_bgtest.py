"""bgtest entry point — isolated shadow run of RICH + background research.

Phase 1: ranking and top-5 extraction only.
Source discovery and document fetching are added in later phases.

Usage (run from the RICH project root):
    py bgtest\\run_bgtest.py
    py bgtest\\run_bgtest.py --dry-run   # log top-5 only; no bgtest DB writes

The --dry-run flag skips bg_storage.init_db(). Note: pipeline.run() still
creates bgtest.db/daily_picks (via storage.init_db) even in dry-run mode —
this is idempotent and does not affect production.
"""
from __future__ import annotations

import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# Path setup — must precede all project imports.
_HERE      = Path(__file__).parent
_RICH_ROOT = _HERE.parent
for _p in (str(_RICH_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config import TIMEZONE                                  # noqa: E402  (production)
from logging_utils import append_run_metrics, setup_logging  # noqa: E402  (production)
from config_bgtest import BGTEST_LOG_DIR                     # noqa: E402
from bg_orchestrator import run as orchestrate               # noqa: E402


def main() -> int:
    dry_run = "--dry-run" in sys.argv

    setup_logging(log_dir=BGTEST_LOG_DIR)
    today = datetime.now(ZoneInfo(TIMEZONE)).date()
    log = logging.getLogger("run_bgtest")
    log.info(
        "=== RICH bgtest shadow run %s (MYT)%s ===",
        today.isoformat(),
        "  [dry-run]" if dry_run else "",
    )

    try:
        result = orchestrate(today=today, dry_run=dry_run)
    except Exception:
        log.exception("bgtest orchestrator failed")
        return 1

    top5 = result.get("bgtest_top5") or []
    log.info(
        "bgtest top-5 summary: %s",
        " | ".join(
            f"{c['symbol']} rank={c['recommendation_rank']} "
            f"score={c['scoring']['total_score']:.1f}"
            for c in top5
        ) or "<empty>",
    )

    if not dry_run:
        csv_path = append_run_metrics(result, log_dir=BGTEST_LOG_DIR)
        log.info("run metrics appended to %s", csv_path)

    return 0


if __name__ == "__main__":
    sys.exit(main())
