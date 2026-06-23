"""Per-ticker factor extraction. Pulls only the fields needed for screening + scoring.

Returns the 8 frozen metrics, historical_years_available, raw fields required
by per-ticker hard rules (revenue, netIncome, operatingCashFlow), and shadow
factors for Phase 3 (Growth + Value/Risk) that are stored but not yet scored.
"""
from __future__ import annotations

import json
import threading
from datetime import date as _date
from pathlib import Path
from typing import Any

from config import FUNDAMENTAL_YEARS, REVENUE_SPIKE_THRESHOLD
from data.fetcher import FMPClient, FMPError
import piotroski

# ── Local FMP response cache (keyed by symbol, scoped to one calendar day) ───
_CACHE_DIR  = Path(__file__).parent / "data"
_CACHE_LOCK = threading.Lock()
_cache_today: str = ""
_cache_data:  dict = {}


def _cache_path(today: str) -> Path:
    return _CACHE_DIR / f"fmp_cache_{today}.json"


def _load_day(today: str) -> None:
    """Read today's cache file into _cache_data. Must be called with _CACHE_LOCK held."""
    global _cache_today, _cache_data
    if _cache_today == today:
        return
    p = _cache_path(today)
    _cache_data  = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    _cache_today = today


def _get_cached(symbol: str, today: str) -> dict | None:
    with _CACHE_LOCK:
        _load_day(today)
        return _cache_data.get(symbol)


def _put_cached(symbol: str, raw: dict, today: str) -> None:
    with _CACHE_LOCK:
        _load_day(today)
        _cache_data[symbol] = raw
        _cache_path(today).write_text(
            json.dumps(_cache_data, separators=(",", ":")),
            encoding="utf-8",
        )


METRIC_NAMES = [
    "returnOnEquity",
    "returnOnAssets",
    "grossProfitMargin",
    "operatingProfitMargin",
    "operatingCashFlowToNetIncome",
    "revenueGrowth",
    "debtToEquityRatio",
    "priceEarningsRatio",
]


def _cagr(start: Any, end: Any, n: int) -> float | None:
    """Compound annual growth rate from start to end over n years.
    Returns None when either value is non-positive, or n < 1.
    """
    try:
        s, e = float(start), float(end)
    except (TypeError, ValueError):
        return None
    if n < 1 or s <= 0 or e <= 0:
        return None
    return round((e / s) ** (1 / n) - 1, 6)


def _row_fcf(row: dict[str, Any]) -> float:
    """Free cash flow from a cash-flow row. Capex is negative in FMP convention."""
    return (row.get("operatingCashFlow") or 0) + (row.get("capitalExpenditure") or 0)


def _pick(d: dict[str, Any], *keys: str) -> Any:
    for k in keys:
        if k in d and d[k] is not None:
            return d[k]
    return None


def _ruin_risk_composite(
    altman_z: float | None,
    interest_cov: float | None,
    fcf_neg_yrs: int | None,
) -> float | None:
    """Composite ruin-risk score [0.0, 1.0]. Higher = more ruin risk.

    Weights: 40% Altman Z-Score (inverted), 35% interest coverage (inverted),
    25% FCF negative years. Missing components have their weight redistributed
    proportionally among available inputs (same pattern as _sleeve_avg in scoring.py).
    Returns None only when all three inputs are None.
    """
    def _norm_inv(v: float | None, floor: float, cap: float) -> float | None:
        if v is None:
            return None
        clamped = max(floor, min(cap, float(v)))
        return 1.0 - (clamped - floor) / (cap - floor)

    components = [
        (0.40, _norm_inv(altman_z, 1.8, 5.0)),
        (0.35, _norm_inv(interest_cov, 1.5, 15.0)),
        (0.25, (fcf_neg_yrs / 5.0) if fcf_neg_yrs is not None else None),
    ]
    available = [(w, v) for w, v in components if v is not None]
    if not available:
        return None
    total_w = sum(w for w, _ in available)
    score = sum((w / total_w) * v for w, v in available)
    return round(max(0.0, min(1.0, score)), 4)


def _lindy_score(
    history_years: int,
    rev_5y: float | None, rev_10y: float | None,
    eps_5y: float | None, eps_10y: float | None,
    fcf_5y: float | None, fcf_10y: float | None,
) -> float:
    """Longevity/durability composite [0, 1]. Higher = more Lindy.

    40%  history-length bonus  — (years − 5) / 10, capped at 1.0
    60%  no-recent-surge score — penalises 5y CAGR >> 10y CAGR (unsustained recent burst);
         rewards when 5y ≤ 10y (growth is durable or accelerating long-term).
         Averaged over available pairs. When no 10y pairs exist, weight collapses to 20%.
    """
    hist_score = max(0.0, min(1.0, (history_years - 5) / 10.0))

    pair_scores = []
    for c5, c10 in [(rev_5y, rev_10y), (eps_5y, eps_10y), (fcf_5y, fcf_10y)]:
        if c5 is None or c10 is None:
            continue
        if c5 <= c10:
            pair_scores.append(1.0)
        else:
            # c5 > c10: recent surge. Penalty grows as the gap widens relative to long-run rate.
            base = max(abs(c10), 0.05)
            surge = min(1.0, (c5 - c10) / base)
            pair_scores.append(1.0 - surge)

    if pair_scores:
        no_surge = sum(pair_scores) / len(pair_scores)
        score = 0.40 * hist_score + 0.60 * no_surge
    else:
        # No 10y CAGR available — score on history alone, discounted weight
        score = 0.20 * hist_score

    return round(max(0.0, min(1.0, score)), 4)


def classify_history(years: int) -> str:
    if years < 5:
        return "reject"
    if years < 10:
        return "acceptable"
    return "preferred"


def extract(symbol: str, client: FMPClient | None = None,
            years: int = FUNDAMENTAL_YEARS) -> dict[str, Any]:
    """Fetch all data needed for screening + scoring for one ticker.

    Returns a dict with: symbol, fiscalYear, historical_years_available,
    history_class, latest revenue/netIncome/operatingCashFlow, the 8 metrics,
    and raw multi-year rows (_inc_rows, _bs_rows, _cf_rows) for Beneish.
    Raises FMPError on fetch failure.
    """
    today  = _date.today().isoformat()
    cached = _get_cached(symbol, today)
    if cached is not None:
        inc = cached["inc"]
        bs  = cached["bs"]
        cf  = cached["cf"]
        r   = cached["r"]
        km  = cached["km"]
        g   = cached["g"]
    else:
        c   = client or FMPClient()
        inc = c.income_statement(symbol, years=years) or []
        bs  = c.balance_sheet(symbol, years=years)    or []
        cf  = c.cash_flow(symbol, years=years)        or []
        r   = c.ratios(symbol, years=years)           or []
        km  = c.key_metrics(symbol, years=years)      or []
        g   = c.financial_growth(symbol, years=years) or []
        _put_cached(symbol, {"inc": inc, "bs": bs, "cf": cf,
                             "r": r, "km": km, "g": g}, today)

    inc0 = inc[0] if inc else {}
    bs0  = bs[0] if bs else {}
    cf0 = cf[0] if cf else {}
    r0 = r[0] if r else {}
    km0 = km[0] if km else {}
    g0 = g[0] if g else {}

    rev = inc0.get("revenue")
    ni = inc0.get("netIncome")
    ocf = cf0.get("operatingCashFlow")
    ocf_ni = (ocf / ni) if (ocf is not None and ni and ni > 0) else None

    history_count = len(inc)

    # ── Phase 3.2 Growth shadow factors ─────────────────────────────────────
    # Years spanned: up to 5, bounded by available rows (screener guarantees ≥5).
    _n     = min(5, len(inc) - 1)
    _n_cf  = min(5, len(cf)  - 1)

    revenue_cagr_5y = _cagr(
        inc[_n].get("revenue") if _n > 0 else None,
        inc0.get("revenue"), _n,
    )
    _eps0 = inc0.get("eps") or inc0.get("epsDiluted")
    _epsN = (inc[_n].get("eps") or inc[_n].get("epsDiluted")) if _n > 0 else None
    eps_cagr_5y = _cagr(_epsN, _eps0, _n)

    fcf_cagr_5y = (
        _cagr(_row_fcf(cf[_n_cf]), _row_fcf(cf0), _n_cf)
        if _n_cf > 0 else None
    )
    asset_growth = g0.get("assetGrowth")   # 1-year YoY; used as penalty in scoring

    # ── §3 Beneish growth-exemption inputs (log-only; not scored, not stored) ─
    # 3-year revenue CAGR + gross-margin trend gate the high-growth exemption.
    # grossProfitMargin is a fraction, so 1.0pp tolerance == 0.01.
    _n3 = min(3, len(inc) - 1)
    revenue_cagr_3y = (
        _cagr(inc[_n3].get("revenue"), inc0.get("revenue"), _n3) if _n3 >= 3 else None
    )
    _gm0 = r0.get("grossProfitMargin")
    _gm1 = r[1].get("grossProfitMargin") if len(r) > 1 else None
    gross_margin_stable = bool(
        _gm0 is not None and _gm1 is not None and float(_gm0) >= float(_gm1) - 0.01
    )

    # ── P4 Revenue one-time-spike protection (shadow) ───────────────────────
    # Flag when any single-year YoY revenue growth exceeds SPIKE x the smoothed
    # 5y CAGR — a one-off jump (contract, divestiture, accounting) that would
    # otherwise inflate the Growth sleeve. Guards: revenue_cagr_5y must be > 0;
    # each YoY pair's base (older) revenue must be > 0.
    revenue_consistency_flag = 0
    revenue_spike_ratio = None
    if revenue_cagr_5y is not None and revenue_cagr_5y > 0:
        _yoys = []
        for _i in range(_n):  # consecutive pairs within the 5y window
            _new = inc[_i].get("revenue")
            _old = inc[_i + 1].get("revenue")
            if _new is not None and _old is not None and float(_old) > 0:
                _yoys.append((float(_new) - float(_old)) / float(_old))
        if _yoys:
            revenue_spike_ratio = round(max(_yoys) / revenue_cagr_5y, 4)
            if revenue_spike_ratio > REVENUE_SPIKE_THRESHOLD:
                revenue_consistency_flag = 1

    # ── Taleb Phase 3: Longevity / Lindy — 10-year CAGR windows ─────────────
    # Guard: require _n10 == 10 (i.e., ≥ 11 rows) for a valid 10-year span.
    _n10    = min(10, len(inc) - 1)
    _n10_cf = min(10, len(cf)  - 1)

    revenue_cagr_10y = (
        _cagr(inc[_n10].get("revenue"), inc0.get("revenue"), _n10)
        if _n10 >= 10 else None
    )
    _epsN_10 = (inc[_n10].get("eps") or inc[_n10].get("epsDiluted")) if _n10 >= 10 else None
    eps_cagr_10y = _cagr(_epsN_10, _eps0, _n10) if _n10 >= 10 else None
    fcf_cagr_10y = (
        _cagr(_row_fcf(cf[_n10_cf]), _row_fcf(cf0), _n10_cf)
        if _n10_cf >= 10 else None
    )

    lindy_score = _lindy_score(
        history_count,
        revenue_cagr_5y, revenue_cagr_10y,
        eps_cagr_5y,     eps_cagr_10y,
        fcf_cagr_5y,     fcf_cagr_10y,
    )

    # ── Phase 3.3 Value/Risk shadow factors ──────────────────────────────────
    ev_ebitda    = km0.get("evToEBITDA")
    ps_ratio     = r0.get("priceToSalesRatio")
    peg_ratio    = r0.get("priceToEarningsGrowthRatio")
    sbc_to_revenue = km0.get("stockBasedCompensationToRevenue")

    # PE vs own history: current PE / median of prior years' PE.
    # Require ≥3 positive historical PEs to produce a stable median.
    # Value < 1 = cheaper than own history; > 1 = pricier than history.
    _pe_cur  = r0.get("priceToEarningsRatio")
    _pe_hist = [
        float(row["priceToEarningsRatio"])
        for row in r[1:]
        if row.get("priceToEarningsRatio")
        and float(row.get("priceToEarningsRatio", 0) or 0) > 0
    ]
    if len(_pe_hist) >= 3 and _pe_cur and float(_pe_cur or 0) > 0:
        _med = sorted(_pe_hist)[len(_pe_hist) // 2]
        pe_vs_own_history = round(float(_pe_cur) / _med, 4) if _med else None
    else:
        pe_vs_own_history = None

    # ── Taleb Phase 1: Fragility / Hidden Asymmetry shadow factors ──────────────
    interest_coverage = r0.get("interestCoverageRatio")

    _cf5 = cf[:min(5, len(cf))]
    fcf_negative_years_5 = (
        sum(1 for row in _cf5 if _row_fcf(row) < 0)
        if _cf5 else None
    )

    # Altman Z-Score (5-factor public-company model).
    # X4 uses market cap / total liabilities — asset-light companies may score
    # low despite being healthy; treat as shadow-only until distribution reviewed.
    _ta  = bs0.get("totalAssets")
    _tca = bs0.get("totalCurrentAssets")
    _tcl = bs0.get("totalCurrentLiabilities")
    _wc  = (_tca - _tcl) if (_tca is not None and _tcl is not None) else None
    _re  = bs0.get("retainedEarnings")
    _ebit = inc0.get("ebit") or inc0.get("operatingIncome")
    _mcap = km0.get("marketCap")
    _tl   = bs0.get("totalLiabilities")
    if (
        _ta and _ta > 0
        and _wc is not None
        and _re is not None
        and _ebit is not None
        and _mcap and _mcap > 0
        and _tl and _tl > 0
        and rev and rev > 0
    ):
        altman_z_score = round(
            1.2 * (_wc / _ta)
            + 1.4 * (_re / _ta)
            + 3.3 * (_ebit / _ta)
            + 0.6 * (_mcap / _tl)
            + 1.0 * (rev / _ta),
            4,
        )
    else:
        altman_z_score = None

    # ── Taleb Phase 2: Ruin Risk composite ──────────────────────────────────────
    ruin_risk_score = _ruin_risk_composite(
        altman_z_score, interest_coverage, fcf_negative_years_5
    )

    # ── §7 Lookahead / data-vintage audit (P1; shadow) ──────────────────────────
    # Age of the most recent annual filing. `fillingDate` is FMP's filing-date
    # field (note FMP's spelling); fall back to filingDate / acceptedDate. NOT
    # inc0["date"], which is the fiscal period END, not when the data was filed.
    latest_filing_date = _pick(inc0, "fillingDate", "filingDate", "acceptedDate")
    data_vintage_days = None
    if latest_filing_date:
        try:
            _filed = _date.fromisoformat(str(latest_filing_date)[:10])
            data_vintage_days = (_date.fromisoformat(today) - _filed).days
        except ValueError:
            data_vintage_days = None
    # Annual cadence: a healthy filer reports within ~12 months. Flag stale ONLY
    # when no new annual filing for > ~455 days (≈15 months) — a genuinely
    # delinquent/halted filer. Unknown vintage (missing/unparseable date) is NOT
    # flagged (fail open here; the point-in-time backtest guard fails closed).
    stale_data_flag = 1 if (data_vintage_days is not None and data_vintage_days > 455) else 0

    return {
        "symbol": symbol,
        "fiscalYear": inc0.get("fiscalYear") or r0.get("fiscalYear"),
        "historical_years_available": history_count,
        "history_class": classify_history(history_count),
        "revenue": rev,
        "netIncome": ni,
        "operatingCashFlow": ocf,
        # 8 frozen metrics
        "returnOnEquity": _pick(r0, "returnOnEquity") or _pick(km0, "returnOnEquity"),
        "returnOnAssets": _pick(r0, "returnOnAssets") or _pick(km0, "returnOnAssets"),
        "grossProfitMargin": _pick(r0, "grossProfitMargin"),
        "operatingProfitMargin": _pick(r0, "operatingProfitMargin"),
        "operatingCashFlowToNetIncome": ocf_ni,
        "revenueGrowth": _pick(g0, "revenueGrowth"),
        "debtToEquityRatio": _pick(r0, "debtToEquityRatio"),
        "priceEarningsRatio": _pick(
            r0, "priceToEarningsRatio", "priceEarningsRatio", "peRatio"
        ),
        # raw multi-year rows for Beneish M-Score (newest-first)
        "_inc_rows": inc,
        "_bs_rows": bs,
        "_cf_rows": cf,
        # Piotroski F-Score (Phase 3 Quality)
        "f_score": (
            piotroski.f_score(
                piotroski.compute_f_signals(inc[0], inc[1], bs[0], bs[1], cf[0])
            )
            if len(inc) >= 2 and len(bs) >= 2 and cf
            else None
        ),
        # Novy-Marx gross profitability (Phase 3 Quality)
        "novy_marx_gp_assets": (
            (inc0.get("grossProfit") / bs0["totalAssets"])
            if (inc0.get("grossProfit") is not None
                and bs0.get("totalAssets"))
            else None
        ),
        # Phase 3.2 — Growth shadow factors (stored; not yet in scoring)
        "revenue_cagr_5y": revenue_cagr_5y,
        "eps_cagr_5y":     eps_cagr_5y,
        "fcf_cagr_5y":     fcf_cagr_5y,
        "asset_growth":    asset_growth,
        # §3 Beneish growth-exemption inputs (log-only; not scored/stored)
        "revenue_cagr_3y":     revenue_cagr_3y,
        "gross_margin_stable": gross_margin_stable,
        # P4 Revenue one-time-spike protection (shadow)
        "revenue_consistency_flag": revenue_consistency_flag,
        "revenue_spike_ratio":      revenue_spike_ratio,
        # Taleb Phase 3 — Longevity / Lindy shadow factors
        "revenue_cagr_10y": revenue_cagr_10y,
        "eps_cagr_10y":     eps_cagr_10y,
        "fcf_cagr_10y":     fcf_cagr_10y,
        "lindy_score":      lindy_score,
        # Phase 3.3 — Value/Risk shadow factors (stored; not yet in scoring)
        "ev_ebitda":          ev_ebitda,
        "ps_ratio":           ps_ratio,
        "peg_ratio":          peg_ratio,
        "pe_vs_own_history":  pe_vs_own_history,
        # Taleb Phase 4 — Management Alignment shadow factors (zero extra API calls)
        "sbc_to_revenue": sbc_to_revenue,
        # Taleb Phase 1 — Fragility / Hidden Asymmetry shadow factors
        "altman_z_score":       altman_z_score,
        "interest_coverage":    interest_coverage,
        "fcf_negative_years_5": fcf_negative_years_5,
        # Taleb Phase 2 — Ruin Risk composite (shadow; path to VR sleeve)
        "ruin_risk_score": ruin_risk_score,
        # §7 Lookahead / data-vintage audit (P1; shadow)
        "latest_filing_date": latest_filing_date,
        "data_vintage_days":  data_vintage_days,
        "stale_data_flag":    stale_data_flag,
    }
