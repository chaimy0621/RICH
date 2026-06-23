"""SEC EDGAR and company IR source discovery for bgtest top-5 candidates.

Phase 2 scope
-------------
  * CIK resolution via SEC company_tickers.json (one request per run)
  * EDGAR submissions metadata (one request per ticker)
  * Recent 10-K and 10-Q filing URL extraction (no content download)
  * Conservative IR URL discovery — SEC investorWebsite field first,
    then a limited set of subdomain / path patterns (HEAD only)

Not in scope (later phases)
---------------------------
  * Document content fetching
  * Earnings transcript discovery
  * Broad web crawling or scraping

Selection basis
---------------
  Candidates are passed in by bg_orchestrator, which selects them by
  recommendation_rank (total_score rank). This module never re-ranks or
  uses valuation overlay data.

Failure policy
--------------
  Every expected source that cannot be resolved is recorded as an explicit
  MISSING or FETCH_FAILED row in source_registry. No silent substitution.
  The run continues even if one ticker's discovery fails entirely.
"""
from __future__ import annotations

import json
import logging
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

# Path setup — self-sufficient whether imported or run directly.
_HERE      = Path(__file__).parent
_RICH_ROOT = _HERE.parent
for _p in (str(_RICH_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config_bgtest import BGTEST_DB_PATH  # noqa: E402
import bg_storage                          # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# EDGAR endpoints and rate-limit constants
# ---------------------------------------------------------------------------
_COMPANY_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
_SUBMISSIONS_BASE    = "https://data.sec.gov/submissions"
_ARCHIVES_BASE       = "https://www.sec.gov/Archives/edgar/data"
_USER_AGENT          = "RICH-bgtest chai.my621@gmail.com"
_EDGAR_TIMEOUT       = (10, 30)   # (connect_s, read_s)
_HEAD_TIMEOUT        = (5, 10)
_RATE_DELAY          = 0.12       # seconds; SEC enforces <= 10 req/s

# Filing types to discover and per-run caps (submissions are newest-first)
_TARGET_FORMS: frozenset[str] = frozenset({"10-K", "10-Q"})
_FORM_CAPS: dict[str, int]    = {"10-K": 3, "10-Q": 4}

# IR pattern tiers tried when investorWebsite is absent (tried in order)
_IR_SUBDOMAINS = ("investor", "ir", "investors")
_IR_PATHS      = ("/investor-relations", "/investors", "/ir")

_ALLOWED_FILE = _HERE / "allowed_sources.json"


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def discover(
    candidates: list[dict[str, Any]],
    run_date: str,
    db_path: Path = BGTEST_DB_PATH,
) -> list[dict]:
    """Discover SEC and IR sources for each candidate; persist to source_registry.

    Args:
        candidates : top-N candidate dicts from pipeline (must have 'symbol'
                     and 'recommendation_rank').
        run_date   : YYYY-MM-DD string from pipeline result.
        db_path    : bgtest DB path (never production DB).

    Returns:
        All rows written to source_registry (DISCOVERED + MISSING + FETCH_FAILED).
    """
    allowed_cfg = _load_allowed_config()

    log.info("[discovery] fetching EDGAR company_tickers.json ...")
    tickers_map, map_ok = _fetch_tickers_map()
    if map_ok:
        log.info("[discovery] tickers map loaded (%d entries)", len(tickers_map))
    else:
        log.warning("[discovery] tickers map unavailable — all tickers get MISSING CIK")

    all_rows: list[dict] = []
    for c in candidates:
        sym  = c["symbol"]
        rank = c["recommendation_rank"]
        log.info("[discovery] --- %s (rank=%d) ---", sym, rank)
        rows = _discover_one(sym, rank, run_date, tickers_map, map_ok, allowed_cfg)
        _log_status_breakdown(sym, rows)
        all_rows.extend(rows)
        time.sleep(_RATE_DELAY)   # polite pause between tickers

    n = bg_storage.insert_sources(all_rows, path=db_path)
    log.info("[discovery] inserted %d rows into source_registry", n)
    return all_rows


# ---------------------------------------------------------------------------
# Per-ticker orchestration
# ---------------------------------------------------------------------------

def _discover_one(
    symbol: str,
    rank: int,
    run_date: str,
    tickers_map: dict,
    map_ok: bool,
    allowed_cfg: dict,
) -> list[dict]:
    now = _now_iso()

    # ---- step B: CIK resolution -----------------------------------------
    if not map_ok:
        return [_missing(run_date, symbol, rank,
                         "SEC_EDGAR_CIK", "TICKERS_MAP_UNAVAILABLE", now)]

    entry = tickers_map.get(symbol.upper())
    if not entry:
        log.warning("[discovery] %s: CIK not found in company_tickers.json", symbol)
        return [_missing(run_date, symbol, rank,
                         "SEC_EDGAR_CIK", "CIK_NOT_FOUND", now)]

    cik_int = int(entry["cik_str"])
    cik_pad = f"{cik_int:010d}"
    log.info("[discovery] %s: CIK=%s (%s)", symbol, cik_pad, entry.get("title", ""))

    # ---- step C: submissions fetch ---------------------------------------
    sub_url = f"{_SUBMISSIONS_BASE}/CIK{cik_pad}.json"
    time.sleep(_RATE_DELAY)
    submissions = _fetch_json(sub_url)
    if submissions is None:
        return [_missing(run_date, symbol, rank,
                         "SEC_EDGAR_SUBMISSIONS", "FETCH_FAILED", now)]

    rows: list[dict] = []

    # Record the submissions metadata URL as a source in its own right
    rows.append(_source(run_date, symbol, rank,
                        "SEC_EDGAR_SUBMISSIONS", sub_url, allowed_cfg, now))

    # ---- step D: filing URL extraction ----------------------------------
    rows.extend(
        _filing_urls(run_date, symbol, rank, cik_int, submissions, allowed_cfg, now)
    )

    # ---- step E: IR URL discovery ----------------------------------------
    rows.extend(
        _discover_ir(run_date, symbol, rank, submissions, allowed_cfg, now)
    )

    return rows


# ---------------------------------------------------------------------------
# Filing URL extraction  (step D — no network calls)
# ---------------------------------------------------------------------------

def _filing_urls(
    run_date: str,
    symbol: str,
    rank: int,
    cik: int,
    submissions: dict,
    allowed_cfg: dict,
    ts: str,
) -> list[dict]:
    rows: list[dict] = []
    recent       = (submissions.get("filings") or {}).get("recent") or {}
    accessions   = recent.get("accessionNumber") or []
    forms        = recent.get("form") or []
    dates        = recent.get("filingDate") or []
    primary_docs = recent.get("primaryDocument") or []

    counts: dict[str, int] = {f: 0 for f in _TARGET_FORMS}

    for acc, form, fdate, pdoc in zip(accessions, forms, dates, primary_docs):
        if form not in _TARGET_FORMS:
            continue
        if counts[form] >= _FORM_CAPS[form]:
            continue
        if not pdoc:
            # SEC sometimes omits primaryDocument for older filings; skip silently.
            continue

        acc_nodash  = acc.replace("-", "")
        filing_url  = f"{_ARCHIVES_BASE}/{cik}/{acc_nodash}/{pdoc}"
        source_type = f"SEC_EDGAR_{form.replace('-', '')}"   # SEC_EDGAR_10K / SEC_EDGAR_10Q
        rows.append(_source(run_date, symbol, rank, source_type,
                            filing_url, allowed_cfg, ts, document_date=fdate))
        counts[form] += 1

    if counts["10-K"] == 0:
        rows.append(_missing(run_date, symbol, rank,
                             "SEC_EDGAR_10K", "NO_10K_IN_RECENT_FILINGS", ts))

    log.info("[discovery] %s: %d 10-K, %d 10-Q filing URLs",
             symbol, counts["10-K"], counts["10-Q"])
    return rows


# ---------------------------------------------------------------------------
# IR URL discovery  (step E)
# ---------------------------------------------------------------------------

def _discover_ir(
    run_date: str,
    symbol: str,
    rank: int,
    submissions: dict,
    allowed_cfg: dict,
    ts: str,
) -> list[dict]:
    # Tier 1 — SEC-provided investorWebsite (most authoritative; no HEAD needed)
    ir_url = (submissions.get("investorWebsite") or "").strip().rstrip("/")
    if ir_url:
        log.info("[discovery] %s: IR from submissions.investorWebsite: %s", symbol, ir_url)
        return [_source(run_date, symbol, rank,
                        "COMPANY_IR", ir_url, allowed_cfg, ts)]

    # Tier 2 — derive patterns from SEC-provided website domain
    website = (submissions.get("website") or "").strip().rstrip("/")
    if not website:
        log.info("[discovery] %s: no investorWebsite or website in submissions", symbol)
        return [_missing(run_date, symbol, rank,
                         "COMPANY_IR", "NO_IR_URL_IN_SUBMISSIONS", ts)]

    dom  = _domain(website)
    base = website if website.startswith("http") else f"https://{website}"

    patterns: list[str] = (
        [f"https://{sub}.{dom}" for sub in _IR_SUBDOMAINS]
        + [f"{base}{path}" for path in _IR_PATHS]
    )

    for url in patterns:
        if not _is_allowed(url, allowed_cfg):
            log.info("[discovery] %s: IR pattern disallowed: %s", symbol, url)
            continue
        time.sleep(_RATE_DELAY)
        if _head_ok(url):
            log.info("[discovery] %s: IR pattern resolved: %s", symbol, url)
            return [_source(run_date, symbol, rank,
                            "COMPANY_IR", url, allowed_cfg, ts)]

    log.info("[discovery] %s: IR — no pattern resolved (tried %d)", symbol, len(patterns))
    return [_missing(run_date, symbol, rank,
                     "COMPANY_IR", "IR_PATTERN_RESOLUTION_FAILED", ts)]


# ---------------------------------------------------------------------------
# Network helpers
# ---------------------------------------------------------------------------

def _fetch_tickers_map() -> tuple[dict, bool]:
    """Fetch company_tickers.json; return (ticker→entry, success_bool).
    Ticker keys are uppercase. Returns ({}, False) on any failure.
    """
    try:
        resp = requests.get(
            _COMPANY_TICKERS_URL,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=_EDGAR_TIMEOUT,
        )
        resp.raise_for_status()
        raw = resp.json()
        return {v["ticker"].upper(): v for v in raw.values()}, True
    except Exception as exc:
        log.warning("[discovery] company_tickers.json failed: %s", exc)
        return {}, False


def _fetch_json(url: str) -> dict | None:
    """GET JSON from url with EDGAR headers. Returns None on any failure."""
    try:
        resp = requests.get(
            url,
            headers={"User-Agent": _USER_AGENT, "Accept": "application/json"},
            timeout=_EDGAR_TIMEOUT,
        )
        resp.raise_for_status()
        return resp.json()
    except Exception as exc:
        log.warning("[discovery] GET failed %s — %s", url, exc)
        return None


def _head_ok(url: str) -> bool:
    """Return True if url returns HTTP < 400 (following redirects). Never raises."""
    try:
        r = requests.head(
            url,
            headers={"User-Agent": _USER_AGENT},
            timeout=_HEAD_TIMEOUT,
            allow_redirects=True,
        )
        return r.status_code < 400
    except Exception:
        return False


# ---------------------------------------------------------------------------
# Row builders
# ---------------------------------------------------------------------------

def _source(
    run_date: str,
    symbol: str,
    rank: int,
    source_type: str,
    source_url: str,
    allowed_cfg: dict,
    ts: str,
    document_date: str | None = None,
) -> dict:
    """Build a DISCOVERED or DISALLOWED source_registry row with full provenance."""
    dom     = (urlparse(source_url).netloc or "").lower()
    allowed = 1 if _is_allowed(source_url, allowed_cfg) else 0
    return {
        "run_date":          run_date,
        "symbol":            symbol,
        "rank_at_discovery": rank,
        "source_type":       source_type,
        "source_url":        source_url,
        "source_domain":     dom,
        "allowed":           allowed,
        "status":            "DISCOVERED" if allowed else "DISALLOWED",
        "missing_reason":    None,
        "discovery_ts":      ts,
    }


def _missing(
    run_date: str,
    symbol: str,
    rank: int,
    source_type: str,
    reason: str,
    ts: str,
) -> dict:
    """Build an explicit MISSING source_registry row. source_url = sentinel 'MISSING'."""
    return {
        "run_date":          run_date,
        "symbol":            symbol,
        "rank_at_discovery": rank,
        "source_type":       source_type,
        "source_url":        "MISSING",
        "source_domain":     None,
        "allowed":           0,
        "status":            "MISSING",
        "missing_reason":    reason,
        "discovery_ts":      ts,
    }


# ---------------------------------------------------------------------------
# Allowlist and utilities
# ---------------------------------------------------------------------------

def _load_allowed_config() -> dict:
    with open(_ALLOWED_FILE, encoding="utf-8") as f:
        return json.load(f)


def _is_allowed(url: str, cfg: dict) -> bool:
    """Return True if url passes the allowlist check.

    Logic (in order):
      1. Domain in allowed_domains  → always True
      2. Any disallowed keyword in the full url (case-insensitive) → False
      3. Otherwise → True  (conservative default: allow unless blocked)
    """
    try:
        domain = urlparse(url).netloc.lower()
    except Exception:
        return False
    if domain in cfg.get("allowed_domains", []):
        return True
    url_lower = url.lower()
    for kw in cfg.get("disallowed_keywords", []):
        if kw in url_lower:
            return False
    return True


def _domain(url: str) -> str:
    """Extract netloc from a URL string; fall back to the raw string."""
    parsed = urlparse(url if url.startswith("http") else f"https://{url}")
    return parsed.netloc or url


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _log_status_breakdown(symbol: str, rows: list[dict]) -> None:
    breakdown = {}
    for r in rows:
        s = r["status"]
        breakdown[s] = breakdown.get(s, 0) + 1
    log.info("[discovery] %s: %d rows — %s", symbol, len(rows), breakdown)
