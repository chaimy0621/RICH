"""Phase 3 bgtest document fetcher — SEC EDGAR 10-K / 10-Q / 20-F.

Reads source_registry for allowed, discovered EDGAR filing URLs,
fetches their content, extracts plain text, and writes results to
background_docs.  Every attempt is logged in document_fetch_log.

Selection basis
---------------
  source_type IN {SEC_EDGAR_10K, SEC_EDGAR_10Q, SEC_EDGAR_20F}
  allowed = 1, status = 'DISCOVERED'
  COMPANY_IR sources are skipped entirely (out of Phase 3 scope).

Idempotency
-----------
  Pre-flight check: bg_storage.get_fetched_urls() returns the set of
  source_urls that already have a non-missing row in background_docs for
  this run_date.  Those URLs are skipped without any network call.

  INSERT OR IGNORE on both tables provides a second safety net in case of
  concurrent or partial re-runs.

Failure policy
--------------
  Every URL that cannot be fetched or parsed receives:
    document_fetch_log  — fetch_success = 0
    background_docs     — is_missing = 1, missing_reason set

Missing reason codes
--------------------
  FETCH_FAILED      network error / non-HTTP exception
  HTTP_4XX          HTTP 4xx response
  HTTP_5XX          HTTP 5xx response
  SIZE_EXCEEDED     download exceeds _MAX_DOWNLOAD_BYTES
  EMPTY_CONTENT     HTTP 2xx but 0 bytes returned
  PARSE_FAILED      text extraction raised an exception

Text extraction
---------------
  HTML: lxml (installed) preferred; stdlib HTMLParser fallback.
  PDF:  pdfplumber → PyPDF2 (neither currently installed).
        Raw bytes are always stored for PDFs; content_text will be None
        when no PDF extraction library is available.
  HTML: content_raw_bytes is not stored (text-only; keeps DB size down).
"""
from __future__ import annotations

import hashlib
import io
import logging
import re
import sys
import time
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

import requests

_HERE      = Path(__file__).parent
_RICH_ROOT = _HERE.parent
for _p in (str(_RICH_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config_bgtest import BGTEST_DB_PATH  # noqa: E402
import bg_storage                          # noqa: E402

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
_USER_AGENT         = "RICH-bgtest chai.my621@gmail.com"
_EDGAR_TIMEOUT      = (10, 60)           # generous read timeout for large filings
_RATE_DELAY         = 0.12               # seconds; SEC enforces <= 10 req/s
_MAX_DOWNLOAD_BYTES = 25 * 1024 * 1024   # 25 MB hard cap per document

_TARGET_SOURCE_TYPES = frozenset({"SEC_EDGAR_10K", "SEC_EDGAR_10Q", "SEC_EDGAR_20F"})


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def fetch(
    candidates: list[dict[str, Any]],
    run_date: str,
    db_path: Path = BGTEST_DB_PATH,
) -> dict[str, int]:
    """Fetch EDGAR filing content for all top-N candidates.

    Queries source_registry for eligible URLs, skips already-fetched ones,
    then fetches, extracts, and persists each document.

    Returns:
        stats dict with keys: total, fetched, skipped, failed.
    """
    rank_map = {c["symbol"]: c["recommendation_rank"] for c in candidates}
    symbols  = list(rank_map.keys())

    sources      = bg_storage.get_fetch_candidates(run_date, symbols, db_path)
    already_done = bg_storage.get_fetched_urls(run_date, symbols, db_path)

    log.info(
        "[fetcher] %d EDGAR filing URLs eligible | %d already fetched (will skip)",
        len(sources), len(already_done),
    )

    stats: dict[str, int] = {"total": len(sources), "fetched": 0, "skipped": 0, "failed": 0}

    for src in sources:
        sym   = src["symbol"]
        url   = src["source_url"]
        stype = src["source_type"]
        rank  = src.get("rank_at_discovery") or rank_map.get(sym, 0)

        if url in already_done:
            log.debug("[fetcher] skip (already done): %s  %s", sym, url)
            stats["skipped"] += 1
            continue

        log.info("[fetcher] %s  %-22s  %s", sym, stype, url)
        time.sleep(_RATE_DELAY)
        ok = _fetch_one(sym, rank, run_date, url, stype, db_path)
        if ok:
            stats["fetched"] += 1
        else:
            stats["failed"] += 1

    return stats


# ---------------------------------------------------------------------------
# Per-URL fetch
# ---------------------------------------------------------------------------

def _fetch_one(
    symbol: str,
    rank: int,
    run_date: str,
    source_url: str,
    source_type: str,
    db_path: Path,
) -> bool:
    """Fetch one URL; write one fetch_log row and one background_docs row.

    Returns True on success (HTTP 2xx + extraction), False on any failure.
    """
    fetch_ts = _now_iso()

    # ---- network fetch --------------------------------------------------
    try:
        resp = requests.get(
            source_url,
            headers={"User-Agent": _USER_AGENT, "Accept": "*/*"},
            timeout=_EDGAR_TIMEOUT,
        )
    except Exception as exc:
        _write_failure(
            run_date, symbol, source_url, source_type, fetch_ts,
            http_status=None,
            missing_reason="FETCH_FAILED",
            failure_reason=f"{type(exc).__name__}: {exc}",
            content_bytes=0,
            db_path=db_path,
        )
        return False

    http_status   = resp.status_code
    raw_bytes     = resp.content
    content_bytes = len(raw_bytes)

    # ---- HTTP error checks ---------------------------------------------
    if http_status >= 500:
        _write_failure(
            run_date, symbol, source_url, source_type, fetch_ts,
            http_status=http_status, missing_reason="HTTP_5XX",
            failure_reason=f"HTTP {http_status}", content_bytes=content_bytes,
            db_path=db_path,
        )
        return False

    if http_status >= 400:
        _write_failure(
            run_date, symbol, source_url, source_type, fetch_ts,
            http_status=http_status, missing_reason="HTTP_4XX",
            failure_reason=f"HTTP {http_status}", content_bytes=content_bytes,
            db_path=db_path,
        )
        return False

    if content_bytes == 0:
        _write_failure(
            run_date, symbol, source_url, source_type, fetch_ts,
            http_status=http_status, missing_reason="EMPTY_CONTENT",
            failure_reason="0 bytes received", content_bytes=0,
            db_path=db_path,
        )
        return False

    if content_bytes > _MAX_DOWNLOAD_BYTES:
        _write_failure(
            run_date, symbol, source_url, source_type, fetch_ts,
            http_status=http_status, missing_reason="SIZE_EXCEEDED",
            failure_reason=f"{content_bytes:,} bytes > {_MAX_DOWNLOAD_BYTES:,}",
            content_bytes=content_bytes, db_path=db_path,
        )
        return False

    # ---- content type detection + extraction ---------------------------
    content_type = resp.headers.get("Content-Type", "").lower()
    is_pdf       = _detect_pdf(source_url, content_type, raw_bytes)
    content_hash = _sha256(raw_bytes)

    if is_pdf:
        text, parse_err = _extract_pdf(raw_bytes)
        raw_store       = raw_bytes
    else:
        text, parse_err = _extract_html(raw_bytes)
        raw_store       = None   # HTML: store extracted text only

    if parse_err:
        _write_failure(
            run_date, symbol, source_url, source_type, fetch_ts,
            http_status=http_status, missing_reason="PARSE_FAILED",
            failure_reason=parse_err, content_bytes=content_bytes,
            db_path=db_path,
        )
        return False

    title    = _title_from_html(raw_bytes) if not is_pdf else None
    doc_date = _date_from_url(source_url)

    log.info(
        "[fetcher] %s OK  %s  %d bytes  hash=%s...  text=%s chars",
        symbol, source_type, content_bytes, content_hash[:12],
        f"{len(text):,}" if text else "None",
    )

    # ---- persist --------------------------------------------------------
    bg_storage.insert_fetch_log({
        "run_date":       run_date,
        "symbol":         symbol,
        "source_url":     source_url,
        "source_type":    source_type,
        "fetch_ts":       fetch_ts,
        "http_status":    http_status,
        "fetch_success":  1,
        "failure_reason": None,
        "content_bytes":  content_bytes,
        "content_hash":   content_hash,
    }, db_path)

    bg_storage.upsert_doc({
        "run_date":          run_date,
        "symbol":            symbol,
        "source_url":        source_url,
        "source_type":       source_type,
        "document_date":     doc_date,
        "document_title":    title,
        "fetch_ts":          fetch_ts,
        "content_hash":      content_hash,
        "content_text":      text,
        "content_raw_bytes": raw_store,
        "is_missing":        0,
        "missing_reason":    None,
    }, db_path)

    return True


def _write_failure(
    run_date: str,
    symbol: str,
    source_url: str,
    source_type: str,
    fetch_ts: str,
    http_status: int | None,
    missing_reason: str,
    failure_reason: str,
    content_bytes: int,
    db_path: Path,
) -> None:
    log.warning("[fetcher] %s FAIL %s — %s", symbol, missing_reason, failure_reason)
    bg_storage.insert_fetch_log({
        "run_date":       run_date,
        "symbol":         symbol,
        "source_url":     source_url,
        "source_type":    source_type,
        "fetch_ts":       fetch_ts,
        "http_status":    http_status,
        "fetch_success":  0,
        "failure_reason": failure_reason,
        "content_bytes":  content_bytes,
        "content_hash":   None,
    }, db_path)
    bg_storage.upsert_doc({
        "run_date":          run_date,
        "symbol":            symbol,
        "source_url":        source_url,
        "source_type":       source_type,
        "document_date":     None,
        "document_title":    None,
        "fetch_ts":          fetch_ts,
        "content_hash":      None,
        "content_text":      None,
        "content_raw_bytes": None,
        "is_missing":        1,
        "missing_reason":    missing_reason,
    }, db_path)


# ---------------------------------------------------------------------------
# Text extraction
# ---------------------------------------------------------------------------

def _extract_html(content: bytes) -> tuple[str | None, str | None]:
    """Extract plain text from HTML bytes.

    Returns (text, error_string).  error_string is None on success.
    Prefers lxml (installed); falls back to stdlib HTMLParser.
    """
    try:
        from lxml import html as lxml_html
        tree = lxml_html.fromstring(content)
        for elem in tree.xpath("//script | //style | //head"):
            parent = elem.getparent()
            if parent is not None:
                parent.remove(elem)
        text = _normalise_whitespace(tree.text_content())
        return (text or None), None
    except ImportError:
        pass
    except Exception as exc:
        log.debug("[fetcher] lxml parse error, falling back to stdlib: %s", exc)

    try:
        extractor = _StdlibTextExtractor()
        extractor.feed(content.decode("utf-8", errors="replace"))
        text = _normalise_whitespace(extractor.get_text())
        return (text or None), None
    except Exception as exc:
        return None, f"HTMLParser: {exc}"


def _extract_pdf(content: bytes) -> tuple[str | None, str | None]:
    """Extract text from PDF bytes.

    Returns (text, error_string).  When no PDF library is available,
    returns (None, None) — raw bytes are still stored by the caller.
    """
    try:
        import pdfplumber
        with pdfplumber.open(io.BytesIO(content)) as pdf:
            pages = [p.extract_text() for p in pdf.pages if p.extract_text()]
        text = _normalise_whitespace("\n".join(pages))
        return (text or None), None
    except ImportError:
        pass
    except Exception as exc:
        log.debug("[fetcher] pdfplumber error: %s", exc)

    try:
        import PyPDF2
        reader = PyPDF2.PdfReader(io.BytesIO(content))
        pages  = [p.extract_text() for p in reader.pages if p.extract_text()]
        text   = _normalise_whitespace("\n".join(pages))
        return (text or None), None
    except ImportError:
        pass
    except Exception as exc:
        log.debug("[fetcher] PyPDF2 error: %s", exc)

    # No PDF library available — caller stores raw bytes; text stays None.
    return None, None


def _title_from_html(content: bytes) -> str | None:
    """Extract <title> from HTML bytes.  Returns at most 256 chars."""
    try:
        from lxml import html as lxml_html
        tree  = lxml_html.fromstring(content[:16384])
        title = tree.findtext(".//title")
        if title:
            return title.strip()[:256]
    except Exception:
        pass
    m = re.search(rb"<title[^>]*>(.*?)</title>", content[:16384], re.I | re.S)
    if m:
        return m.group(1).decode("utf-8", errors="replace").strip()[:256]
    return None


# ---------------------------------------------------------------------------
# Stdlib HTML extractor (bs4-free fallback)
# ---------------------------------------------------------------------------

class _StdlibTextExtractor(HTMLParser):
    _SKIP_TAGS = frozenset({"script", "style", "head", "meta", "link", "noscript"})

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self._skip_depth = 0
        self._parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list) -> None:
        if tag.lower() in self._SKIP_TAGS:
            self._skip_depth += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self._SKIP_TAGS and self._skip_depth > 0:
            self._skip_depth -= 1

    def handle_data(self, data: str) -> None:
        if not self._skip_depth:
            stripped = data.strip()
            if stripped:
                self._parts.append(stripped)

    def get_text(self) -> str:
        return "\n".join(self._parts)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _detect_pdf(url: str, content_type: str, raw: bytes) -> bool:
    if "pdf" in content_type:
        return True
    if urlparse(url).path.lower().endswith(".pdf"):
        return True
    return raw[:4] == b"%PDF"


def _date_from_url(url: str) -> str | None:
    """Parse YYYY-MM-DD from the filename component of a URL."""
    name = Path(urlparse(url).path).name
    m = re.search(r"(\d{4})(\d{2})(\d{2})", name)
    if m:
        y, mo, d = m.group(1), m.group(2), m.group(3)
        if 1990 <= int(y) <= 2099 and 1 <= int(mo) <= 12 and 1 <= int(d) <= 31:
            return f"{y}-{mo}-{d}"
    return None


def _normalise_whitespace(text: str) -> str:
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
