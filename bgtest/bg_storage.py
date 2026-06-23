"""bgtest SQLite storage — wholly isolated from production data/storage.py.

Phase 1: defines the four bgtest table schemas.
Phase 2: adds source_registry schema (status + missing_reason columns, updated
         UNIQUE constraint) and insert_sources() for Phase 2 source discovery writes.

All SQL is confined here; no other bgtest module issues raw SQL.
Tables created here are in bgtest/data/bgtest.db only.
The production data/cache.db is never opened by this module.
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
import sys

_HERE = Path(__file__).parent
if str(_HERE) not in sys.path:
    sys.path.insert(0, str(_HERE))

from config_bgtest import BGTEST_DB_PATH  # noqa: E402

# ---------------------------------------------------------------------------
# Schema — four tables; all idempotent (IF NOT EXISTS).
# ---------------------------------------------------------------------------
_SCHEMA = """
CREATE TABLE IF NOT EXISTS source_registry (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date           TEXT    NOT NULL,
    symbol             TEXT    NOT NULL,
    rank_at_discovery  INTEGER,
    source_type        TEXT    NOT NULL,
    source_url         TEXT    NOT NULL,       -- real URL, or sentinel 'MISSING'
    source_domain      TEXT,
    allowed            INTEGER NOT NULL DEFAULT 1,
    status             TEXT    NOT NULL DEFAULT 'DISCOVERED',
    missing_reason     TEXT,                   -- populated for MISSING / FETCH_FAILED only
    discovery_ts       TEXT    NOT NULL,
    UNIQUE(run_date, symbol, source_type, source_url)
);

CREATE TABLE IF NOT EXISTS document_fetch_log (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date       TEXT    NOT NULL,
    symbol         TEXT    NOT NULL,
    source_url     TEXT    NOT NULL,
    source_type    TEXT    NOT NULL,
    fetch_ts       TEXT    NOT NULL,
    http_status    INTEGER,
    fetch_success  INTEGER NOT NULL,
    failure_reason TEXT,
    content_bytes  INTEGER,
    content_hash   TEXT
);

CREATE TABLE IF NOT EXISTS background_docs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date          TEXT    NOT NULL,
    symbol            TEXT    NOT NULL,
    source_url        TEXT    NOT NULL,
    source_type       TEXT    NOT NULL,
    document_date     TEXT,
    document_title    TEXT,
    fetch_ts          TEXT    NOT NULL,
    content_hash      TEXT,
    content_text      TEXT,
    content_raw_bytes BLOB,
    is_missing        INTEGER NOT NULL DEFAULT 0,
    missing_reason    TEXT,
    UNIQUE(run_date, symbol, source_url)
);

CREATE TABLE IF NOT EXISTS background_review_memo (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    run_date        TEXT    NOT NULL,
    symbol          TEXT    NOT NULL,
    rank_at_review  INTEGER,
    pass_fail       TEXT    NOT NULL,
    memo_text       TEXT    NOT NULL,
    docs_found      INTEGER NOT NULL,
    docs_missing    INTEGER NOT NULL,
    sources_checked TEXT    NOT NULL,
    review_ts       TEXT    NOT NULL,
    memo_version    TEXT    NOT NULL DEFAULT 'v1',
    UNIQUE(run_date, symbol)
);
"""


@contextmanager
def connect(path: Path = BGTEST_DB_PATH):
    """Open bgtest.db, commit on success, close always."""
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        yield conn
        conn.commit()
    finally:
        conn.close()


# Defensive migrations — run after schema creation to handle existing tables
# that were created by an earlier schema version (e.g. Phase 1 without status).
# Each statement is tried independently; "already exists" errors are silently ignored.
_BGTEST_MIGRATIONS = [
    # Phase 2 — source_registry columns added after initial scaffold
    "ALTER TABLE source_registry ADD COLUMN status TEXT NOT NULL DEFAULT 'DISCOVERED'",
    "ALTER TABLE source_registry ADD COLUMN missing_reason TEXT",
    # Phase 3 — ensure document_fetch_log is idempotent across re-runs
    "CREATE UNIQUE INDEX IF NOT EXISTS ux_fetch_log ON document_fetch_log(run_date, symbol, source_url)",
]


def _run_migrations(path: Path) -> None:
    with connect(path) as c:
        for sql in _BGTEST_MIGRATIONS:
            try:
                c.execute(sql)
            except Exception:
                pass  # column already exists


def init_db(path: Path = BGTEST_DB_PATH) -> None:
    """Create bgtest-specific tables and run migrations. Idempotent."""
    with connect(path) as c:
        c.executescript(_SCHEMA)
    _run_migrations(path)


# ---------------------------------------------------------------------------
# Phase 2 — source_registry writes
# ---------------------------------------------------------------------------

_SOURCE_REGISTRY_COLUMNS = [
    "run_date", "symbol", "rank_at_discovery", "source_type",
    "source_url", "source_domain", "allowed", "status",
    "missing_reason", "discovery_ts",
]


def insert_sources(rows: list[dict], path: Path = BGTEST_DB_PATH) -> int:
    """Batch-insert source_registry rows. INSERT OR IGNORE — re-runnable.

    Returns the number of rows passed in (not the SQLite rowcount, which
    under OR IGNORE reflects only newly inserted rows).
    """
    if not rows:
        return 0
    cols  = ", ".join(_SOURCE_REGISTRY_COLUMNS)
    phlds = ", ".join(f":{c}" for c in _SOURCE_REGISTRY_COLUMNS)
    sql   = f"INSERT OR IGNORE INTO source_registry ({cols}) VALUES ({phlds})"
    with connect(path) as c:
        c.executemany(sql, rows)
    return len(rows)


# ---------------------------------------------------------------------------
# Phase 3 — document_fetch_log and background_docs
# ---------------------------------------------------------------------------

def get_fetch_candidates(
    run_date: str,
    symbols: list[str],
    path: Path = BGTEST_DB_PATH,
) -> list[dict]:
    """Return source_registry rows eligible for Phase 3 document fetching.

    Filters to allowed, DISCOVERED EDGAR filing URLs for the given symbols
    and run_date.  Ordered by rank then source_type to give consistent runs.
    """
    if not symbols:
        return []
    ph  = ",".join("?" * len(symbols))
    sql = f"""
        SELECT symbol, source_url, source_type, rank_at_discovery
        FROM   source_registry
        WHERE  run_date    = ?
          AND  symbol      IN ({ph})
          AND  source_type IN ('SEC_EDGAR_10K', 'SEC_EDGAR_10Q', 'SEC_EDGAR_20F')
          AND  allowed     = 1
          AND  status      = 'DISCOVERED'
        ORDER  BY rank_at_discovery, source_type, source_url
    """
    with connect(path) as c:
        rows = c.execute(sql, [run_date, *symbols]).fetchall()
    return [dict(r) for r in rows]


def get_fetched_urls(
    run_date: str,
    symbols: list[str],
    path: Path = BGTEST_DB_PATH,
) -> set[str]:
    """Return source_urls already successfully stored in background_docs.

    Used as a pre-flight idempotency check: URLs in this set are skipped
    without any network call.
    """
    if not symbols:
        return set()
    ph  = ",".join("?" * len(symbols))
    sql = f"""
        SELECT source_url
        FROM   background_docs
        WHERE  run_date  = ?
          AND  symbol    IN ({ph})
          AND  is_missing = 0
    """
    with connect(path) as c:
        rows = c.execute(sql, [run_date, *symbols]).fetchall()
    return {r["source_url"] for r in rows}


_FETCH_LOG_COLUMNS = [
    "run_date", "symbol", "source_url", "source_type",
    "fetch_ts", "http_status", "fetch_success", "failure_reason",
    "content_bytes", "content_hash",
]


def insert_fetch_log(row: dict, path: Path = BGTEST_DB_PATH) -> None:
    """INSERT OR IGNORE one row into document_fetch_log.

    The UNIQUE index on (run_date, symbol, source_url) (added by Phase 3
    migration) ensures re-runs produce no duplicate log entries.
    """
    cols  = ", ".join(_FETCH_LOG_COLUMNS)
    phlds = ", ".join(f":{c}" for c in _FETCH_LOG_COLUMNS)
    sql   = f"INSERT OR IGNORE INTO document_fetch_log ({cols}) VALUES ({phlds})"
    with connect(path) as c:
        c.execute(sql, row)


_BACKGROUND_DOC_COLUMNS = [
    "run_date", "symbol", "source_url", "source_type",
    "document_date", "document_title", "fetch_ts",
    "content_hash", "content_text", "content_raw_bytes",
    "is_missing", "missing_reason",
]


def upsert_doc(row: dict, path: Path = BGTEST_DB_PATH) -> None:
    """INSERT OR IGNORE one row into background_docs.

    The UNIQUE constraint on (run_date, symbol, source_url) ensures
    idempotency.  The first write (success or failure) is permanent;
    re-runs that reach the same URL are silently ignored.
    """
    cols  = ", ".join(_BACKGROUND_DOC_COLUMNS)
    phlds = ", ".join(f":{c}" for c in _BACKGROUND_DOC_COLUMNS)
    sql   = f"INSERT OR IGNORE INTO background_docs ({cols}) VALUES ({phlds})"
    with connect(path) as c:
        c.execute(sql, row)


# ---------------------------------------------------------------------------
# Phase 4 — background_review_memo reads and writes
# ---------------------------------------------------------------------------

def get_docs_for_symbol(
    run_date: str,
    symbol: str,
    path: Path = BGTEST_DB_PATH,
) -> list[dict]:
    """Return all background_docs rows for a symbol and run_date.

    Ordered by source_type then document_date descending (most recent first
    within each type) so callers can trivially pick the latest 10-K.
    content_text is included (may be large — caller is responsible for use).
    """
    sql = """
        SELECT symbol, source_url, source_type, document_date,
               document_title, is_missing, missing_reason, content_text
        FROM   background_docs
        WHERE  run_date = ? AND symbol = ?
        ORDER  BY source_type, document_date DESC
    """
    with connect(path) as c:
        rows = c.execute(sql, (run_date, symbol)).fetchall()
    return [dict(r) for r in rows]


def get_missing_sources(
    run_date: str,
    symbol: str,
    path: Path = BGTEST_DB_PATH,
) -> list[dict]:
    """Return source_registry rows with status='MISSING' for a symbol."""
    sql = """
        SELECT source_type, source_url, missing_reason
        FROM   source_registry
        WHERE  run_date = ? AND symbol = ? AND status = 'MISSING'
    """
    with connect(path) as c:
        rows = c.execute(sql, (run_date, symbol)).fetchall()
    return [dict(r) for r in rows]


def get_existing_memos(
    run_date: str,
    symbols: list[str],
    path: Path = BGTEST_DB_PATH,
) -> set[str]:
    """Return set of symbols that already have a memo for this run_date."""
    if not symbols:
        return set()
    ph  = ",".join("?" * len(symbols))
    sql = f"""
        SELECT symbol FROM background_review_memo
        WHERE  run_date = ? AND symbol IN ({ph})
    """
    with connect(path) as c:
        rows = c.execute(sql, [run_date, *symbols]).fetchall()
    return {r["symbol"] for r in rows}


_MEMO_COLUMNS = [
    "run_date", "symbol", "rank_at_review", "pass_fail", "memo_text",
    "docs_found", "docs_missing", "sources_checked", "review_ts", "memo_version",
]


def insert_memo(row: dict, path: Path = BGTEST_DB_PATH) -> None:
    """INSERT OR IGNORE one row into background_review_memo.

    UNIQUE(run_date, symbol) ensures idempotency across re-runs.
    """
    cols  = ", ".join(_MEMO_COLUMNS)
    phlds = ", ".join(f":{c}" for c in _MEMO_COLUMNS)
    sql   = f"INSERT OR IGNORE INTO background_review_memo ({cols}) VALUES ({phlds})"
    with connect(path) as c:
        c.execute(sql, row)
