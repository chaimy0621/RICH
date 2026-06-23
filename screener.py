"""Per-ticker hard screening rules 4-7.

Applied after universe.py (rules 1-3). Returns pass/reject with explicit
reason codes for every rejected ticker.
"""
from __future__ import annotations

import logging
from typing import Any

from config import (
    MIN_HISTORICAL_YEARS,
    BENEISH_GROWTH_EXEMPT_THRESHOLD,
    BENEISH_GROWTH_EXEMPT_LIVE,
)
from factors import METRIC_NAMES, extract
from data.fetcher import FMPClient, FMPError
import beneish

log = logging.getLogger(__name__)

# Reject reason codes
R_FETCH_ERROR = "FETCH_ERROR"
R_HIST_LT_5 = "HIST_LT_5"
R_REV_NONPOSITIVE = "REV_NONPOSITIVE"
R_NI_OCF_NONPOSITIVE = "NI_OCF_NONPOSITIVE"
R_OCF_NEGATIVE = "OCF_NEGATIVE"
R_MISSING_FIELDS = "MISSING_FIELDS"
R_BENEISH_FLAG = "BENEISH_FLAG"

BENEISH_THRESHOLD = -1.78


def screen_ticker(symbol: str, client: FMPClient | None = None) -> dict[str, Any]:
    """Apply rules 4-7 to one ticker.

    Returns:
        {"symbol": str, "pass": bool, "factors": dict | None,
         "reason": str | None, "detail": str | None}
    """
    try:
        f = extract(symbol, client)
    except (FMPError, Exception) as e:
        return {"symbol": symbol, "pass": False, "factors": None,
                "reason": R_FETCH_ERROR, "detail": str(e)[:100]}

    hist = f["historical_years_available"]
    if hist < MIN_HISTORICAL_YEARS:
        return {"symbol": symbol, "pass": False, "factors": f,
                "reason": R_HIST_LT_5, "detail": f"only {hist} years"}

    rev = f["revenue"]
    if rev is None or rev <= 0:
        return {"symbol": symbol, "pass": False, "factors": f,
                "reason": R_REV_NONPOSITIVE, "detail": f"revenue={rev}"}

    ni = f["netIncome"]
    ocf = f["operatingCashFlow"]
    if not ((ni is not None and ni > 0) or (ocf is not None and ocf > 0)):
        return {"symbol": symbol, "pass": False, "factors": f,
                "reason": R_NI_OCF_NONPOSITIVE,
                "detail": f"ni={ni}, ocf={ocf}"}

    if ocf is None or ocf <= 0:
        return {"symbol": symbol, "pass": False, "factors": f,
                "reason": R_OCF_NEGATIVE,
                "detail": f"ocf={ocf}"}

    bm = beneish.compute(f["_inc_rows"], f["_bs_rows"], f["_cf_rows"])
    m_score = bm["m_score"]
    f["m_score"] = m_score
    f["beneish_flag"] = 1 if (m_score is not None and m_score > BENEISH_THRESHOLD) else 0

    if m_score is not None and m_score > BENEISH_THRESHOLD:
        # §3 growth exemption (log-only by default): high-growth, stable-margin
        # firms can trip the M-Score gate on legitimate AR/revenue expansion.
        # Qualify only when M sits in (BENEISH_THRESHOLD, EXEMPT_THRESHOLD].
        exempt = (
            f.get("revenue_cagr_3y") is not None
            and f["revenue_cagr_3y"] > 0.25
            and f.get("gross_margin_stable") is True
            and m_score <= BENEISH_GROWTH_EXEMPT_THRESHOLD
        )
        if exempt and BENEISH_GROWTH_EXEMPT_LIVE:
            log.info("BENEISH_GROWTH_EXEMPT %s M=%.3f rev_cagr_3y=%.2f gm_stable=True "
                     "(LIVE: admitted)", symbol, m_score, f["revenue_cagr_3y"])
            # fall through — do not reject
        else:
            if exempt:
                log.info("BENEISH_GROWTH_EXEMPT %s M=%.3f rev_cagr_3y=%.2f gm_stable=True "
                         "(log-only: still rejected)", symbol, m_score, f["revenue_cagr_3y"])
            return {"symbol": symbol, "pass": False, "factors": f,
                    "reason": R_BENEISH_FLAG,
                    "detail": f"M={m_score:.3f}",
                    "beneish_growth_exempt": exempt}

    missing = [m for m in METRIC_NAMES if f.get(m) is None]
    if missing:
        return {"symbol": symbol, "pass": False, "factors": f,
                "reason": R_MISSING_FIELDS,
                "detail": ",".join(missing)}

    return {"symbol": symbol, "pass": True, "factors": f,
            "reason": None, "detail": None}
