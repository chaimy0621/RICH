"""bgtest orchestrator — Phase 2: pipeline call, top-5 extraction, source discovery.

Calls the production pipeline with persist=False and an isolated DB path,
then reads the top-BGTEST_TOP_N stocks by recommendation_rank (= total_score
rank), then runs SEC EDGAR and IR source discovery for each candidate.

Candidate selection rule
------------------------
  top N by recommendation_rank (assigned by total_score, before cooldown).
  Never by recommendation_rank_with_valuation (shadow valuation overlay).

Side-effects on bgtest.db
--------------------------
  pipeline.run() always calls storage.init_db(BGTEST_DB_PATH) — this creates
  the daily_picks schema in bgtest.db even with persist=False. This is
  idempotent and isolated from production (different file path).

  bg_storage.init_db() creates the four bgtest-specific tables.
  With dry_run=True this step is skipped.
"""
from __future__ import annotations

import logging
import sys
from datetime import date
from pathlib import Path
from typing import Any

# Path setup — must precede all project imports.
_HERE      = Path(__file__).parent
_RICH_ROOT = _HERE.parent
for _p in (str(_RICH_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config_bgtest import BGTEST_DB_PATH, BGTEST_TOP_N  # noqa: E402
import bg_storage                                        # noqa: E402
import document_fetcher                                  # noqa: E402
import memo_generator                                    # noqa: E402
import pipeline                                          # noqa: E402
import source_discovery                                  # noqa: E402

log = logging.getLogger(__name__)


def run(
    today: date | None = None,
    dry_run: bool = False,
) -> dict[str, Any]:
    """Run the bgtest shadow pipeline.

    Returns the pipeline result dict extended with:
        bgtest_top5 : list[dict] — top BGTEST_TOP_N candidates by
                      recommendation_rank (ascending), each being a raw
                      pipeline candidate dict including 'scoring' and 'factors'.
    """
    log.info(
        "[bgtest 1/6] %sbgtest DB: %s",
        "[dry-run] skipping init for " if dry_run else "initialising ",
        BGTEST_DB_PATH,
    )
    if not dry_run:
        bg_storage.init_db()

    log.info(
        "[bgtest 2/6] pipeline.run(persist=False, db_path=%s) ...",
        BGTEST_DB_PATH.name,
    )
    result: dict[str, Any] = pipeline.run(
        today=today,
        persist=False,
        db_path=BGTEST_DB_PATH,
    )

    raw_top: list[dict[str, Any]] = result.get("raw_top") or []
    top5 = raw_top[:BGTEST_TOP_N]

    log.info("[bgtest 3/6] top-%d by total_score rank:", BGTEST_TOP_N)
    for c in top5:
        sc = c["scoring"]
        log.info(
            "  rank=%-2d  %-6s  total=%5.1f  Q=%5.1f  G=%5.1f  VR=%5.1f  %s",
            c["recommendation_rank"],
            c["symbol"],
            sc["total_score"],
            sc["quality_score"],
            sc["growth_score"],
            sc["value_risk_score"],
            c.get("company_name") or "",
        )

    daily_pick = result.get("daily_pick")
    if daily_pick:
        log.info(
            "  production daily_pick (bgtest cooldown-free): %s (rank=%d)",
            daily_pick["symbol"],
            daily_pick["recommendation_rank"],
        )
    else:
        log.info("  production daily_pick: <none>")

    result["bgtest_top5"] = top5

    if not dry_run:
        log.info("[bgtest 4/6] source discovery for %d candidates ...", len(top5))
        source_discovery.discover(
            candidates=top5,
            run_date=result["run_date"],
            db_path=BGTEST_DB_PATH,
        )

        log.info("[bgtest 5/6] document fetch for %d candidates ...", len(top5))
        fetch_stats = document_fetcher.fetch(
            candidates=top5,
            run_date=result["run_date"],
            db_path=BGTEST_DB_PATH,
        )
        log.info(
            "[fetcher] total=%d  fetched=%d  skipped=%d  failed=%d",
            fetch_stats["total"],
            fetch_stats["fetched"],
            fetch_stats["skipped"],
            fetch_stats["failed"],
        )
        result["fetch_stats"] = fetch_stats

        log.info("[bgtest 6/6] memo generation for %d candidates ...", len(top5))
        memo_stats = memo_generator.generate(
            candidates=top5,
            run_date=result["run_date"],
            db_path=BGTEST_DB_PATH,
        )
        log.info(
            "[memo] total=%d  written=%d  skipped=%d  failed=%d",
            memo_stats["total"],
            memo_stats["written"],
            memo_stats["skipped"],
            memo_stats["failed"],
        )
        result["memo_stats"] = memo_stats

    return result
