"""Central config for RICH."""
from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).parent
load_dotenv(ROOT / ".env")

FMP_API_KEY = os.getenv("FMP_API_KEY", "").strip()
FMP_BASE_URL = "https://financialmodelingprep.com/stable"

CACHE_DB = ROOT / "data" / "cache.db"
LOG_DIR = ROOT / "logs"
RESULTS_DIR = ROOT / "results"

TIMEZONE = "Asia/Kuala_Lumpur"
DAILY_RUN_TIME = "08:30"

FUNDAMENTAL_YEARS = 11
REQUEST_TIMEOUT_S = 20
MAX_RETRIES = 3

# Screening floors
MIN_MARKET_CAP_USD = 2_000_000_000
MIN_DAILY_DOLLAR_VOLUME = 5_000_000
MIN_HISTORICAL_YEARS = 5
PREFERRED_HISTORICAL_YEARS = 10

# Sectors excluded from the universe — these are structurally hard to score
# fairly with RICH's fundamental model (regulatory capital, rate sensitivity,
# non-standard accruals). "Financial Services" covers banks and insurance.
EXCLUDED_SECTORS: frozenset[str] = frozenset({
    "Financial Services",
    "Utilities",
    "Real Estate",
})

# Cooldown
COOLDOWN_DAYS = 60

# Output
TOP_N = 5

# Valuation
VALUATION_WEIGHT = 0.5   # k in: total_score_adj = total_score + k * valuation_adj

# Beneish growth exemption (§3). High-growth, stable-margin firms can trip the
# M-Score gate on legitimate AR/revenue expansion. When LIVE, a qualifying stock
# with M in (BENEISH_THRESHOLD, BENEISH_GROWTH_EXEMPT_THRESHOLD] is admitted
# instead of rejected. Shipped LOG-ONLY (LIVE=False): the exemption is detected
# and logged but the stock is STILL rejected, so live picks are unchanged.
BENEISH_GROWTH_EXEMPT_THRESHOLD = -1.50
BENEISH_GROWTH_EXEMPT_LIVE = False

# Revenue one-time-spike protection (P4 / §4; shadow). A single-year YoY revenue
# jump greater than REVENUE_SPIKE_THRESHOLD x the smoothed 5y CAGR is treated as
# non-recurring; a shadow growth penalty of REVENUE_SPIKE_PENALTY pts is recorded
# (never applied to the live growth_score / total_score).
REVENUE_SPIKE_THRESHOLD = 3.0
REVENUE_SPIKE_PENALTY = 10


def require_api_key() -> str:
    if not FMP_API_KEY:
        raise RuntimeError(
            "FMP_API_KEY missing. Copy .env.example to .env and set your key."
        )
    return FMP_API_KEY


# ---------------------------------------------------------------------------
# Signal governance registry
# Each entry documents the lifecycle of a shadow signal from first observation
# to promotion decision. Maintained by governance.py.
# ---------------------------------------------------------------------------
SIGNAL_GOVERNANCE: dict[str, dict] = {
    # --- Phase 0: Valuation Overlay ---
    "peg_ratio": {
        "status": "shadow",
        "sleeve": "Growth",
        "shadow_since": "2026-05-03",
        "gate_date": None,  # parked indefinitely — too sparse / forward-looking
        "promotion_criteria": "Achieve <30% null rate and show positive rank-correlation with 1-year forward return over ≥60 observations.",
        "notes": "High null rate on FMP; forward-looking P/E growth estimate unreliable for small-caps.",
    },
    "valuation_adj": {
        "status": "shadow",
        "sleeve": "Overlay",
        "shadow_since": "2026-05-03",
        "gate_date": "2026-06-02",
        "promotion_criteria": "Valuation-adjusted ranking (recommendation_rank_with_valuation) produces equal or better 30-day return vs. base ranking over ≥30 observations.",
        "notes": "Sector-median PE model v1_static_sector_pe; live blend (k=0.50) already adjusts rank display but does not change daily_pick selection.",
    },
    "total_score_adj": {
        "status": "shadow",
        "sleeve": "Overlay",
        "shadow_since": "2026-05-03",
        "gate_date": "2026-06-02",
        "promotion_criteria": "Promote alongside valuation_adj; derived column, no independent gate.",
        "notes": "total_score + k * valuation_adj; observation column only.",
    },
    "recommendation_rank_with_valuation": {
        "status": "shadow",
        "sleeve": "Overlay",
        "shadow_since": "2026-05-03",
        "gate_date": "2026-06-02",
        "promotion_criteria": "Replace recommendation_rank as the daily-pick selection criterion if 30-day return improves ≥5% vs. base.",
        "notes": "Valuation-adjusted rank; stored for comparison only until gate date.",
    },
    # --- Phase 1: Fragility Filter ---
    "altman_z_score": {
        "status": "shadow",
        "sleeve": "ValueRisk",
        "shadow_since": "2026-05-23",
        "gate_date": "2026-07-04",
        "promotion_criteria": "Verify z<1.81 (distress zone) does not appear in top-10 picks with positive outcomes; promote as soft VR sleeve penalty if distribution confirms bias.",
        "notes": "Biased low for asset-light tech (negative WC/RE); never promote to hard gate without sector-stratified distribution review.",
    },
    "interest_coverage": {
        "status": "shadow",
        "sleeve": "ValueRisk",
        "shadow_since": "2026-05-23",
        "gate_date": "2026-07-04",
        "promotion_criteria": "Promote to VR sleeve soft penalty if coverage <1.5 correlates with drawdown events over ≥30 observations.",
        "notes": "NULL for zero-debt companies; treat NULL as neutral (not penalising).",
    },
    "fcf_negative_years_5": {
        "status": "shadow",
        "sleeve": "ValueRisk",
        "shadow_since": "2026-05-23",
        "gate_date": "2026-07-04",
        "promotion_criteria": "Promote to ruin_risk_score component (already done); consider hard gate at ≥4 of 5 years negative if confirmed by drawdown data.",
        "notes": "Range 0–5; distinctive from D/E — captures persistent cash burn even with low reported debt.",
    },
    # --- Phase 2: Ruin Risk Composite ---
    "ruin_risk_score": {
        "status": "shadow",
        "sleeve": "ValueRisk",
        "shadow_since": "2026-05-23",
        "gate_date": "2026-07-04",
        "promotion_criteria": "Promote as VR sleeve soft penalty (score -= ruin_risk_score * w) if picks with score >0.6 show worse 60-day return; fast-track to hard gate if score >0.8 pick triggers Beneish flag or >15% drawdown.",
        "notes": "Composite of altman_z (40%), interest_coverage (35%), fcf_neg_yrs (25%); NULL components redistribute weight proportionally.",
    },
    # --- Phase 3: Longevity / Lindy ---
    "revenue_cagr_10y": {
        "status": "watch",
        "sleeve": "Growth",
        "shadow_since": "2026-05-23",
        "gate_date": None,  # structural ~40% null (requires 11+ years); cannot pass <30% promotion threshold
        "promotion_criteria": "Supplement revenue_cagr_5y in Growth sleeve once null rate drops below 25% and 10y CAGR shows stronger return correlation than 5y.",
        "notes": "NULL when <11 years of income statement data available. Structurally high null rate — gate date removed until live-run null rate stabilises below 30%.",
    },
    "eps_cagr_10y": {
        "status": "watch",
        "sleeve": "Growth",
        "shadow_since": "2026-05-23",
        "gate_date": None,  # structural ~40% null; same issue as revenue_cagr_10y
        "promotion_criteria": "Promote alongside revenue_cagr_10y; same null-rate and return-correlation criteria.",
        "notes": "NULL when <11 years of income statement data available. Structurally high null rate — gate date removed until live-run null rate stabilises below 30%.",
    },
    "fcf_cagr_10y": {
        "status": "watch",
        "sleeve": "Growth",
        "shadow_since": "2026-05-23",
        "gate_date": None,  # structural ~40% null; same issue as revenue_cagr_10y
        "promotion_criteria": "Promote alongside revenue_cagr_10y; same null-rate and return-correlation criteria.",
        "notes": "NULL when <11 years of cash flow data available. Structurally high null rate — gate date removed until live-run null rate stabilises below 30%.",
    },
    "lindy_score": {
        "status": "watch",
        "sleeve": "Growth",
        "shadow_since": "2026-05-23",
        "gate_date": None,  # depends on 10y CAGR availability; gate removed alongside 10y signals
        "promotion_criteria": "Promote as Growth sleeve supplementary signal if lindy_score >0.7 predicts above-median 90-day return with statistical significance (p<0.10) over ≥60 observations.",
        "notes": "Composite: 40% history-length bonus + 60% no-surge score; collapses to 20% history-only when no 10y data. Gate date removed — re-evaluate once 10y CAGR null rate drops below 30%.",
    },
    # --- Phase 4: Management Alignment ---
    "sbc_to_revenue": {
        "status": "shadow",
        "sleeve": "Governance",
        "shadow_since": "2026-05-23",
        "gate_date": "2026-07-04",
        "promotion_criteria": "Promote as Governance sleeve penalty if sbc_to_revenue >5% correlates with underperformance vs. benchmark over ≥30 observations.",
        "notes": "Sourced from key_metrics[0].stockBasedCompensationToRevenue; proxy for shareholder dilution risk.",
    },
    "insider_net_shares_3m": {
        "status": "shadow",
        "sleeve": "Governance",
        "shadow_since": "2026-05-23",
        "gate_date": "2026-07-04",
        "promotion_criteria": "Promote as Governance sleeve signal if net buying (>0) predicts above-median 90-day return over ≥30 observations.",
        "notes": "totalAcquired minus totalDisposed from /insider-trading/statistics; positive = net buying.",
    },
    "ceo_pay_ratio": {
        "status": "shadow",
        "sleeve": "Governance",
        "shadow_since": "2026-05-23",
        "gate_date": "2026-07-04",
        "promotion_criteria": "Promote as Governance sleeve penalty if ceo_pay_ratio >1% correlates with underperformance; demote/park if null rate remains >40% after 60 observations.",
        "notes": "CEO total comp / revenue * 100; NULL if no CEO match in /governance-executive-compensation or revenue is zero.",
    },
    # --- §3: Beneish growth exemption (log-only) ---
    "beneish_growth_exempt": {
        "status": "log_only",
        "sleeve": "Screener",
        "shadow_since": "2026-06-05",
        "gate_date": "2026-07-17",
        "promotion_criteria": "Flip BENEISH_GROWTH_EXEMPT_LIVE to True only after >=30 logged exemptions confirm the admitted names are legitimate high-growth firms (not manipulators) and show non-negative forward alpha vs. the rejected baseline.",
        "notes": "Relaxes the M>-1.78 reject to M<=-1.50 for stocks with revenue_cagr_3y>0.25 AND stable/expanding gross margin. Log-only: detected + counted in run_metrics.csv, still rejected; no live-pick change and no DB column until promotion.",
    },
    # --- §4 / P4: Revenue one-time-spike protection (shadow) ---
    "revenue_consistency_flag": {
        "status": "shadow",
        "sleeve": "Growth",
        "shadow_since": "2026-06-06",
        "gate_date": "2026-07-18",
        "promotion_criteria": "Promote the -10pt growth penalty into live scoring only if spike-flagged picks show materially worse 60-day forward return than non-flagged picks over >=30 observations.",
        "notes": "Flag set when any single-year YoY revenue growth > REVENUE_SPIKE_THRESHOLD x revenue_cagr_5y (guarded for cagr<=0 / non-positive base). growth_score_spike_adj = growth_score - REVENUE_SPIKE_PENALTY is shadow-only; live growth_score / total_score unchanged.",
    },
}
