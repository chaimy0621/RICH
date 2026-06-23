"""bgtest-specific constants. Never modifies or shadows production config.py."""
from __future__ import annotations

from pathlib import Path

BGTEST_ROOT = Path(__file__).parent
RICH_ROOT   = BGTEST_ROOT.parent   # production tree — treated as read-only

BGTEST_DB_PATH = BGTEST_ROOT / "data" / "bgtest.db"
BGTEST_LOG_DIR = BGTEST_ROOT / "logs"

# Candidates selected by recommendation_rank (total_score rank).
# Never by recommendation_rank_with_valuation (shadow valuation overlay).
BGTEST_TOP_N = 5
