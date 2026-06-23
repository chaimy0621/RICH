"""Build the eligible US stock universe via /stable/company-screener.

Enforces frozen screening rules 1, 1b, 2, 3 (market cap, dollar-volume proxy,
preferred/warrant/dual-listing filter). Per-ticker rules (4-7) live in screener.py.
"""
from __future__ import annotations

import logging
import re
from typing import Any

from config import EXCLUDED_SECTORS, MIN_DAILY_DOLLAR_VOLUME, MIN_MARKET_CAP_USD
from data.fetcher import FMPClient

log = logging.getLogger(__name__)

EXCHANGES = ("NASDAQ", "NYSE", "AMEX")

# Preferred shares: 4-letter root + 'P' + class letter, e.g. CTA-PA, MKC-PV
# Warrants/when-issued: -W, -WS, .WS, -V (when-issued)
# Dual listings on non-US venues: .NE, .CN, .L, .TO, .V
_DROP_PATTERNS = [
    re.compile(r"-P[A-Z]?$"),     # preferred shares with -P*
    re.compile(r"-W[A-Z]?$"),     # warrants -W, -WS
    re.compile(r"\.WS$"),         # warrants .WS
    re.compile(r"-V$"),           # when-issued
    re.compile(r"\.[A-Z]{2,3}$"), # foreign dual listings (.NE/.CN/.L/.TO/.V)
]


def _is_excluded(symbol: str) -> bool:
    return any(p.search(symbol) for p in _DROP_PATTERNS)


def fetch_screener_rows(client: FMPClient | None = None) -> list[dict[str, Any]]:
    c = client or FMPClient()
    common = dict(
        marketCapMoreThan=MIN_MARKET_CAP_USD,
        country="US",
        isEtf="false",
        isFund="false",
        isActivelyTrading="true",
        limit=5000,
    )
    rows: list[dict[str, Any]] = []
    for ex in EXCHANGES:
        batch = c.company_screener(exchange=ex, **common)
        log.info("screener %s -> %d rows", ex, len(batch))
        rows.extend(batch)
    return rows


def build_universe(client: FMPClient | None = None) -> dict[str, Any]:
    """Return dict with eligible rows + counts breakdown for reporting."""
    rows = fetch_screener_rows(client)

    by_exchange: dict[str, int] = {}
    for r in rows:
        by_exchange[r.get("exchangeShortName") or r.get("exchange") or "?"] = (
            by_exchange.get(r.get("exchangeShortName") or r.get("exchange") or "?", 0) + 1
        )

    after_symbol_filter = [r for r in rows if not _is_excluded(r.get("symbol", ""))]
    dropped_symbol_filter = len(rows) - len(after_symbol_filter)

    # Sector exclusion: drop structurally-challenging sectors before any
    # per-ticker work (Financial Services covers banks + insurance).
    after_sector: list[dict[str, Any]] = []
    dropped_sector = 0
    for r in after_symbol_filter:
        if (r.get("sector") or "") in EXCLUDED_SECTORS:
            dropped_sector += 1
        else:
            after_sector.append(r)
    log.info("    dropped sector exclusion: %d  (%s)",
             dropped_sector, ", ".join(sorted(EXCLUDED_SECTORS)))

    # ADR guard: belt-and-suspenders country check (API already filters
    # country=US, but catches any data-quality slippage).
    after_adr: list[dict[str, Any]] = []
    dropped_adr = 0
    for r in after_sector:
        country = r.get("country")
        if country and country != "US":
            dropped_adr += 1
        else:
            after_adr.append(r)
    if dropped_adr:
        log.info("    dropped non-US country: %d", dropped_adr)

    eligible: list[dict[str, Any]] = []
    dropped_dollar_volume = 0
    for r in after_adr:
        price = r.get("price") or 0
        volume = r.get("volume") or 0
        ddv = (price or 0) * (volume or 0)
        r["daily_dollar_volume_proxy"] = ddv
        # P0: guarantee sector/industry keys exist on every eligible row so the
        # pipeline can thread them into the candidate before scoring. company_screener
        # already returns both; setdefault is a no-op when present.
        r.setdefault("sector", None)
        r.setdefault("industry", None)
        if ddv >= MIN_DAILY_DOLLAR_VOLUME:
            eligible.append(r)
        else:
            dropped_dollar_volume += 1

    return {
        "raw_total": len(rows),
        "by_exchange": by_exchange,
        "dropped_symbol_filter": dropped_symbol_filter,
        "dropped_sector": dropped_sector,
        "dropped_adr": dropped_adr,
        "dropped_dollar_volume": dropped_dollar_volume,
        "eligible": eligible,
        "eligible_count": len(eligible),
    }


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    result = build_universe()
    print(f"\nRaw screener rows (3 exchanges union): {result['raw_total']}")
    print(f"By exchange: {result['by_exchange']}")
    print(f"Dropped (preferred/warrant/dual-listing filter): {result['dropped_symbol_filter']}")
    print(f"Dropped (excluded sectors):                      {result['dropped_sector']}")
    print(f"Dropped (non-US country):                        {result['dropped_adr']}")
    print(f"Dropped (daily_dollar_volume_proxy < 5M):        {result['dropped_dollar_volume']}")
    print(f"Eligible universe: {result['eligible_count']}")
    print("\nTop 5 by market cap:")
    for r in sorted(result["eligible"], key=lambda x: x.get("marketCap") or 0, reverse=True)[:5]:
        print(f"  {r['symbol']:8} {r['companyName'][:35]:35} mcap=${r['marketCap']/1e9:.0f}B "
              f"ddv=${r['daily_dollar_volume_proxy']/1e6:.0f}M")
