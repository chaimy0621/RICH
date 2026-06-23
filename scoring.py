"""Frozen sleeve scoring: Quality + Growth + Value/Risk -> total_score 0-100.

Inputs must already pass screener.py (rule 7 ensures no null in 8 core metrics).
Optional metrics (f_score, novy_marx_gp_assets, all Phase 3.2/3.3 factors) fall
back gracefully — sleeve averages are computed over available metrics only.
"""
from __future__ import annotations

import statistics as _statistics
from typing import Any

# (lo, hi, higher_is_better) — ranges used only by V1 linear scorer (diagnostic)
RULES = {
    # --- Quality (core) ---
    "returnOnEquity":               (0.00, 0.30,  True),
    "returnOnAssets":               (0.00, 0.15,  True),
    "grossProfitMargin":            (0.10, 0.60,  True),
    "operatingProfitMargin":        (0.00, 0.30,  True),
    "operatingCashFlowToNetIncome": (0.50, 1.50,  True),
    # --- Quality (Phase 3.1, optional) ---
    "f_score":                      (0,    9,     True),
    "novy_marx_gp_assets":          (0.10, 0.60,  True),
    # --- Growth (Phase 3.2) ---
    "revenueGrowth":                (-0.05, 0.25, True),
    "revenue_cagr_5y":              (-0.10, 0.30, True),
    "eps_cagr_5y":                  (-0.10, 0.30, True),
    "fcf_cagr_5y":                  (-0.10, 0.30, True),
    "asset_growth":                 (-0.10, 0.25, False),  # penalty: lower = better
    # --- Value/Risk (Phase 3.3) ---
    "debtToEquityRatio":            (0.00, 2.50,  False),
    "priceEarningsRatio":           (5.00, 40.00, False),
    "ev_ebitda":                    (5.0,  40.0,  False),
    "ps_ratio":                     (0.5,  10.0,  False),
    "pe_vs_own_history":            (0.5,  2.0,   False),  # < 1 = cheaper than own history
}

# Metrics that may legitimately be None; sleeve avg falls back to available only.
_OPTIONAL = frozenset({
    "f_score", "novy_marx_gp_assets",
    # Phase 3.2 Growth
    "revenue_cagr_5y", "eps_cagr_5y", "fcf_cagr_5y", "asset_growth",
    # Phase 3.3 Value/Risk
    "ev_ebitda", "ps_ratio", "pe_vs_own_history",
})

# Metrics where values at or below the floor are economically meaningless and
# are excluded from z-score pooling and per-candidate scoring (stored as-is).
_SCORE_FLOOR: dict[str, float] = {
    "ev_ebitda": 0.0,  # negative EV/EBITDA means negative EBITDA — not comparable
}

QUALITY_METRICS = [
    "returnOnEquity", "returnOnAssets", "grossProfitMargin",
    "operatingProfitMargin", "operatingCashFlowToNetIncome",
    "f_score", "novy_marx_gp_assets",
]
GROWTH_METRICS = [
    "revenueGrowth",
    "revenue_cagr_5y", "eps_cagr_5y", "fcf_cagr_5y",
    "asset_growth",
]
VALUE_RISK_METRICS = [
    "debtToEquityRatio", "priceEarningsRatio",
    "ev_ebitda", "ps_ratio", "pe_vs_own_history",
]

WEIGHTS = {"quality": 0.45, "growth": 0.25, "value_risk": 0.30}  # V1 — kept for diagnostic

# Phase 4: cross-sectional z-score weights
WEIGHTS_V2 = {"quality": 0.35, "growth": 0.40, "value_risk": 0.25}
SCORING_VERSION = "zscore_v2"
Z_CLIP = 3.0  # clip raw z before mapping to [0, 100]


def normalize(v: float, lo: float, hi: float, higher: bool) -> float:
    x = max(lo, min(hi, v))
    n = (x - lo) / (hi - lo) * 100
    return n if higher else 100 - n


def _sleeve_avg(norms: dict[str, float | None], metrics: list[str]) -> float:
    """Average normalized scores for a sleeve, ignoring None entries."""
    vals = [norms[m] for m in metrics if norms.get(m) is not None]
    return round(sum(vals) / len(vals), 1) if vals else 0.0


def spike_adjusted_growth(growth_score: float, revenue_consistency_flag,
                          penalty: float) -> float:
    """Shadow-only growth-sleeve adjustment for one-time revenue spikes (P4 / §4).

    Returns ``growth_score - penalty`` when the spike flag is set, else
    ``growth_score`` unchanged (clamped to >= 0). Pure and side-effect-free: the
    live growth_score and total_score are never modified — the caller stores the
    result in a separate shadow column (growth_score_spike_adj).
    """
    adj = growth_score - penalty if revenue_consistency_flag else growth_score
    return round(max(0.0, adj), 1)


def score(factors: dict[str, Any]) -> dict[str, Any]:
    """Compute normalized metric scores, sleeve scores, total, and drivers."""
    norms: dict[str, float | None] = {}
    for name, (lo, hi, higher) in RULES.items():
        v = factors.get(name)
        if v is None:
            if name not in _OPTIONAL:
                raise ValueError(f"missing metric {name} for {factors.get('symbol')}")
            norms[name] = None
        else:
            norms[name] = round(normalize(float(v), lo, hi, higher), 1)

    quality = _sleeve_avg(norms, QUALITY_METRICS)
    growth = _sleeve_avg(norms, GROWTH_METRICS)
    value_risk = _sleeve_avg(norms, VALUE_RISK_METRICS)

    total = (
        WEIGHTS["quality"] * quality
        + WEIGHTS["growth"] * growth
        + WEIGHTS["value_risk"] * value_risk
    )
    total = round(max(0.0, min(100.0, total)), 1)

    # Drivers: ranked over non-None normalized scores only.
    ranked = sorted(
        ((k, v) for k, v in norms.items() if v is not None),
        key=lambda kv: kv[1],
        reverse=True,
    )
    top_2 = [k for k, _ in ranked[:2]]
    bottom_2 = [k for k, _ in ranked[-2:][::-1]]

    return {
        "symbol": factors.get("symbol"),
        "norms": norms,
        "quality_score": quality,
        "growth_score": growth,
        "value_risk_score": value_risk,
        "total_score": total,
        "top_2_drivers": top_2,
        "bottom_2_drivers": bottom_2,
    }


def score_cross_sectional(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pass 2: cross-sectional z-score scoring over all passing candidates.

    Each candidate dict must have "symbol" and "factors" (from _screen_only).
    Returns the same list extended with two new keys per candidate:
      "scoring"    — z-score, WEIGHTS_V2 Q=35/G=40/V=25  (production)
      "scoring_v1" — linear-range, WEIGHTS V1 Q=45/G=25/V=30 (diagnostic)

    Z-score pipeline per metric:
      1. Pool all candidate values to get cross-sectional mean + std.
      2. z = (v − mean) / std; flip sign if lower_is_better.
      3. Clip to [−Z_CLIP, +Z_CLIP].
      4. Map to [0, 100]: (z + Z_CLIP) / (2 × Z_CLIP) × 100.
      5. Sleeve averages (None-safe) → blend with WEIGHTS_V2.
    """
    n = len(candidates)
    if n == 0:
        return candidates

    # ── Step 1: pool raw values per metric ────────────────────────────────
    raw: dict[str, list[float]] = {name: [] for name in RULES}
    for c in candidates:
        f = c["factors"]
        for name in RULES:
            v = f.get(name)
            if v is not None:
                try:
                    fv = float(v)
                    floor = _SCORE_FLOOR.get(name)
                    if floor is None or fv > floor:
                        raw[name].append(fv)
                except (TypeError, ValueError):
                    pass

    # ── Step 2: cross-sectional mean and std per metric ───────────────────
    cs_mu:    dict[str, float] = {}
    cs_sigma: dict[str, float] = {}
    for name, vals in raw.items():
        if len(vals) >= 2:
            cs_mu[name]    = _statistics.mean(vals)
            cs_sigma[name] = _statistics.stdev(vals)
        elif len(vals) == 1:
            cs_mu[name]    = vals[0]
            cs_sigma[name] = 0.0
        else:
            cs_mu[name]    = 0.0
            cs_sigma[name] = 0.0

    # ── Steps 3–5: score each candidate ───────────────────────────────────
    results: list[dict[str, Any]] = []
    for c in candidates:
        f = c["factors"]

        norms_z: dict[str, float | None] = {}
        for name, (_lo, _hi, higher) in RULES.items():
            v = f.get(name)
            if v is None:
                norms_z[name] = None
                continue
            fv = float(v)
            floor = _SCORE_FLOOR.get(name)
            if floor is not None and fv <= floor:
                norms_z[name] = None
                continue
            sigma = cs_sigma.get(name, 0.0)
            z = (fv - cs_mu.get(name, 0.0)) / sigma if sigma > 0.0 else 0.0
            if not higher:
                z = -z
            z_c = max(-Z_CLIP, min(Z_CLIP, z))
            norms_z[name] = round((z_c + Z_CLIP) / (2.0 * Z_CLIP) * 100.0, 1)

        quality_z    = _sleeve_avg(norms_z, QUALITY_METRICS)
        growth_z     = _sleeve_avg(norms_z, GROWTH_METRICS)
        value_risk_z = _sleeve_avg(norms_z, VALUE_RISK_METRICS)
        total_z = round(max(0.0, min(100.0,
            WEIGHTS_V2["quality"]      * quality_z
            + WEIGHTS_V2["growth"]     * growth_z
            + WEIGHTS_V2["value_risk"] * value_risk_z,
        )), 1)

        ranked = sorted(
            ((k, s) for k, s in norms_z.items() if s is not None),
            key=lambda kv: kv[1], reverse=True,
        )
        top_2    = [k for k, _ in ranked[:2]]
        bottom_2 = [k for k, _ in ranked[-2:][::-1]]

        results.append({
            **c,
            "scoring": {
                "symbol":           c["symbol"],
                "norms":            norms_z,
                "quality_score":    quality_z,
                "growth_score":     growth_z,
                "value_risk_score": value_risk_z,
                "total_score":      total_z,
                "top_2_drivers":    top_2,
                "bottom_2_drivers": bottom_2,
                "universe_n":       n,
            },
            "scoring_v1": score(f),   # old linear scoring for diagnostic
        })

    return results
