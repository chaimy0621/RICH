"""Phase 4 bgtest memo generator — structured pass/fail evidence memos.

For each of the top-N bgtest tickers, reads stored 10-K content_text from
background_docs, extracts key sections by regex, and writes a structured
evidence memo to background_review_memo.

No network calls.  Reads and writes exclusively through bg_storage.
Idempotent: INSERT OR IGNORE on UNIQUE(run_date, symbol).

Section extraction strategy
----------------------------
  Inline XBRL 10-K filings begin with a dense XBRL/namespace preamble
  (typically 30–75 KB of machine-readable data) before the human-readable
  Item sections.  The same "Item N. Title" heading appears in the Table of
  Contents (TOC) and then again at the actual section body.

  TOC entries are discriminated by being immediately followed by a digit
  (the page number).  Actual section starts are followed by a letter or
  punctuation.  We skip TOC matches and take the first real section.

  This approach handles:
    "Item 1.  Business Our mission..."     (BKNG / BKNG style)
    "Item 1. BusinessOur mission..."       (APP style, no space)
    "Item\xa01.Business3Item\xa01A."       (EXEL TOC with NBSP)
    "ITEM 1.BusinessUnless the context..." (CHK/EXE CAPS style)

Red flag detection
------------------
  Only confirmed disclosures are flagged — boilerplate audit language is
  not.  Examples that are intentionally NOT flagged:
    "assessing the risk that a material weakness exists"  (audit opinion)
    "recovery analysis of... restatements"  (executive clawback policy)

Pass/Fail verdicts
------------------
  FLAG       at least one hard red flag found in the document
  INCOMPLETE Item 1 (Business) section body not parseable
  PASS       no red flags and Item 1 found

EXE / Chesapeake name change
----------------------------
  Expand Energy (EXE) filed its FY2023 10-K under the prior ticker CHK
  (Chesapeake Energy).  When any of the ticker's stored source_urls
  contains "chk-", this is recorded as a context note in the memo.
"""
from __future__ import annotations

import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

_HERE      = Path(__file__).parent
_RICH_ROOT = _HERE.parent
for _p in (str(_RICH_ROOT), str(_HERE)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

from config_bgtest import BGTEST_DB_PATH  # noqa: E402
import bg_storage                          # noqa: E402

log = logging.getLogger(__name__)

MEMO_VERSION = "v1"

# ---------------------------------------------------------------------------
# Section header patterns (case-insensitive; [\s\xa0\.]+ handles all spacing)
# ---------------------------------------------------------------------------
_SEC = {
    # Patterns include enough of the title that the char immediately after the
    # match is a digit (TOC page-number) for table-of-contents entries or a
    # letter (prose) for actual sections.  This is what the discriminator tests.
    "1":   re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+1[\s\xa0.]+Business"),
    # "Risk Factors" — extend past "Risk" so the discriminator sees "6" not "F"
    "1a":  re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+1A[\s\xa0.]+Risk[\s\xa0]+Factors"),
    "1b":  re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+1B[\s\xa0.]"),
    "2":   re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+2[\s\xa0.]+Propert"),
    # "Operations" — extend to end of standard title so page number is first char
    "7":   re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+7[\s\xa0.]+Management[^\n]{5,120}Operations"),
    "7a":  re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+7A[\s\xa0.]"),
    "8":   re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+8[\s\xa0.]"),
    "9a":  re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+9A[\s\xa0.]+Controls"),
    "9b":  re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+9B[\s\xa0.]"),
    "10":  re.compile(r"(?i)(?:ITEM|Item)[\s\xa0]+10[\s\xa0.]"),
}

# Section body extraction limits
_BUSINESS_CHARS     = 1_500
_RISK_CHARS         = 2_500
_MDA_CHARS          = 1_800
_SECTION_SEARCH_CAP = 25_000   # max chars to scan for keywords within a section

# ---------------------------------------------------------------------------
# Moat signal keywords  (phrase → short label)
# ---------------------------------------------------------------------------
_MOAT_KEYWORDS: list[tuple[str, str]] = [
    ("switching cost",    "switching costs"),
    ("network effect",    "network effects"),
    ("pricing power",     "pricing power"),
    ("barriers to entry", "barriers to entry"),
    ("scale advantage",   "scale advantage"),
    ("proprietary",       "proprietary tech/data"),
    ("brand recognition", "brand recognition"),
    ("loyalty program",   "loyalty program"),
    ("dominant",          "market dominance"),
    ("market share",      "market share"),
    ("exclusive",         "exclusive relationships"),
]

# ---------------------------------------------------------------------------
# Capital allocation keywords
# ---------------------------------------------------------------------------
_CAPALLOC_KEYWORDS: list[tuple[str, str]] = [
    ("repurchas",       "share repurchase"),
    ("buyback",         "buyback"),
    ("dividend",        "dividend"),
    ("acquisition",     "acquisition / M&A"),
    ("capital expenditure", "capex"),
    ("capex",           "capex"),
    ("allocat",         "capital allocation"),
]

# ---------------------------------------------------------------------------
# Red flag phrases — confirmed disclosures only (not boilerplate)
# ---------------------------------------------------------------------------
_RED_FLAG_PHRASES: list[tuple[str, str]] = [
    ("identified a material weakness",         "MATERIAL_WEAKNESS"),
    ("material weakness in our internal",      "MATERIAL_WEAKNESS"),
    ("material weakness in our financial",     "MATERIAL_WEAKNESS"),
    ("going concern",                          "GOING_CONCERN"),
    ("we have restated",                       "RESTATEMENT"),
    ("required to restate",                    "RESTATEMENT"),
    ("restatement of our financial",           "RESTATEMENT"),
]

# Snippet context around red flag hits
_FLAG_CONTEXT = 180


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(
    candidates: list[dict[str, Any]],
    run_date: str,
    db_path: Path = BGTEST_DB_PATH,
) -> dict[str, int]:
    """Generate evidence memos for all top-N candidates.

    Reads stored 10-K content from background_docs.
    Writes one row per ticker to background_review_memo.

    Returns stats dict: total, written, skipped (already in DB), failed.
    """
    stats: dict[str, int] = {"total": len(candidates), "written": 0, "skipped": 0, "failed": 0}
    already = bg_storage.get_existing_memos(run_date, [c["symbol"] for c in candidates], db_path)

    for c in candidates:
        sym  = c["symbol"]
        rank = c["recommendation_rank"]
        if sym in already:
            log.info("[memo] %s: already in background_review_memo — skip", sym)
            stats["skipped"] += 1
            continue
        log.info("[memo] generating memo for %s (rank=%d) ...", sym, rank)
        try:
            _generate_one(sym, rank, run_date, db_path)
            stats["written"] += 1
        except Exception:
            log.exception("[memo] %s: memo generation failed", sym)
            stats["failed"] += 1

    log.info(
        "[memo] done — total=%d written=%d skipped=%d failed=%d",
        stats["total"], stats["written"], stats["skipped"], stats["failed"],
    )
    return stats


# ---------------------------------------------------------------------------
# Per-ticker memo generation
# ---------------------------------------------------------------------------

def _generate_one(symbol: str, rank: int, run_date: str, db_path: Path) -> None:
    """Generate and store one memo for a single ticker."""
    now = _now_iso()
    docs = bg_storage.get_docs_for_symbol(run_date, symbol, db_path)
    missing_srcs = bg_storage.get_missing_sources(run_date, symbol, db_path)

    docs_found   = sum(1 for d in docs if not d["is_missing"])
    docs_missing = sum(1 for d in docs if d["is_missing"]) + len(missing_srcs)

    # Pick the most recent available 10-K
    tenk_docs = [
        d for d in docs
        if d["source_type"] == "SEC_EDGAR_10K" and not d["is_missing"]
        and d.get("content_text")
    ]
    tenk_docs.sort(key=lambda d: d.get("document_date") or "", reverse=True)
    primary_doc = tenk_docs[0] if tenk_docs else None

    # Name-change context flag for EXE (filed under CHK ticker historically)
    chk_urls = [
        d["source_url"] for d in docs
        if "chk-" in (d.get("source_url") or "").lower()
    ]
    name_change_note = (
        f"CONTEXT: Oldest 10-K (FY2023) filed under prior name "
        f"Chesapeake Energy (CHK). URL: {chk_urls[0]}"
        if chk_urls else None
    )

    if primary_doc is None:
        memo_text = _memo_incomplete(symbol, run_date, rank, missing_srcs, name_change_note)
        pass_fail = "INCOMPLETE"
    else:
        memo_text, pass_fail = _build_memo(
            symbol, run_date, rank, primary_doc, docs, missing_srcs, name_change_note
        )

    sources_checked = json.dumps([
        d["source_url"] for d in docs
        if not d["is_missing"] and d["source_type"] == "SEC_EDGAR_10K"
    ])

    bg_storage.insert_memo({
        "run_date":       run_date,
        "symbol":         symbol,
        "rank_at_review": rank,
        "pass_fail":      pass_fail,
        "memo_text":      memo_text,
        "docs_found":     docs_found,
        "docs_missing":   docs_missing,
        "sources_checked": sources_checked,
        "review_ts":      now,
        "memo_version":   MEMO_VERSION,
    }, db_path)

    log.info("[memo] %s: %s  docs_found=%d  docs_missing=%d",
             symbol, pass_fail, docs_found, docs_missing)


# ---------------------------------------------------------------------------
# Memo builders
# ---------------------------------------------------------------------------

def _build_memo(
    symbol: str,
    run_date: str,
    rank: int,
    primary_doc: dict,
    all_docs: list[dict],
    missing_srcs: list[dict],
    name_change_note: str | None,
) -> tuple[str, str]:
    """Extract sections and build the full memo text.

    Returns (memo_text, pass_fail).
    """
    text  = primary_doc["content_text"]
    d_date = primary_doc.get("document_date") or "unknown date"
    d_url  = primary_doc["source_url"]

    # ---- extract sections -----------------------------------------------
    item1_text  = _extract_section(text, "1",  ["1a", "2"])
    item1a_text = _extract_section(text, "1a", ["1b", "2"])
    item7_text  = _extract_section(text, "7",  ["7a", "8"])

    # ---- build each memo block ------------------------------------------
    s1 = _section_business(item1_text)
    s2 = _section_moat(item1_text)
    s3 = _section_risks(item1a_text)
    s4 = _section_capalloc(item7_text)
    s5 = _section_revenue_trend(item7_text)
    s6, red_flags = _section_redflags(text)

    # ---- determine pass/fail --------------------------------------------
    if red_flags:
        pass_fail = "FLAG"
    elif item1_text is None:
        pass_fail = "INCOMPLETE"
    else:
        pass_fail = "PASS"

    # ---- sources used / missing -----------------------------------------
    used_sources = "\n".join(
        f"  {d['source_type']}  {d.get('document_date','')}  {d['source_url']}"
        for d in all_docs
        if not d["is_missing"] and d["source_type"] == "SEC_EDGAR_10K"
    ) or "  (none)"

    missing_lines = "\n".join(
        f"  {m['source_type']}  {m.get('missing_reason','')}  {m['source_url']}"
        for m in missing_srcs
    ) or "  NONE"

    # ---- assemble -------------------------------------------------------
    lines = [
        f"=== BACKGROUND MEMO: {symbol} ({run_date}) ===",
        f"Rank: {rank}",
        f"Pass/Fail: {pass_fail}",
        f"Primary source: {d_url}  (period: {d_date})",
    ]
    if name_change_note:
        lines.append(f"Note: {name_change_note}")
    lines += [
        "",
        "[1] Business Overview",
        s1,
        "",
        "[2] Moat Signals",
        s2,
        "",
        "[3] Key Risks (from Item 1A Risk Factors)",
        s3,
        "",
        "[4] Capital Allocation",
        s4,
        "",
        "[5] Revenue & Earnings Trend (from Item 7 MD&A)",
        s5,
        "",
        "[6] Red Flags",
        s6,
        "",
        "Sources used (10-K):",
        used_sources,
        "",
        "Missing sources:",
        missing_lines,
        "===",
    ]
    return "\n".join(lines), pass_fail


def _memo_incomplete(
    symbol: str,
    run_date: str,
    rank: int,
    missing_srcs: list[dict],
    name_change_note: str | None,
) -> str:
    missing_lines = "\n".join(
        f"  {m['source_type']}  {m.get('missing_reason','')}  {m['source_url']}"
        for m in missing_srcs
    ) or "  NONE"
    lines = [
        f"=== BACKGROUND MEMO: {symbol} ({run_date}) ===",
        f"Rank: {rank}",
        "Pass/Fail: INCOMPLETE",
        "Reason: No 10-K content available in background_docs.",
    ]
    if name_change_note:
        lines.append(f"Note: {name_change_note}")
    lines += [
        "",
        "Missing sources:",
        missing_lines,
        "===",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Section content builders
# ---------------------------------------------------------------------------

def _section_business(text: str | None) -> str:
    if text is None:
        return "NOT_FOUND — Item 1 (Business) section not located in document."
    excerpt = _clean(text[:_BUSINESS_CHARS])
    return f"[Excerpt from Item 1, first {_BUSINESS_CHARS} chars]\n{excerpt}"


def _section_moat(text: str | None) -> str:
    if text is None:
        return "NOT_FOUND — Item 1 section unavailable; moat scan skipped."
    search_text = text[:_SECTION_SEARCH_CAP]
    hits: list[str] = []
    seen_labels: set[str] = set()
    for phrase, label in _MOAT_KEYWORDS:
        if label in seen_labels:
            continue
        idx = search_text.lower().find(phrase.lower())
        if idx >= 0:
            snip = _clean(search_text[max(0, idx - 40):idx + 160])
            hits.append(f"• {label}: ...{snip}...")
            seen_labels.add(label)
    if not hits:
        return "NOT_FOUND — No explicit moat signal keywords detected in Item 1."
    return "\n".join(hits)


def _section_risks(text: str | None) -> str:
    if text is None:
        return "NOT_FOUND — Item 1A (Risk Factors) section not located."
    excerpt = _clean(text[:_RISK_CHARS])
    return f"[Excerpt from Item 1A, first {_RISK_CHARS} chars — contains top risks]\n{excerpt}"


def _section_capalloc(text: str | None) -> str:
    if text is None:
        return "NOT_FOUND — Item 7 (MD&A) section not located."
    search_text = text[:_SECTION_SEARCH_CAP]
    hits: list[str] = []
    seen_labels: set[str] = set()
    for phrase, label in _CAPALLOC_KEYWORDS:
        if label in seen_labels:
            continue
        idx = search_text.lower().find(phrase.lower())
        if idx >= 0:
            snip = _clean(search_text[max(0, idx - 30):idx + 170])
            hits.append(f"• {label}: ...{snip}...")
            seen_labels.add(label)
    if not hits:
        return "NOT_FOUND — No capital allocation keywords detected in Item 7."
    return "\n".join(hits)


def _section_revenue_trend(text: str | None) -> str:
    if text is None:
        return "NOT_FOUND — Item 7 (MD&A) section not located."
    excerpt = _clean(text[:_MDA_CHARS])
    return f"[Excerpt from Item 7 MD&A, first {_MDA_CHARS} chars]\n{excerpt}"


def _section_redflags(full_text: str) -> tuple[str, list[str]]:
    """Scan the full document for confirmed red flag disclosures.

    Returns (formatted_string, list_of_flag_codes).
    list_of_flag_codes is empty when the document is clean.
    """
    found: list[tuple[str, str]] = []   # (code, excerpt)
    lower = full_text.lower()
    for phrase, code in _RED_FLAG_PHRASES:
        # Avoid duplicate codes (multiple phrases map to the same code)
        if any(c == code for c, _ in found):
            continue
        idx = lower.find(phrase.lower())
        if idx >= 0:
            snip = _clean(full_text[max(0, idx - 40):idx + _FLAG_CONTEXT])
            found.append((code, snip))

    if not found:
        return "NONE FOUND", []

    lines = []
    for code, snip in found:
        lines.append(f"⚑ {code}: ...{snip}...")
    return "\n".join(lines), [code for code, _ in found]


# ---------------------------------------------------------------------------
# Section extractor
# ---------------------------------------------------------------------------

def _extract_section(
    text: str,
    section_key: str,
    end_keys: list[str],
    max_chars: int = 30_000,
) -> str | None:
    """Extract the body text of a 10-K section.

    Finds the first non-TOC occurrence of the section header, then
    extracts text up to the earliest following section header or max_chars.

    Args:
        text        : full document content_text
        section_key : key into _SEC patterns (e.g. "1", "1a", "7")
        end_keys    : section keys that mark the end (tried in order)
        max_chars   : hard cap on returned text length

    Returns:
        Body text string, or None if the section header is not found.
    """
    start = _find_section_start(text, _SEC[section_key])
    if start < 0:
        return None

    remaining = text[start:]
    end_pos   = len(remaining)
    for ek in end_keys:
        ep = _find_section_start(remaining, _SEC[ek], skip_first=200)
        if 0 < ep < end_pos:
            end_pos = ep

    return remaining[:min(end_pos, max_chars)]


def _find_section_start(
    text: str,
    pattern: re.Pattern,
    skip_first: int = 0,
) -> int:
    """Return position of the first non-TOC section header match, or -1.

    A match is considered a TOC entry when the character immediately
    following the matched text (after stripping whitespace and NBSP) is
    a digit.  We skip those and return the first real section start.

    skip_first: do not return any match before this character position
                (used to find end-of-section headers inside the body).
    """
    for m in pattern.finditer(text):
        if m.start() < skip_first:
            continue
        after = text[m.end():m.end() + 10].lstrip(" \t\n\r\xa0")
        if after and after[0].isdigit():
            continue   # TOC entry — page number follows
        return m.start()
    return -1


# ---------------------------------------------------------------------------
# Utilities
# ---------------------------------------------------------------------------

def _clean(s: str) -> str:
    """Collapse excess whitespace and strip non-breaking spaces."""
    s = s.replace("\xa0", " ")
    s = re.sub(r"[ \t]{2,}", " ", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
