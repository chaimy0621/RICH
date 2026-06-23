"""SQLite storage for daily picks. Single source of truth for cooldown lookups."""
from __future__ import annotations

import logging
import sqlite3
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

from config import CACHE_DB

log = logging.getLogger(__name__)

SCHEMA_RESEARCH = """
CREATE TABLE IF NOT EXISTS research_universe (
    run_date         TEXT NOT NULL,
    symbol           TEXT NOT NULL,

    -- Cross-sectional z-score results (WEIGHTS_V2)
    total_score       REAL,
    quality_score     REAL,
    growth_score      REAL,
    value_risk_score  REAL,
    universe_n        INTEGER,  -- # candidates in the cross-section this run
    universe_rank     INTEGER,  -- rank among ALL scored candidates (1 = best)
    in_top20          INTEGER NOT NULL DEFAULT 0,  -- 1 if inside RAW_TOP cutoff

    -- Screener audit
    historical_years_available INTEGER,
    m_score       REAL,
    beneish_flag  INTEGER,

    -- Quality sleeve factors
    returnOnEquity               REAL,
    returnOnAssets               REAL,
    grossProfitMargin            REAL,
    operatingProfitMargin        REAL,
    operatingCashFlowToNetIncome REAL,
    f_score                      REAL,
    novy_marx_gp_assets          REAL,

    -- Growth sleeve factors
    revenueGrowth    REAL,
    revenue_cagr_5y  REAL,
    eps_cagr_5y      REAL,
    fcf_cagr_5y      REAL,
    asset_growth     REAL,

    -- Value/Risk sleeve factors
    debtToEquityRatio  REAL,
    priceEarningsRatio REAL,
    ev_ebitda          REAL,
    ps_ratio           REAL,
    pe_vs_own_history  REAL,

    -- Management alignment (zero extra API calls; sourced from key_metrics cache)
    sbc_to_revenue REAL,

    -- P0: sector/industry plumbing (company_screener sourced)
    sector   TEXT,
    industry TEXT,

    -- P1 (§7): data-vintage / lookahead audit (shadow)
    data_vintage_days  INTEGER,
    latest_filing_date TEXT,
    stale_data_flag    INTEGER NOT NULL DEFAULT 0,

    -- P4 (§4): revenue one-time-spike protection (shadow)
    revenue_consistency_flag INTEGER NOT NULL DEFAULT 0,
    revenue_spike_ratio      REAL,
    growth_score_spike_adj   REAL,

    -- Audit / reproducibility
    rule_version         TEXT,     -- scoring weight set in use, e.g. "WEIGHTS_V2"
    normalization_method TEXT,     -- normalization approach, e.g. "zscore_v2"
    is_backfill          INTEGER NOT NULL DEFAULT 0,  -- 1 when row was written via rebuild path

    PRIMARY KEY (run_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_ru_date
    ON research_universe(run_date);
CREATE INDEX IF NOT EXISTS idx_ru_rank
    ON research_universe(run_date, universe_rank);
"""

COLUMNS_RESEARCH = [
    "run_date", "symbol",
    "total_score", "quality_score", "growth_score", "value_risk_score",
    "universe_n", "universe_rank", "in_top20",
    "historical_years_available", "m_score", "beneish_flag",
    # Quality
    "returnOnEquity", "returnOnAssets", "grossProfitMargin",
    "operatingProfitMargin", "operatingCashFlowToNetIncome",
    "f_score", "novy_marx_gp_assets",
    # Growth
    "revenueGrowth", "revenue_cagr_5y", "eps_cagr_5y", "fcf_cagr_5y", "asset_growth",
    # Value/Risk
    "debtToEquityRatio", "priceEarningsRatio", "ev_ebitda", "ps_ratio", "pe_vs_own_history",
    # Management alignment
    "sbc_to_revenue",
    # P0: sector/industry plumbing
    "sector", "industry",
    # P1 (§7): data-vintage / lookahead audit (shadow)
    "data_vintage_days", "latest_filing_date", "stale_data_flag",
    # P4 (§4): revenue one-time-spike protection (shadow)
    "revenue_consistency_flag", "revenue_spike_ratio", "growth_score_spike_adj",
    # Audit / reproducibility
    "rule_version", "normalization_method", "is_backfill",
]

SCHEMA = """
CREATE TABLE IF NOT EXISTS daily_picks (
    run_date TEXT NOT NULL,
    symbol TEXT NOT NULL,
    company_name TEXT,
    -- P0: sector/industry plumbing (company_screener / profile sourced)
    sector TEXT,
    industry TEXT,
    total_score REAL,
    quality_score REAL,
    growth_score REAL,
    value_risk_score REAL,
    recommendation_rank INTEGER,
    is_daily_pick INTEGER NOT NULL DEFAULT 0,
    eligible_for_pick INTEGER NOT NULL DEFAULT 1,
    in_cooldown INTEGER NOT NULL DEFAULT 0,
    cooldown_until TEXT,
    historical_years_available INTEGER,
    top_drivers TEXT,
    bottom_drivers TEXT,
    short_reason TEXT,
    sector_median_pe       REAL,
    fair_value_pe          REAL,
    pe_vs_sector           REAL,
    valuation_adj          REAL,
    valuation_label        TEXT,
    valuation_model_version TEXT,
    total_score_adj                    REAL,
    recommendation_rank_with_valuation INTEGER,
    m_score                            REAL,
    beneish_flag                       INTEGER,
    f_score                            INTEGER,
    novy_marx_gp_assets                REAL,
    -- Growth shadow factors (Phase 3.2; not yet in scoring)
    revenue_cagr_5y  REAL,
    eps_cagr_5y      REAL,
    fcf_cagr_5y      REAL,
    asset_growth     REAL,
    -- Value/Risk shadow factors (Phase 3.3; not yet in scoring)
    ev_ebitda        REAL,
    ps_ratio         REAL,
    peg_ratio        REAL,
    pe_vs_own_history REAL,
    -- Taleb Phase 1: fragility / hidden asymmetry shadow signals
    altman_z_score       REAL,
    interest_coverage    REAL,
    fcf_negative_years_5 INTEGER,
    -- Taleb Phase 2: ruin risk composite (shadow; path to VR sleeve)
    ruin_risk_score REAL,
    -- Taleb Phase 3: longevity / Lindy shadow factors
    revenue_cagr_10y REAL,
    eps_cagr_10y     REAL,
    fcf_cagr_10y     REAL,
    lindy_score      REAL,
    -- Taleb Phase 4: management alignment shadow signals
    sbc_to_revenue        REAL,
    insider_net_shares_3m REAL,
    ceo_pay_ratio         REAL,
    -- Phase 4 diagnostics: V1 linear scores + scoring metadata
    total_score_v1       REAL,
    quality_score_v1     REAL,
    growth_score_v1      REAL,
    value_risk_score_v1  REAL,
    normalization_method TEXT,
    universe_n_at_scoring INTEGER,
    -- Phase 6 performance tracking
    pick_price REAL,
    -- Overlay candidate tracking (shadow analysis; populated on is_daily_pick=1 rows only)
    overlay_candidate_sym   TEXT,
    overlay_candidate_price REAL,
    -- Backfill flag
    is_backfill INTEGER DEFAULT 0,
    -- Portfolio awareness: 1 when symbol is a current Moomoo US holding.
    -- held_reject_code explains a NULL-score held row (failed screen / outside universe).
    is_held INTEGER DEFAULT 0,
    held_reject_code TEXT,
    -- P1 (§7): data-vintage / lookahead audit (shadow)
    data_vintage_days   INTEGER,
    latest_filing_date  TEXT,
    stale_data_flag     INTEGER DEFAULT 0,
    -- §6: sector-concentration guardrail (warn only; daily_picks only)
    sector_concentration_warning INTEGER DEFAULT 0,
    -- P4 (§4): revenue one-time-spike protection (shadow)
    revenue_consistency_flag INTEGER DEFAULT 0,
    revenue_spike_ratio      REAL,
    growth_score_spike_adj   REAL,
    PRIMARY KEY (run_date, symbol)
);

CREATE INDEX IF NOT EXISTS idx_picks_lookup
    ON daily_picks(symbol, is_daily_pick, run_date);
"""

COLUMNS = [
    "run_date", "symbol", "company_name",
    # P0: sector/industry plumbing
    "sector", "industry",
    "total_score", "quality_score", "growth_score", "value_risk_score",
    "recommendation_rank", "is_daily_pick", "eligible_for_pick",
    "in_cooldown", "cooldown_until", "historical_years_available",
    "top_drivers", "bottom_drivers", "short_reason",
    # valuation overlay (shadow mode; not used for ranking)
    "sector_median_pe", "fair_value_pe", "pe_vs_sector",
    "valuation_adj", "valuation_label", "valuation_model_version",
    # dual ranking (observation only)
    "total_score_adj", "recommendation_rank_with_valuation",
    # Beneish M-Score gate (Phase 2)
    "m_score", "beneish_flag",
    # Piotroski F-Score + Novy-Marx GP/Assets (Phase 3 Quality, shadow)
    "f_score", "novy_marx_gp_assets",
    # Growth shadow factors (Phase 3.2)
    "revenue_cagr_5y", "eps_cagr_5y", "fcf_cagr_5y", "asset_growth",
    # Value/Risk shadow factors (Phase 3.3)
    "ev_ebitda", "ps_ratio", "peg_ratio", "pe_vs_own_history",
    # Taleb Phase 1: fragility / hidden asymmetry shadow signals
    "altman_z_score", "interest_coverage", "fcf_negative_years_5",
    # Taleb Phase 2: ruin risk composite (shadow; path to VR sleeve)
    "ruin_risk_score",
    # Taleb Phase 3: longevity / Lindy shadow factors
    "revenue_cagr_10y", "eps_cagr_10y", "fcf_cagr_10y", "lindy_score",
    # Taleb Phase 4: management alignment shadow signals
    "sbc_to_revenue", "insider_net_shares_3m", "ceo_pay_ratio",
    # Phase 4 diagnostics
    "total_score_v1", "quality_score_v1", "growth_score_v1", "value_risk_score_v1",
    "normalization_method", "universe_n_at_scoring",
    # Phase 6 performance tracking
    "pick_price",
    # Overlay candidate tracking (shadow analysis)
    "overlay_candidate_sym",
    "overlay_candidate_price",
    # Backfill flag
    "is_backfill",
    # Portfolio awareness (Moomoo holdings sync)
    "is_held", "held_reject_code",
    # P1 (§7): data-vintage / lookahead audit (shadow)
    "data_vintage_days", "latest_filing_date", "stale_data_flag",
    # §6: sector-concentration guardrail (daily_picks only)
    "sector_concentration_warning",
    # P4 (§4): revenue one-time-spike protection (shadow)
    "revenue_consistency_flag", "revenue_spike_ratio", "growth_score_spike_adj",
]


@contextmanager
def connect(path: Path = CACHE_DB):
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


_MIGRATIONS = [
    # research_universe additions (safe to run against DBs created before these columns existed)
    "ALTER TABLE research_universe ADD COLUMN rule_version TEXT",
    "ALTER TABLE research_universe ADD COLUMN normalization_method TEXT",
    "ALTER TABLE research_universe ADD COLUMN is_backfill INTEGER NOT NULL DEFAULT 0",
    # Phase 2
    "ALTER TABLE daily_picks ADD COLUMN m_score REAL",
    "ALTER TABLE daily_picks ADD COLUMN beneish_flag INTEGER",
    # Phase 3 Quality
    "ALTER TABLE daily_picks ADD COLUMN f_score INTEGER",
    "ALTER TABLE daily_picks ADD COLUMN novy_marx_gp_assets REAL",
    # Phase 3.2 Growth shadow
    "ALTER TABLE daily_picks ADD COLUMN revenue_cagr_5y REAL",
    "ALTER TABLE daily_picks ADD COLUMN eps_cagr_5y REAL",
    "ALTER TABLE daily_picks ADD COLUMN fcf_cagr_5y REAL",
    "ALTER TABLE daily_picks ADD COLUMN asset_growth REAL",
    # Phase 3.3 Value/Risk shadow
    "ALTER TABLE daily_picks ADD COLUMN ev_ebitda REAL",
    "ALTER TABLE daily_picks ADD COLUMN ps_ratio REAL",
    "ALTER TABLE daily_picks ADD COLUMN peg_ratio REAL",
    "ALTER TABLE daily_picks ADD COLUMN pe_vs_own_history REAL",
    # Phase 4 diagnostics
    "ALTER TABLE daily_picks ADD COLUMN total_score_v1 REAL",
    "ALTER TABLE daily_picks ADD COLUMN quality_score_v1 REAL",
    "ALTER TABLE daily_picks ADD COLUMN growth_score_v1 REAL",
    "ALTER TABLE daily_picks ADD COLUMN value_risk_score_v1 REAL",
    "ALTER TABLE daily_picks ADD COLUMN normalization_method TEXT",
    "ALTER TABLE daily_picks ADD COLUMN universe_n_at_scoring INTEGER",
    # Phase 6 performance tracking
    "ALTER TABLE daily_picks ADD COLUMN pick_price REAL",
    # Taleb Phase 1: fragility / hidden asymmetry shadow signals
    "ALTER TABLE daily_picks ADD COLUMN altman_z_score REAL",
    "ALTER TABLE daily_picks ADD COLUMN interest_coverage REAL",
    "ALTER TABLE daily_picks ADD COLUMN fcf_negative_years_5 INTEGER",
    # Taleb Phase 2: ruin risk composite (shadow; path to VR sleeve)
    "ALTER TABLE daily_picks ADD COLUMN ruin_risk_score REAL",
    # Taleb Phase 3: longevity / Lindy shadow factors
    "ALTER TABLE daily_picks ADD COLUMN revenue_cagr_10y REAL",
    "ALTER TABLE daily_picks ADD COLUMN eps_cagr_10y REAL",
    "ALTER TABLE daily_picks ADD COLUMN fcf_cagr_10y REAL",
    "ALTER TABLE daily_picks ADD COLUMN lindy_score REAL",
    # Taleb Phase 4: management alignment shadow signals
    "ALTER TABLE daily_picks ADD COLUMN sbc_to_revenue REAL",
    "ALTER TABLE daily_picks ADD COLUMN insider_net_shares_3m REAL",
    "ALTER TABLE daily_picks ADD COLUMN ceo_pay_ratio REAL",
    # Overlay candidate tracking (shadow analysis)
    "ALTER TABLE daily_picks ADD COLUMN overlay_candidate_sym TEXT",
    "ALTER TABLE daily_picks ADD COLUMN overlay_candidate_price REAL",
    # Backfill flag
    "ALTER TABLE daily_picks ADD COLUMN is_backfill INTEGER DEFAULT 0",
    # Portfolio awareness (is_held feature; safe on existing DBs)
    "ALTER TABLE daily_picks ADD COLUMN is_held INTEGER DEFAULT 0",
    "ALTER TABLE daily_picks ADD COLUMN held_reject_code TEXT",
    # P0: sector/industry plumbing
    "ALTER TABLE daily_picks ADD COLUMN sector TEXT",
    "ALTER TABLE daily_picks ADD COLUMN industry TEXT",
    "ALTER TABLE research_universe ADD COLUMN sector TEXT",
    "ALTER TABLE research_universe ADD COLUMN industry TEXT",
    # P1 (§7): data-vintage / lookahead audit (shadow)
    "ALTER TABLE daily_picks ADD COLUMN data_vintage_days INTEGER",
    "ALTER TABLE daily_picks ADD COLUMN latest_filing_date TEXT",
    "ALTER TABLE daily_picks ADD COLUMN stale_data_flag INTEGER DEFAULT 0",
    "ALTER TABLE research_universe ADD COLUMN data_vintage_days INTEGER",
    "ALTER TABLE research_universe ADD COLUMN latest_filing_date TEXT",
    "ALTER TABLE research_universe ADD COLUMN stale_data_flag INTEGER DEFAULT 0",
    # §6: sector-concentration guardrail (daily_picks only — NOT research_universe)
    "ALTER TABLE daily_picks ADD COLUMN sector_concentration_warning INTEGER DEFAULT 0",
    # P4 (§4): revenue one-time-spike protection (shadow) — both tables
    "ALTER TABLE daily_picks ADD COLUMN revenue_consistency_flag INTEGER DEFAULT 0",
    "ALTER TABLE daily_picks ADD COLUMN revenue_spike_ratio REAL",
    "ALTER TABLE daily_picks ADD COLUMN growth_score_spike_adj REAL",
    "ALTER TABLE research_universe ADD COLUMN revenue_consistency_flag INTEGER DEFAULT 0",
    "ALTER TABLE research_universe ADD COLUMN revenue_spike_ratio REAL",
    "ALTER TABLE research_universe ADD COLUMN growth_score_spike_adj REAL",
]


def _migrate_db(path: Path) -> None:
    """Add new columns to an existing DB without dropping data."""
    with connect(path) as c:
        for sql in _MIGRATIONS:
            try:
                c.execute(sql)
            except Exception:
                pass  # column already exists


def init_db(path: Path = CACHE_DB) -> None:
    with connect(path) as c:
        c.executescript(SCHEMA)
        c.executescript(SCHEMA_RESEARCH)
    _migrate_db(path)


def insert_picks(rows: list[dict], path: Path = CACHE_DB) -> int:
    placeholders = ",".join("?" * len(COLUMNS))
    sql = (
        f"INSERT OR REPLACE INTO daily_picks ({','.join(COLUMNS)}) "
        f"VALUES ({placeholders})"
    )
    with connect(path) as c:
        c.executemany(sql, [tuple(r.get(k) for k in COLUMNS) for r in rows])
    return len(rows)


def insert_research_universe(rows: list[dict], path: Path = CACHE_DB) -> int:
    """Append full scored universe rows for one run_date.

    Uses INSERT OR IGNORE — first write wins; reruns of the same date are no-ops.
    All rows in `rows` must share the same run_date. Never mix run_dates in one batch.
    """
    placeholders = ",".join("?" * len(COLUMNS_RESEARCH))
    sql = (
        f"INSERT OR IGNORE INTO research_universe ({','.join(COLUMNS_RESEARCH)}) "
        f"VALUES ({placeholders})"
    )
    with connect(path) as c:
        c.executemany(sql, [tuple(r.get(k) for k in COLUMNS_RESEARCH) for r in rows])
    return len(rows)


def rebuild_research_universe(
    rows: list[dict], run_date: str, path: Path = CACHE_DB
) -> int:
    """Delete all rows for run_date and reinsert atomically. Maintenance path only.

    Use when correcting a previously stored run or backfilling with an updated
    scoring version. Caller must set is_backfill=1 in each row to mark the
    provenance change. DELETE + INSERT execute in one transaction; a crash
    leaves either the old data intact or the new data fully written.
    """
    placeholders = ",".join("?" * len(COLUMNS_RESEARCH))
    insert_sql = (
        f"INSERT INTO research_universe ({','.join(COLUMNS_RESEARCH)}) "
        f"VALUES ({placeholders})"
    )
    with connect(path) as c:
        deleted = c.execute(
            "DELETE FROM research_universe WHERE run_date = ?", (run_date,)
        ).rowcount
        c.executemany(insert_sql, [tuple(r.get(k) for k in COLUMNS_RESEARCH) for r in rows])
    return deleted


def cooldown_map(today: date, cooldown_days: int,
                 path: Path = CACHE_DB) -> dict[str, str]:
    """Return {symbol: cooldown_until_iso} for symbols still in cooldown today."""
    cutoff = (today - timedelta(days=cooldown_days)).isoformat()
    with connect(path) as c:
        rows = c.execute(
            "SELECT symbol, MAX(run_date) AS last_pick FROM daily_picks "
            "WHERE is_daily_pick=1 AND run_date > ? GROUP BY symbol",
            (cutoff,),
        ).fetchall()
    out: dict[str, str] = {}
    for r in rows:
        last = date.fromisoformat(r["last_pick"])
        out[r["symbol"]] = (last + timedelta(days=cooldown_days)).isoformat()
    return out


def fetch_run(run_date: str, path: Path = CACHE_DB) -> list[dict]:
    with connect(path) as c:
        rows = c.execute(
            "SELECT * FROM daily_picks WHERE run_date=? ORDER BY recommendation_rank",
            (run_date,),
        ).fetchall()
    return [dict(r) for r in rows]


def held_us_symbols(db_path: Path = CACHE_DB) -> frozenset[str]:
    """Return the set of US symbols held as of the latest Moomoo snapshot.

    Reads the most recent snapshot from ``moomoo_position_snapshots`` (written by
    the standalone ``sync_moomoo.py`` utility), filtered to ``trd_market='US'``.
    Returns an empty frozenset if the table is absent or empty — the picker never
    depends on Moomoo data being present (graceful degradation). Emits a WARNING
    when the latest snapshot is more than 1 day old, plus an INFO line recording
    the set size and snapshot provenance (date + age).
    """
    try:
        with connect(db_path) as c:
            rows = c.execute(
                "SELECT symbol, snapshot_date FROM moomoo_position_snapshots "
                "WHERE trd_market = 'US' AND snapshot_ts = ("
                "SELECT MAX(snapshot_ts) FROM moomoo_position_snapshots "
                "WHERE trd_market = 'US')"
            ).fetchall()
    except sqlite3.OperationalError:
        return frozenset()  # table not created yet — no sync has run

    if not rows:
        return frozenset()

    symbols = frozenset(r["symbol"] for r in rows if r["symbol"])
    snap_date = rows[0]["snapshot_date"]
    age_days = None
    if snap_date:
        try:
            age_days = (date.today() - date.fromisoformat(snap_date)).days
        except ValueError:
            age_days = None

    if age_days is not None and age_days > 1:
        log.warning("Moomoo holdings snapshot is stale: dated %s (+%dd ago)",
                    snap_date, age_days)
    log.info("Held set: %d symbols from snapshot dated %s (+%sd ago)",
             len(symbols), snap_date,
             age_days if age_days is not None else "?")
    return symbols
