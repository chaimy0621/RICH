"""End-to-end pipeline: universe -> screen -> score -> rank -> cooldown -> store."""
from __future__ import annotations

import logging
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import date
from pathlib import Path
from typing import Any

from config import (
    CACHE_DB, COOLDOWN_DAYS, TOP_N, VALUATION_WEIGHT, REVENUE_SPIKE_PENALTY,
)
from cooldown import apply_cooldown
from data import storage
from data.fetcher import FMPClient
from scoring import (
    SCORING_VERSION, WEIGHTS_V2, score_cross_sectional, spike_adjusted_growth,
)
from screener import screen_ticker
from valuation import apply_valuation
import universe

log = logging.getLogger(__name__)

RAW_TOP = 20
MAX_WORKERS = 6
_MAX_WORKERS_CAP = 20

# Identifies the active scoring weight set; stored in research_universe for audit.
_RESEARCH_RULE_VERSION = "WEIGHTS_V2"


def _short_reason(s: dict[str, Any]) -> str:
    return (
        f"Q={s['quality_score']:.0f} G={s['growth_score']:.0f} "
        f"VR={s['value_risk_score']:.0f}; +{s['top_2_drivers'][0]} "
        f"-{s['bottom_2_drivers'][0]}"
    )[:120]


def _screen_only(uni_row: dict[str, Any], client: FMPClient) -> dict[str, Any]:
    """Pass 1 worker: fetch + screen; returns raw factors only (no scoring)."""
    sym = uni_row["symbol"]
    s = screen_ticker(sym, client)
    if not s["pass"]:
        return {"pass": False, "symbol": sym, "reason": s["reason"],
                # §3: propagate the log-only exemption flag for run-metrics counting
                "beneish_growth_exempt": s.get("beneish_growth_exempt", False)}
    return {
        "pass": True,
        "symbol": sym,
        "company_name": uni_row.get("companyName"),
        # P0: sector/industry available on the candidate BEFORE [2b/7] scoring.
        "sector": uni_row.get("sector"),
        "industry": uni_row.get("industry"),
        "factors": s["factors"],
    }


def _build_research_row(
    c: dict[str, Any],
    run_date: str,
    universe_rank: int,
    in_top20: bool,
    is_backfill: int = 0,
) -> dict[str, Any]:
    sc = c["scoring"]
    f  = c["factors"]
    return {
        "run_date":        run_date,
        "symbol":          c["symbol"],
        "total_score":     sc["total_score"],
        "quality_score":   sc["quality_score"],
        "growth_score":    sc["growth_score"],
        "value_risk_score": sc["value_risk_score"],
        "universe_n":      sc.get("universe_n"),
        "universe_rank":   universe_rank,
        "in_top20":        1 if in_top20 else 0,
        "historical_years_available": f.get("historical_years_available"),
        "m_score":      f.get("m_score"),
        "beneish_flag": f.get("beneish_flag"),
        # Quality
        "returnOnEquity":               f.get("returnOnEquity"),
        "returnOnAssets":               f.get("returnOnAssets"),
        "grossProfitMargin":            f.get("grossProfitMargin"),
        "operatingProfitMargin":        f.get("operatingProfitMargin"),
        "operatingCashFlowToNetIncome": f.get("operatingCashFlowToNetIncome"),
        "f_score":             f.get("f_score"),
        "novy_marx_gp_assets": f.get("novy_marx_gp_assets"),
        # Growth
        "revenueGrowth":   f.get("revenueGrowth"),
        "revenue_cagr_5y": f.get("revenue_cagr_5y"),
        "eps_cagr_5y":     f.get("eps_cagr_5y"),
        "fcf_cagr_5y":     f.get("fcf_cagr_5y"),
        "asset_growth":    f.get("asset_growth"),
        # Value/Risk
        "debtToEquityRatio":  f.get("debtToEquityRatio"),
        "priceEarningsRatio": f.get("priceEarningsRatio"),
        "ev_ebitda":          f.get("ev_ebitda"),
        "ps_ratio":           f.get("ps_ratio"),
        "pe_vs_own_history":  f.get("pe_vs_own_history"),
        # Management alignment (no extra API calls; already in key_metrics cache)
        "sbc_to_revenue": f.get("sbc_to_revenue"),
        # P0: sector/industry plumbing (uni_row sourced, captured pre-[3b/7])
        "sector":   c.get("sector"),
        "industry": c.get("industry"),
        # §7 Lookahead / data-vintage audit (P1; shadow)
        "data_vintage_days":  f.get("data_vintage_days"),
        "latest_filing_date": f.get("latest_filing_date"),
        "stale_data_flag":    f.get("stale_data_flag", 0),
        # P4 revenue one-time-spike protection (shadow)
        "revenue_consistency_flag": f.get("revenue_consistency_flag", 0),
        "revenue_spike_ratio":      f.get("revenue_spike_ratio"),
        "growth_score_spike_adj":   spike_adjusted_growth(
            sc["growth_score"], f.get("revenue_consistency_flag"), REVENUE_SPIKE_PENALTY
        ),
        # Audit / reproducibility
        "rule_version":         _RESEARCH_RULE_VERSION,
        "normalization_method": SCORING_VERSION,
        "is_backfill":          is_backfill,
    }


def _build_row(c: dict[str, Any], run_date: str, rank: int) -> dict[str, Any]:
    sc    = c["scoring"]
    sc_v1 = c.get("scoring_v1") or {}
    f     = c["factors"]
    return {
        "run_date": run_date,
        "symbol": c["symbol"],
        "company_name": c.get("company_name"),
        # P0: sector/industry plumbing
        "sector": c.get("sector"),
        "industry": c.get("industry"),
        "total_score": sc["total_score"],
        "quality_score": sc["quality_score"],
        "growth_score": sc["growth_score"],
        "value_risk_score": sc["value_risk_score"],
        "recommendation_rank": rank,
        "is_daily_pick": c.get("is_daily_pick", 0),
        "eligible_for_pick": c.get("eligible_for_pick", 1),
        "in_cooldown": c.get("in_cooldown", 0),
        "cooldown_until": c.get("cooldown_until"),
        "historical_years_available": f["historical_years_available"],
        "top_drivers": ",".join(sc["top_2_drivers"]),
        "bottom_drivers": ",".join(sc["bottom_2_drivers"]),
        "short_reason": _short_reason(sc),
        # valuation overlay (shadow; not used for ranking)
        "sector_median_pe":       c.get("sector_median_pe"),
        "fair_value_pe":          c.get("fair_value_pe"),
        "pe_vs_sector":           c.get("pe_vs_sector"),
        "valuation_adj":          c.get("valuation_adj", 0.0),
        "valuation_label":        c.get("valuation_label", "n/a"),
        "valuation_model_version": c.get("valuation_model_version", "v1_static_sector_pe"),
        # dual ranking (observation only; does not affect daily pick selection)
        "total_score_adj":                    c.get("total_score_adj"),
        "recommendation_rank_with_valuation": c.get("recommendation_rank_with_valuation"),
        # Beneish M-Score (Phase 2)
        "m_score":      f.get("m_score"),
        "beneish_flag": f.get("beneish_flag"),
        # Piotroski F-Score + Novy-Marx GP/Assets (Phase 3 Quality, shadow)
        "f_score":             f.get("f_score"),
        "novy_marx_gp_assets": f.get("novy_marx_gp_assets"),
        # Growth shadow factors (Phase 3.2)
        "revenue_cagr_5y": f.get("revenue_cagr_5y"),
        "eps_cagr_5y":     f.get("eps_cagr_5y"),
        "fcf_cagr_5y":     f.get("fcf_cagr_5y"),
        "asset_growth":    f.get("asset_growth"),
        # Value/Risk shadow factors (Phase 3.3)
        "ev_ebitda":          f.get("ev_ebitda"),
        "ps_ratio":           f.get("ps_ratio"),
        "peg_ratio":          f.get("peg_ratio"),
        "pe_vs_own_history":  f.get("pe_vs_own_history"),
        # Taleb Phase 1: fragility / hidden asymmetry shadow signals
        "altman_z_score":       f.get("altman_z_score"),
        "interest_coverage":    f.get("interest_coverage"),
        "fcf_negative_years_5": f.get("fcf_negative_years_5"),
        # Taleb Phase 2: ruin risk composite (shadow; path to VR sleeve)
        "ruin_risk_score": f.get("ruin_risk_score"),
        # Taleb Phase 3: longevity / Lindy shadow factors
        "revenue_cagr_10y": f.get("revenue_cagr_10y"),
        "eps_cagr_10y":     f.get("eps_cagr_10y"),
        "fcf_cagr_10y":     f.get("fcf_cagr_10y"),
        "lindy_score":      f.get("lindy_score"),
        # Taleb Phase 4: management alignment shadow signals
        "sbc_to_revenue":        f.get("sbc_to_revenue"),
        "insider_net_shares_3m": c.get("insider_net_shares_3m"),
        "ceo_pay_ratio":         c.get("ceo_pay_ratio"),
        # Phase 4 diagnostics: V1 linear scores + metadata
        "total_score_v1":       sc_v1.get("total_score"),
        "quality_score_v1":     sc_v1.get("quality_score"),
        "growth_score_v1":      sc_v1.get("growth_score"),
        "value_risk_score_v1":  sc_v1.get("value_risk_score"),
        "normalization_method":  SCORING_VERSION,
        "universe_n_at_scoring": sc.get("universe_n"),
        # Phase 6 performance tracking (non-nil only for the daily pick)
        "pick_price": c.get("pick_price"),
        # Overlay candidate tracking (shadow analysis; non-nil only for the daily pick)
        "overlay_candidate_sym":   c.get("overlay_candidate_sym"),
        "overlay_candidate_price": c.get("overlay_candidate_price"),
        "is_backfill": 0,
        # Portfolio awareness — 1 when symbol is a current Moomoo US holding
        "is_held": c.get("is_held", 0),
        "held_reject_code": c.get("held_reject_code"),
        # §7 Lookahead / data-vintage audit (P1; shadow)
        "data_vintage_days":  f.get("data_vintage_days"),
        "latest_filing_date": f.get("latest_filing_date"),
        "stale_data_flag":    f.get("stale_data_flag", 0),
        # §6 sector-concentration guardrail (set by [5c/7]; default 0)
        "sector_concentration_warning": c.get("sector_concentration_warning", 0),
        # P4 revenue one-time-spike protection (shadow; live growth_score untouched)
        "revenue_consistency_flag": f.get("revenue_consistency_flag", 0),
        "revenue_spike_ratio":      f.get("revenue_spike_ratio"),
        "growth_score_spike_adj":   spike_adjusted_growth(
            sc["growth_score"], f.get("revenue_consistency_flag"), REVENUE_SPIKE_PENALTY
        ),
    }


def _held_stub_row(symbol: str, run_date: str, reject_code: str) -> dict[str, Any]:
    """Minimal daily_picks row for a held symbol that was never scored.

    Used when a held position failed a hard screen or never entered the universe,
    so it has no scoring/factors. insert_picks fills every other COLUMNS key with
    None via .get(). Never a daily pick (is_daily_pick stays 0).
    """
    return {
        "run_date": run_date,
        "symbol": symbol,
        "is_daily_pick": 0,
        "eligible_for_pick": 0,
        "is_held": 1,
        "held_reject_code": reject_code,
        "is_backfill": 0,
    }


def run(
    run_date: date | None = None,
    top_n: int = TOP_N,
    db_path: Path = CACHE_DB,
    max_workers: int = MAX_WORKERS,
    persist: bool = True,
    persist_full_universe: bool = False,
    is_backfill: bool = False,
) -> dict[str, Any]:
    max_workers = min(max_workers, _MAX_WORKERS_CAP)
    today = run_date or date.today()
    run_date = today.isoformat()
    t0 = time.monotonic()
    stage_t: dict[str, float] = {}

    # Init DB upfront so cooldown_map can query even when persist=False.
    storage.init_db(db_path)

    log.info("[1/7] building universe...")
    _ts = time.monotonic()
    uni = universe.build_universe()
    eligible = uni["eligible"]
    stage_t["universe"] = round(time.monotonic() - _ts, 1)
    log.info("    eligible universe: %d  (%.1fs)", len(eligible), stage_t["universe"])

    log.info("[2/7] per-ticker screen (workers=%d)...", max_workers)
    client = FMPClient()
    candidates: list[dict[str, Any]] = []
    rejects: dict[str, int] = {}
    reject_by_sym: dict[str, str] = {}  # per-symbol reject code (for is_held audit)
    beneish_exempt_count = 0  # §3: log-only growth-exemption hits (still rejected)
    err_samples: list[str] = []
    done = 0
    _ts = time.monotonic()
    with ThreadPoolExecutor(max_workers=max_workers) as ex:
        futs = {ex.submit(_screen_only, row, client): row["symbol"]
                for row in eligible}
        for fut in as_completed(futs):
            try:
                r = fut.result()
            except Exception as e:
                rejects["FETCH_ERROR"] = rejects.get("FETCH_ERROR", 0) + 1
                reject_by_sym[futs[fut]] = "FETCH_ERROR"
                if len(err_samples) < 5:
                    err_samples.append(f"{futs[fut]}: {type(e).__name__}: {str(e)[:80]}")
            else:
                if r["pass"]:
                    candidates.append(r)
                else:
                    rejects[r["reason"]] = rejects.get(r["reason"], 0) + 1
                    reject_by_sym[r["symbol"]] = r["reason"]
                    if r.get("beneish_growth_exempt"):
                        beneish_exempt_count += 1
            done += 1
            if done % 200 == 0:
                log.info("    progress: %d/%d  passed=%d", done, len(eligible), len(candidates))
    stage_t["screen"] = round(time.monotonic() - _ts, 1)
    log.info("    passed=%d  rejects=%s  (%.1fs)", len(candidates), rejects, stage_t["screen"])
    log.info("    beneish_growth_exempt (§3 log-only, still rejected): %d", beneish_exempt_count)
    for s in err_samples:
        log.info("    fetch_err sample: %s", s)

    log.info("[2b/7] cross-sectional z-score scoring (n=%d, %s)...",
             len(candidates), SCORING_VERSION)
    _ts = time.monotonic()
    candidates = score_cross_sectional(candidates)
    stage_t["score"] = round(time.monotonic() - _ts, 1)
    log.info("    scored=%d  weights Q=%.0f%% G=%.0f%% V=%.0f%%  (%.1fs)",
             len(candidates),
             WEIGHTS_V2["quality"]    * 100,
             WEIGHTS_V2["growth"]     * 100,
             WEIGHTS_V2["value_risk"] * 100,
             stage_t["score"])

    log.info("[3/7] ranking by total_score...")
    candidates.sort(key=lambda c: c["scoring"]["total_score"], reverse=True)
    raw_top = candidates[:RAW_TOP]
    for i, c in enumerate(raw_top, start=1):
        c["recommendation_rank"] = i

    if persist_full_universe and persist:
        log.info(
            "[3a/7] persisting full scored universe (n=%d, backfill=%s) for research...",
            len(candidates), is_backfill,
        )
        _ts = time.monotonic()
        research_rows = [
            _build_research_row(
                c, run_date,
                universe_rank=i,
                in_top20=(i <= RAW_TOP),
                is_backfill=int(is_backfill),
            )
            for i, c in enumerate(candidates, start=1)
        ]
        if is_backfill:
            deleted = storage.rebuild_research_universe(
                research_rows, run_date=run_date, path=db_path
            )
            log.info(
                "    rebuild: deleted=%d  inserted=%d  (%.1fs)",
                deleted, len(research_rows), time.monotonic() - _ts,
            )
        else:
            storage.insert_research_universe(research_rows, path=db_path)
            log.info(
                "    inserted %d research rows  top20=%d  (%.1fs)",
                len(research_rows), min(RAW_TOP, len(candidates)),
                time.monotonic() - _ts,
            )
        stage_t["research_universe"] = round(time.monotonic() - _ts, 1)

    log.info("[3b/7] fetching sector for top-%d (valuation overlay)...", RAW_TOP)
    _ts = time.monotonic()
    for c in raw_top:
        try:
            prof = client.profile(c["symbol"])
            c["sector"] = prof.get("sector")
            c["industry"] = prof.get("industry")   # P0: top-20 industry from profile
        except Exception:
            c["sector"] = None
            c["industry"] = None

    log.info("[3c/7] applying valuation overlay...")
    for c in raw_top:
        apply_valuation(c)

    log.info("[3d/7] computing valuation-adjusted ranking (k=%.2f)...", VALUATION_WEIGHT)
    for c in raw_top:
        adj = c.get("valuation_adj") or 0.0   # treat NULL / n/a sentinel as 0.0
        c["total_score_adj"] = round(
            c["scoring"]["total_score"] + VALUATION_WEIGHT * adj, 2
        )
    adj_sorted = sorted(raw_top, key=lambda c: c["total_score_adj"], reverse=True)
    adj_rank_map = {c["symbol"]: i for i, c in enumerate(adj_sorted, start=1)}
    for c in raw_top:
        c["recommendation_rank_with_valuation"] = adj_rank_map[c["symbol"]]
    stage_t["valuation"] = round(time.monotonic() - _ts, 1)
    log.info("    rank shifts (orig->adj): %s  (%.1fs)",
             " ".join(
                 f"{c['symbol']}:{c['recommendation_rank']}->{c['recommendation_rank_with_valuation']}"
                 for c in raw_top
                 if c["recommendation_rank"] != c["recommendation_rank_with_valuation"]
             ) or "none",
             stage_t["valuation"])

    log.info("[3e/7] fetching management alignment data for top-%d (shadow)...", RAW_TOP)
    _ts = time.monotonic()
    for c in raw_top:
        sym = c["symbol"]
        rev = c["factors"].get("revenue")

        try:
            stats = client.insider_trading_stats(sym)
            row0 = stats[0] if stats else {}
            c["insider_net_shares_3m"] = (
                row0.get("totalAcquired", 0) - row0.get("totalDisposed", 0)
                if row0 else None
            )
        except Exception:
            c["insider_net_shares_3m"] = None

        try:
            comp = client.executive_compensation(sym)
            ceo_rows = [
                r for r in comp
                if "chief executive" in (r.get("nameAndPosition") or "").lower()
            ]
            if ceo_rows and rev and rev > 0:
                latest = max(ceo_rows, key=lambda r: r.get("year") or 0)
                total_comp = latest.get("total")
                c["ceo_pay_ratio"] = (
                    round(total_comp / rev * 100, 6) if total_comp else None
                )
            else:
                c["ceo_pay_ratio"] = None
        except Exception:
            c["ceo_pay_ratio"] = None

    stage_t["mgmt_align"] = round(time.monotonic() - _ts, 1)
    log.info("    management alignment fetched (%.1fs)", stage_t["mgmt_align"])

    log.info("[4/7] applying %d-day cooldown...", COOLDOWN_DAYS)
    _ts = time.monotonic()
    walk_input = [{"symbol": c["symbol"], "recommendation_rank": c["recommendation_rank"]}
                  for c in raw_top]
    apply_cooldown(walk_input, today=today, db_path=db_path)
    cd_by_sym = {x["symbol"]: x for x in walk_input}
    for c in raw_top:
        meta = cd_by_sym[c["symbol"]]
        c["in_cooldown"] = meta["in_cooldown"]
        c["eligible_for_pick"] = meta["eligible_for_pick"]
        c["cooldown_until"] = meta["cooldown_until"]
        c["is_daily_pick"] = meta["is_daily_pick"]
    stage_t["cooldown"] = round(time.monotonic() - _ts, 1)

    daily_pick = next((c for c in raw_top if c["is_daily_pick"] == 1), None)
    log.info("    daily_pick=%s rank=%s  in_cooldown=%d  (%.1fs)",
             daily_pick["symbol"] if daily_pick else None,
             daily_pick["recommendation_rank"] if daily_pick else None,
             sum(1 for c in raw_top if c.get("in_cooldown")),
             stage_t["cooldown"])

    log.info("[4b/7] fetching EOD close price for daily pick (phase 6)...")
    if daily_pick:
        try:
            hist = client.historical_price(daily_pick["symbol"], light=False)
            daily_pick["pick_price"] = hist[0]["close"] if hist else None
            log.info("    pick_price %s = %s", daily_pick["symbol"],
                     f"{daily_pick['pick_price']:.4f}" if daily_pick["pick_price"] else "n/a")
        except Exception:
            log.warning("    could not fetch pick_price for %s", daily_pick["symbol"])
            daily_pick["pick_price"] = None

    log.info("[4c/7] identifying cooldown-eligible overlay candidate (shadow analysis)...")
    _eligible = [c for c in raw_top if not c.get("in_cooldown")]
    _overlay_candidate = (
        max(_eligible, key=lambda c: c.get("total_score_adj") or c["scoring"]["total_score"])
        if _eligible else None
    )
    if daily_pick:
        oc_sym = _overlay_candidate["symbol"] if _overlay_candidate else None
        daily_pick["overlay_candidate_sym"] = oc_sym
        if oc_sym and oc_sym != daily_pick["symbol"]:
            try:
                _hist = client.historical_price(oc_sym, light=False)
                daily_pick["overlay_candidate_price"] = _hist[0]["close"] if _hist else None
            except Exception:
                log.warning("    could not fetch overlay_candidate_price for %s", oc_sym)
                daily_pick["overlay_candidate_price"] = None
        else:
            # same as base pick — reuse already-fetched price
            daily_pick["overlay_candidate_price"] = daily_pick.get("pick_price")
        log.info("    overlay_candidate=%s  divergent=%s",
                 oc_sym, oc_sym != daily_pick["symbol"] if oc_sym else False)

    log.info("[5/7] preparing top-%d storage rows...", top_n)
    _ts = time.monotonic()
    top_rows = [_build_row(c, run_date, c["recommendation_rank"])
                for c in raw_top[:top_n]]
    if daily_pick and daily_pick["recommendation_rank"] > top_n:
        top_rows.append(_build_row(daily_pick, run_date,
                                   daily_pick["recommendation_rank"]))

    # --- Portfolio awareness: always persist held US positions --------------
    # Additive only. Held rows are flag-stamped on existing top-N rows or
    # appended; the scoring cross-section and the is_daily_pick selection
    # (already done above) are never touched. No new FMP calls — held rows
    # reuse already-fetched candidate data, or are NULL-score stubs.
    held = storage.held_us_symbols(db_path)
    if held:
        rows_by_sym = {r["symbol"]: r for r in top_rows}
        cand_by_sym = {c["symbol"]: (i, c) for i, c in enumerate(candidates, start=1)}
        eligible_syms = {row["symbol"] for row in eligible}
        n_scored = n_screened = n_outside = 0
        for sym in held:
            if sym in rows_by_sym:                  # already a top-N / daily-pick row
                rows_by_sym[sym]["is_held"] = 1
            elif sym in cand_by_sym:                # scored, but ranked below top-N
                rank, cand = cand_by_sym[sym]
                row = _build_row(cand, run_date, rank)
                row["is_held"] = 1
                top_rows.append(row)
                n_scored += 1
            elif sym in reject_by_sym:              # fetched but failed a hard screen
                top_rows.append(_held_stub_row(sym, run_date, reject_by_sym[sym]))
                n_screened += 1
            elif sym not in eligible_syms:          # never entered the universe
                top_rows.append(_held_stub_row(sym, run_date, "OUTSIDE_UNIVERSE"))
                n_outside += 1
            else:                                   # in universe yet untracked — never drop silently
                top_rows.append(_held_stub_row(sym, run_date, "UNKNOWN"))
                n_screened += 1
        log.info("[5b/7] held augmentation: scored_below_top%d=%d screened=%d "
                 "outside_universe=%d (held set=%d)",
                 top_n, n_scored, n_screened, n_outside, len(held))

    # [5c/7] Sector-concentration guardrail — WARN ONLY; never excludes or re-ranks.
    # Count sectors among the top-5 (recommendation_rank <= 5). If a sector repeats,
    # flag every affected top-5 row. Held stubs (rank NULL) and below-top-5 rows are
    # excluded from the count.
    top5_rows = [r for r in top_rows if (r.get("recommendation_rank") or 99) <= 5]
    sec_counts = Counter(r.get("sector") for r in top5_rows if r.get("sector"))
    concentrated = {s for s, cnt in sec_counts.items() if cnt >= 2}
    if concentrated:
        for r in top5_rows:
            if r.get("sector") in concentrated:
                r["sector_concentration_warning"] = 1
        log.warning("[5c/7] sector concentration in top-5: %s  (warning only — nothing excluded)",
                    ", ".join(f"{s} x{sec_counts[s]}" for s in sorted(concentrated)))
    else:
        log.info("[5c/7] sector concentration: none (top-5 sectors distinct)")

    val_overlaid = sum(1 for r in top_rows if r.get("valuation_label", "n/a") != "n/a")
    log.info("    valuation overlay: %d/%d stored rows have overlay data (model=%s)",
             val_overlaid, len(top_rows), "v1_static_sector_pe")

    if persist:
        log.info("[6/7] writing to %s ...", db_path)
        storage.init_db(db_path)
        storage.insert_picks(top_rows, path=db_path)
    stage_t["persist"] = round(time.monotonic() - _ts, 1)

    elapsed = round(time.monotonic() - t0, 1)
    log.info("[7/7] done in %.1fs  stages: universe=%.1fs screen=%.1fs "
             "score=%.1fs valuation=%.1fs mgmt_align=%.1fs cooldown=%.1fs persist=%.1fs",
             elapsed,
             stage_t.get("universe", 0), stage_t.get("screen", 0),
             stage_t.get("score", 0),
             stage_t.get("valuation", 0), stage_t.get("mgmt_align", 0),
             stage_t.get("cooldown", 0), stage_t.get("persist", 0))

    return {
        "run_date": run_date,
        "elapsed_s": elapsed,
        "universe_counts": {
            "raw_total": uni["raw_total"],
            "by_exchange": uni["by_exchange"],
            "dropped_symbol_filter": uni["dropped_symbol_filter"],
            "dropped_sector": uni["dropped_sector"],
            "dropped_adr": uni["dropped_adr"],
            "dropped_dollar_volume": uni["dropped_dollar_volume"],
            "eligible": uni["eligible_count"],
        },
        "scored": len(candidates),
        "rejects": rejects,
        "beneish_growth_exempt": beneish_exempt_count,  # §3 log-only count
        "raw_top": raw_top,
        "daily_picks_rows": top_rows,
        "daily_pick": daily_pick,
        "stage_timings": stage_t,
    }
