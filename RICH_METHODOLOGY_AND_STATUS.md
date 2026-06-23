# RICH — Methodology & Implementation Status

> **Start here.** This is the authoritative project overview for RICH — a fully automated US-equity stock recommender built on Python, SQLite, and the FMP API. It describes the investment philosophy, the complete pipeline architecture, the factor scoring model, and the current implementation status of every phase. If you are reading the code for the first time, read this document before anything else.

**Last updated:** 2026-06-11  
**Stack:** Python 3.13 · SQLite · FMP API · **Windows Task Scheduler (local)** — live runtime (MYT)

> **Live runtime note (2026-06-04):** the production daily run executes on the local Windows workstation via Task Scheduler, not the AWS EC2 instance. The EC2 deployment (see Session Log 2026-05-24) is documented history; it is not the current live path. See Session Log 2026-06-04 for the actual registered tasks and their status.

---

## Table of Contents

1. [Overview](#1-overview)
2. [Architecture](#2-architecture)
3. [Methodology — The 16-Step Framework](#3-methodology--the-16-step-framework)
4. [Implementation Status](#4-implementation-status)
5. [Observability & Tooling](#5-observability--tooling)
6. [Key Files Reference](#6-key-files-reference)
7. [Model Governance](#7-model-governance)
8. [Session Log — 2026-05-24](#8-session-log--2026-05-24)
9. [Session Log — 2026-05-25](#9-session-log--2026-05-25)
10. [Session Log — 2026-06-01](#10-session-log--2026-06-01)
11. [Session Log — 2026-06-04](#11-session-log--2026-06-04)
12. [Session Log — 2026-06-10/11](#12-session-log--2026-06-1011)

---

## 1. Overview

RICH is a **fully automated, pure-fundamentals stock picker** that runs every trading day on an AWS EC2 instance. It ingests raw financial data from the FMP API, filters a broad US-equity universe down to a single daily recommendation, and writes every decision — score, flag, and reject reason — to SQLite for full auditability.

The system is systematic and rules-based: no human intervention is required after setup, and no subjective judgement enters the model. It favours earnings quality and genuine profitability over growth narratives. When no candidate clears all gates, the system outputs no recommendation rather than being forced to pick.

RICH generates recommendations, not orders. It has no connection to any brokerage or execution system.

**Daily schedule (live: local Windows Task Scheduler, MYT):**

| Task | Last result (2026-06-04) | Trigger |
|---|---|---|
| `RICH_Daily_1200_MYT` | 0 ✅ | `run_daily.bat` (`run_daily.py`) |
| `RICH_Daily_0800_MYT` | 0 ✅ | `run_daily.bat` (`run_daily.py`) |
| `RICH_Track_Performance` | 0 ✅ | `track_performance.py` |
| `RICH_Moomoo_Sync` | disabled | `sync_moomoo.py` (see Phase Summary) |

> Both daily-pipeline tasks run successfully; the two `0800/1200` names are historical and no longer match their actual start times. The EC2 cron + S3-backup schedule below is retained as 2026-05-24 deployment history, not the live path.

Typical runtime: **10–15 minutes**, dominated by FMP API calls. A same-day rerun completes in under 1 second via the per-day JSON cache.

---

## 2. Architecture

### 2.1 Pipeline Flow

```
FMP API
  │
  ▼
universe.py ─────────────────────────────────────────────────────────
  │  NASDAQ + NYSE + AMEX via company_screener
  │  Drop: preferred shares / warrants / dual-listed shells
  │  Drop: Financial Services / Utilities / Real Estate      [Phase 1]
  │  Drop: non-US domicile (ADR guard)                       [Phase 1]
  │  Drop: market cap < $2 B or daily dollar volume < $5 M
  ▼
  ~1,000 – 1,150 eligible stocks
  │
  ▼  [Pass 1 — ThreadPoolExecutor, 6 workers, capped at 20]
factors.py ──────────────────────────────────────────────────────────
  │  FMPClient fetches: income_statement / balance_sheet /
  │    cash_flow / ratios / key_metrics / financial_growth
  │  Per-day JSON cache (fmp_cache_YYYY-MM-DD.json) — zero
  │  duplicate API calls on same-day reruns
  │
  ├─→ Scored metrics:  ROE, ROA, GPM, OPM, OCF/NI, D/E, PE, revGrowth
  ├─→ Quality extras:  f_score, novy_marx_gp_assets
  ├─→ Growth extras:   revenue_cagr_5y, eps_cagr_5y, fcf_cagr_5y, asset_growth
  ├─→ Value/Risk:      ev_ebitda, ps_ratio, pe_vs_own_history
  ├─→ Manipulation:    m_score, beneish_flag  (feeds screener gate)
  └─→ Shadow-only:     peg_ratio
  │
screener.py ─────────────────────────────────────────────────────────
  │  Hard reject rules (binary pass/fail):
  │    HIST_LT_5 · REV_NONPOSITIVE · NI_OCF_NONPOSITIVE
  │    MISSING_FIELDS · BENEISH_FLAG                         [Phase 2]
  │
  ▼  [Pass 2 — single-threaded across all passing candidates]
scoring.py ──────────────────────────────────────────────────────────
  │  Cross-sectional z-score (±3σ clip → [0, 100])           [Phase 4]
  │  total_score = 0.35·Q + 0.40·G + 0.25·VR  (WEIGHTS_V2)
  │  V1 linear scores retained as diagnostic columns
  │
  │  Top-20 cap (RAW_TOP=20) ─────────────────────────────────────────
  │  Sort by total_score desc; take top 20 only.
  │  Valuation overlay, cooldown, and daily pick selection all
  │  operate exclusively within this top-20 pool.
  │
valuation.py ────────────────────────────────────────────────────────
  │  Sector-median PE overlay → valuation_adj (±10 pts cap)
  │  Produces total_score_adj + recommendation_rank_with_valuation
  │  ⚠ SHADOW ONLY — does not affect the daily pick
  │
cooldown.py ─────────────────────────────────────────────────────────
  │  60-day cooldown prevents repeat picks
  │  Top-ranked non-cooldown stock → is_daily_pick = 1
  │  EOD close price fetched for daily pick → pick_price     [Phase 6]
  │
data/storage.py ─────────────────────────────────────────────────────
  │  INSERT OR REPLACE into daily_picks (SQLite, 43 columns)
  │  Also manages: portfolio_holdings · portfolio_runs       [Phase 5]
  │                pick_outcomes                             [Phase 6]
  │
logging_utils.py ────────────────────────────────────────────────────
     logs/YYYY-MM-DD.log  ·  logs/run_metrics.csv
```

### 2.2 Key Modules

| Module | Role |
|---|---|
| `run_daily.py` | Entry point — runs `rich_check.py` gate first, then initialises logging and calls `pipeline.run()` |
| `scripts/rich_check.py` | Pre-run integrity gate — 5 checks; aborts pipeline on FAIL |
| `pipeline.py` | Orchestration across 7 stages; enforces worker cap and pick-price fetch |
| `universe.py` | Universe construction: exchange, liquidity, sector, and ADR filters |
| `factors.py` | FMP data fetching and per-ticker factor extraction; thread-safe JSON cache |
| `screener.py` | Hard rejection rules including the Beneish gate |
| `beneish.py` | Standard 8-variable Beneish M-Score model |
| `piotroski.py` | 9-signal Piotroski F-Score model |
| `scoring.py` | Cross-sectional z-score scoring: `RULES`, `WEIGHTS_V2`, `score_cross_sectional()` |
| `valuation.py` | Sector-median PE overlay — shadow mode only |
| `cooldown.py` | 60-day cooldown map derived from `daily_picks` |
| `data/fetcher.py` | `FMPClient` — all FMP `/stable/` endpoints; tenacity retries; `DEFAULT_TIMEOUT = (10, 30)` |
| `data/storage.py` | `SCHEMA`, `COLUMNS`, `_MIGRATIONS` — single source of truth for the DB shape |
| `portfolio.py` | Portfolio engine: entry/exit/hold rules *(read-only, Phase 5)* |
| `track_performance.py` | Pick return vs SPY tracking; writes `pick_outcomes` *(Phase 6)* |

### 2.3 Database Schema (`data/cache.db`)

**`daily_picks`** — 59 columns, PK `(run_date, symbol)`

| Column group | Columns |
|---|---|
| Identity | `run_date`, `symbol`, `company_name` |
| Scores (V2) | `total_score`, `quality_score`, `growth_score`, `value_risk_score` |
| Ranking | `recommendation_rank`, `is_daily_pick`, `eligible_for_pick`, `in_cooldown`, `cooldown_until` |
| Universe | `historical_years_available` |
| Explanation | `top_drivers`, `bottom_drivers`, `short_reason` |
| Valuation overlay *(shadow)* | `sector_median_pe`, `fair_value_pe`, `pe_vs_sector`, `valuation_adj`, `valuation_label`, `valuation_model_version`, `total_score_adj`, `recommendation_rank_with_valuation` |
| Beneish | `m_score`, `beneish_flag` |
| Quality extras | `f_score`, `novy_marx_gp_assets` |
| Growth extras | `revenue_cagr_5y`, `eps_cagr_5y`, `fcf_cagr_5y`, `asset_growth` |
| Value/Risk extras | `ev_ebitda`, `ps_ratio`, `peg_ratio`, `pe_vs_own_history` |
| V1 diagnostics | `total_score_v1`, `quality_score_v1`, `growth_score_v1`, `value_risk_score_v1` |
| Metadata | `normalization_method`, `universe_n_at_scoring` |
| Performance | `pick_price` |
| Taleb fragility *(shadow)* | `altman_z_score`, `interest_coverage`, `fcf_negative_years_5` |
| Taleb ruin risk *(shadow)* | `ruin_risk_score` |
| Taleb longevity *(shadow)* | `revenue_cagr_10y`, `eps_cagr_10y`, `fcf_cagr_10y`, `lindy_score` |
| Taleb governance *(shadow)* | `sbc_to_revenue`, `insider_net_shares_3m`, `ceo_pay_ratio` |
| Overlay candidate *(shadow)* | `overlay_candidate_sym`, `overlay_candidate_price` |
| Backfill flag | `is_backfill` |
| Portfolio awareness *(Moomoo sync)* | `is_held`, `held_reject_code` |

**`pick_outcomes`** — PK `(run_date, symbol)`

`run_date · symbol · pick_price · exit_price · hold_days · return_pct · spy_return_pct · alpha · as_of_date`

**`portfolio_holdings`** · **`portfolio_runs`** — Portfolio engine tables *(Phase 5, read-only)*

---

## 3. Methodology — The 16-Step Framework

### 3.1 Investment Philosophy

RICH is a **pure-fundamentals, anti-glamour** system.

- **No momentum. No sentiment. No macro.** The model uses only audited financial statement data.
- **Anti-hype by construction.** High-multiple story stocks are penalised by the Value/Risk sleeve and the PE-vs-own-history signal.
- **Earnings quality over earnings level.** The Beneish gate and Piotroski F-Score specifically filter out companies that report profits but cannot back them with cash flow.
- **One pick per day, or none.** The system is never forced to recommend; an empty position is a valid output.
- **Full auditability.** Every score, flag, and rejection reason is persisted. Any pick can be traced through all intermediate steps after the fact.

---

### Stage A — Universe Construction (Steps 1–3)

| Filter | Logic | Code |
|---|---|---|
| Exchange | NASDAQ + NYSE + AMEX via FMP `company_screener` | `universe.py` |
| Liquidity floor | Market cap ≥ **$2 B** and daily dollar volume ≥ **$5 M** | `universe.py` |
| Symbol hygiene | Drop preferred shares (`.PR`), warrants (`.WT`), dual-listed shells | `universe.py` |
| **Sector exclusion** | Drop Financial Services, Utilities, Real Estate — structurally incompatible with RICH's factor model | `config.py`, `universe.py` |
| **ADR guard** | Secondary `country` field check; rejects non-US-domiciled companies regardless of listing exchange | `universe.py` |

> **Typical eligible universe:** ~1,000–1,150 stocks per run, down from ~1,550 raw before sector exclusion.

---

### Stage B — Hard Screens (Steps 4–8)

All rules are **binary**. A single failure eliminates the stock from scoring entirely.

| Reject code | Rule | Implementation |
|---|---|---|
| `FETCH_ERROR` | FMP API call failed before factor extraction | `screener.py` |
| `HIST_LT_5` | Fewer than 5 years of financial history | `screener.py` |
| `REV_NONPOSITIVE` | Revenue ≤ 0 | `screener.py` |
| `NI_OCF_NONPOSITIVE` | Both net income **and** operating cash flow ≤ 0 | `screener.py` |
| `OCF_NEGATIVE` | Operating cash flow ≤ 0 in the most recent year (positive NI does not exempt) | `screener.py` *(Taleb Phase 1)* |
| `BENEISH_FLAG` | **Beneish M-Score > −1.78** | `beneish.py` → `screener.py` |
| `MISSING_FIELDS` | Any of the 8 core scored metrics is absent | `screener.py` |

#### Beneish M-Score Gate

The Beneish model uses 8 financial ratios derived from two consecutive years of income, balance sheet, and cash flow statements to estimate the likelihood of earnings manipulation. A score above −1.78 (the published threshold) triggers a hard reject — the stock is excluded from scoring regardless of any other metric. In practice, `BENEISH_FLAG` eliminates roughly 7–8% of the eligible universe per run.

---

### Stage C — Factor Scoring (Steps 9–13)

Scoring uses **cross-sectional z-scores** across all candidates that passed Stage B, not fixed linear ranges. For each metric:

1. Pool all passing-candidate values → compute cross-sectional mean (μ) and std (σ)
2. `z = (v − μ) / σ`, sign-inverted for lower-is-better metrics
3. Clip at **±3σ**
4. Map to **[0, 100]**
5. Average within sleeve → weighted composite

```
total_score = 0.35 · quality_score + 0.40 · growth_score + 0.25 · value_risk_score
```

#### Quality Sleeve — weight 0.35

| Metric | Notes |
|---|---|
| `returnOnEquity` | Core profitability |
| `returnOnAssets` | Asset efficiency |
| `grossProfitMargin` | Pricing power |
| `operatingProfitMargin` | Operating leverage |
| OCF/NI ratio | Cash conversion quality |
| **`f_score`** | Piotroski 9-signal composite (profitability + leverage + efficiency) |
| **`novy_marx_gp_assets`** | Gross profit / total assets — Novy-Marx quality factor |

#### Growth Sleeve — weight 0.40

| Metric | Notes |
|---|---|
| `revenueGrowth` | Latest year-on-year |
| **`revenue_cagr_5y`** | 5-year revenue compound growth |
| **`eps_cagr_5y`** | 5-year EPS compound growth |
| **`fcf_cagr_5y`** | 5-year free cash flow compound growth |
| **`asset_growth`** | *Inverted penalty* — rapid asset expansion scores lower (Fama-French CMA style) |

CAGR metrics are optional per-ticker: if data is unavailable, the sleeve average is computed from the remaining metrics rather than failing.

#### Value/Risk Sleeve — weight 0.25

| Metric | Direction | Notes |
|---|---|---|
| `debtToEquityRatio` | ↓ lower is better | Leverage |
| `priceEarningsRatio` | ↓ | Traditional PE multiple |
| **`ev_ebitda`** | ↓ | Enterprise multiple; stocks with negative EBITDA are excluded from this metric |
| **`ps_ratio`** | ↓ | Price-to-sales |
| **`pe_vs_own_history`** | ↓ | Current PE divided by the median of ≥3 prior-year positive PEs — a pseudo-CAPE signal |

`peg_ratio` is computed and stored as a shadow column only; it is too sparse and too frequently negative to participate in scoring reliably.

---

### Stage D — Valuation Overlay (Steps 14–15)

> ⚠ **Shadow mode only.** The overlay is computed and stored but does not affect the daily pick.

A sector-median PE model (quality/growth-blended fair PE, static sector benchmarks) applies a ±10 pt adjustment to produce an alternative composite score and rank. The overlay runs on the **top-20 candidates only** (after the RAW_TOP=20 cap); stocks ranked 21+ receive no overlay columns. These columns are persisted for observation and will be evaluated against live performance data before any promotion decision.

| Column | Meaning |
|---|---|
| `valuation_adj` | Adjustment, capped at ±10 pts |
| `valuation_label` | `deep_discount / discount / fair / premium / expensive / n/a` |
| `total_score_adj` | `total_score + 0.5 × valuation_adj` |
| `recommendation_rank_with_valuation` | Alternative rank if the overlay were live |

**Decision gate:** On or after **2026-06-02** (~20 trading days of accumulated data), compare alpha from overlay-adjusted picks against alpha from raw-score picks. Promote to live if the overlay shows consistent improvement; recalibrate or remove it otherwise. ⚠️ **OVERDUE as of 2026-06-04** — the gate date has passed and the promote/extend/remove decision has not yet been made.

---

### Stage E — Cooldown & Selection (Step 16)

- A **60-day cooldown** prevents the same ticker from being selected again within two months
- The highest-scoring stock not in cooldown is designated `is_daily_pick = 1`
- The top-5 rows by score (`TOP_N = 5`) are persisted each run; the daily pick is always included even if it ranks outside the top-5
- The pipeline fetches the most recent EOD close price for the daily pick immediately after selection and stores it as `pick_price` in `daily_picks`

---

## 4. Implementation Status

### Phase Summary

| Phase | Description | Status |
|---|---|---|
| **Phase 1** | Universe hardening — sector exclusion, ADR guard | ✅ Live |
| **Phase 2** | Beneish M-Score hard gate | ✅ Live |
| **Phase 3.1** | Quality sleeve — F-Score + Novy-Marx | ✅ Live |
| **Phase 3.2** | Growth sleeve — 5-year CAGRs + asset growth penalty | ✅ Live |
| **Phase 3.3** | Value/Risk sleeve — EV/EBITDA, PS ratio, PE vs own history | ✅ Live |
| **Phase 4** | Cross-sectional z-score scoring; V2 weights Q=35/G=40/V=25 | ✅ Live |
| **Phase 5** | Portfolio engine — entry/exit/hold cycle | 🔵 Read-only (pending promotion) |
| **Phase 6** | Performance tracking — `pick_outcomes`, alpha vs SPY | ✅ Live |
| **Valuation overlay** | Sector-median PE shadow rank | 🔴 Shadow — gate **OVERDUE** (was 2026-06-02; decision pending) |
| **FMP day-cache** | Per-day JSON cache; zero API calls on same-day rerun | ✅ Live |
| **Timeout hardening** | Split connect/read timeouts; `Timeout` warning log | ✅ Live |
| **Taleb Phase 1** | OCF_NEGATIVE hard gate + fragility shadow signals (Altman Z, interest coverage, FCF-negative years) | ✅ Live |
| **Taleb Phase 2** | Ruin risk composite `ruin_risk_score` [0–1] *(shadow)* | 🔵 Shadow (gate: 2026-07-04) |
| **Taleb Phase 3** | 10-year CAGR windows + `lindy_score` *(shadow)* | 🔵 Shadow (gate: 2026-07-04) |
| **Taleb Phase 4** | Management alignment: `sbc_to_revenue`, `insider_net_shares_3m`, `ceo_pay_ratio` *(shadow)* | 🔵 Shadow (gate: 2026-07-04) |
| **Taleb Phase 5** | Model governance: `SIGNAL_GOVERNANCE` registry in `config.py`; `governance.py` report script | ✅ Live |
| **research_universe** | Full scored-universe snapshot for factor validation (Alphalens) | ✅ Live (2026-06-01) |
| **Moomoo sync** | Read-only broker account/position snapshots → SQLite (`sync_moomoo.py`); additive, no picker changes. Scheduled task `RICH_Moomoo_Sync` **created**; fail-fast OpenD retry cap **added** (2026-06-04); task **disabled pending OpenD availability** | 🟡 Code live; task disabled |
| **Portfolio awareness** | `is_held` — held US positions always scored/persisted to `daily_picks` (`storage.py` + `pipeline.py` `[5b/7]`) | ✅ Live (2026-06-01) |

---

### ✅ Phase 1 — Universe Hardening

- NASDAQ + NYSE + AMEX; preferred shares, warrants, and dual-listed shells removed
- **Sector exclusion:** Financial Services, Utilities, Real Estate excluded via `EXCLUDED_SECTORS` in `config.py`
- **ADR guard:** secondary `country` field check applied after the API's own `country=US` filter

---

### ✅ Phase 2 — Beneish M-Score Hard Gate

- `beneish.py` implements the standard 8-variable model using consecutive-year IS/BS/CF data; returns `None` when fewer than 2 years are available
- Applied in `screener.py` immediately after the OCF/NI check: `M-Score > −1.78` → `BENEISH_FLAG` reject
- `m_score` and `beneish_flag` are written to `daily_picks` for every passing and failing candidate

---

### ✅ Phase 3.1 — Quality Sleeve

- `piotroski.py` evaluates all 9 Piotroski signals (profitability, leverage, operating efficiency) and sums them into `f_score` (0–9)
- `novy_marx_gp_assets = grossProfit / totalAssets` computed from the most recent annual data
- Both metrics are active participants in the Quality sleeve score

---

### ✅ Phase 3.2 — Growth Sleeve

- `revenue_cagr_5y`, `eps_cagr_5y`, and `fcf_cagr_5y` computed in `factors.py` and active in scoring
- `asset_growth` included as an inverted penalty: unusually rapid asset expansion is treated as a capital-efficiency warning, not a strength
- Each CAGR metric is optional per-ticker; the sleeve average degrades gracefully on missing data

---

### ✅ Phase 3.3 — Value/Risk Sleeve

- `ev_ebitda`, `ps_ratio`, and `pe_vs_own_history` are active in scoring; all three are inverted (lower is better)
- Stocks with negative EBITDA are excluded from the `ev_ebitda` signal via `_SCORE_FLOOR`
- `pe_vs_own_history` requires at least 3 years of positive prior-year PE data; returns `None` otherwise
- `peg_ratio` is computed and persisted as a shadow column; not used in scoring

---

### ✅ Phase 4 — Cross-Sectional Z-Score Scoring

- Pass 1 (parallel) screens and extracts factors; Pass 2 (single-threaded) scores all passing candidates together in one cross-section
- Z-score normalisation eliminates the fixed-range fragility of the original V1 model
- Final sleeve weights: Q = 35%, G = 40%, VR = 25%
- V1 linear scores retained in `*_score_v1` diagnostic columns for backward comparison

---

### 🔵 Phase 5 — Portfolio Engine *(read-only)*

The portfolio engine is fully implemented in `portfolio.py` and `run_portfolio.py`. It reads from `daily_picks`, applies its own entry/exit rules, and writes results to `portfolio_holdings` and `portfolio_runs`. It does not modify the core pipeline or daily pick selection in any way.

The engine is in **read-only observation mode**: it processes picks and logs signals, but its output is not yet used to drive any external action. Promotion to live use is pending sufficient accumulated pick history to validate the entry/exit rules.

**Entry criteria:**
- Piotroski F-Score ≥ 6
- `pe_vs_own_history < 1.0` (stock trading below its own historical PE median); stocks with `NULL` pe_vs_own_history are skipped
- Not already held (Beneish is not a direct entry check — it is enforced upstream by the screener, so all rows in `daily_picks` already have `beneish_flag = 0`)
- Candidate pool: most-recent `daily_picks` row per symbol within the last **30 days** (`CANDIDATE_LOOKBACK = 30`)

**Exit criteria:**
- Hard exit: `beneish_flag = 1` or `f_score ≤ 3`
- Soft exit: position held for ≥ 84 days

**Parameters:** `MIN_FSCORE_ENTRY=6` · `MAX_PE_VS_OWN_HIST=1.0` · `HARD_EXIT_FSCORE=3` · `SOFT_HOLD_DAYS=84` · `MAX_HOLDINGS=30` · `CANDIDATE_LOOKBACK=30`

**CLI:** `python run_portfolio.py --show`

---

### ✅ Phase 6 — Performance Tracking

RICH now measures whether its picks outperform the S&P 500.

**`pick_price` in `daily_picks`:** the pipeline fetches the most recent EOD close price for the daily pick via `FMPClient.historical_price()` and stores it at run time. Pre-Phase-6 picks with no stored price are backfilled by `track_performance.py` using the same API.

**`pick_outcomes` table:** updated by running `python track_performance.py`. For each historical `is_daily_pick = 1` row, the script:
1. Resolves `pick_price` (stored or backfilled)
2. Fetches the current price via `FMPClient.quote()` as `exit_price`
3. Fetches SPY historical data once and looks up the SPY close on or before each pick date
4. Computes `return_pct`, `spy_return_pct`, and `alpha = return_pct − spy_return_pct`
5. Writes results with `INSERT OR REPLACE` — safe to re-run at any time

**Results** *(17 tracked daily-pick rows; `pick_outcomes` as of 2026-06-04 — still a small sample, not statistically conclusive. Days with multiple rows reflect multiple pipeline runs that day; each `is_daily_pick=1` row is tracked):*

| Pick date | Symbol | Return | SPY | Alpha | Hold days |
|---|---|---|---|---|---|
| 2026-05-03 | CALM | −1.18% | +4.66% | **−5.84%** | 32 |
| 2026-05-05 | BKNG | −1.70% | +4.21% | **−5.91%** | 30 |
| 2026-05-12 | HALO | +5.13% | +2.18% | **+2.96%** | 23 |
| 2026-05-13 | EXE | −5.56% | +1.61% | **−7.17%** | 22 |
| 2026-05-14 | BCRX | −14.06% | +0.81% | **−14.87%** | 21 |
| 2026-05-15 | APP | +17.66% | +2.04% | **+15.62%** | 20 |
| 2026-05-16 | EQT | −3.11% | +2.04% | **−5.15%** | 19 |
| 2026-05-23 | DAVE | +9.14% | +1.15% | **+7.98%** | 12 |
| 2026-06-01 | EXEL | +3.33% | −0.57% | **+3.90%** | 3 |
| 2026-06-01 | FANG | +5.06% | −0.57% | **+5.63%** | 3 |
| 2026-06-01 | PODD | −1.86% | −0.57% | **−1.29%** | 3 |
| 2026-06-02 | GRND | −13.63% | −0.70% | **−12.93%** | 2 |
| 2026-06-02 | UTI | +7.63% | −0.70% | **+8.34%** | 2 |
| 2026-06-03 | INOD | +0.46% | 0.00% | **+0.46%** | 1 |
| 2026-06-03 | KLAC | +2.99% | 0.00% | **+2.99%** | 1 |
| 2026-06-04 | TMDX | +0.89% | 0.00% | **+0.89%** | 0 |
| 2026-06-04 | GPOR | −0.03% | 0.00% | **−0.03%** | 0 |

*Aggregate: 9/17 positive alpha (~53%); mean alpha ≈ −0.26% (dominated by the BCRX/GRND tails). Too few observations and too short a horizon to draw conclusions.*

---

### ✅ Infrastructure — FMP Cache & Timeout Hardening

**Per-day FMP cache**
- `factors.py` writes raw API responses to `data/fmp_cache_YYYY-MM-DD.json`, keyed by symbol
- The cache is thread-safe under `ThreadPoolExecutor` via `threading.Lock`
- Same-day reruns skip all FMP calls and produce byte-identical scores, eliminating non-idempotent rerun divergence

**Timeout hardening** (`data/fetcher.py`)
- `DEFAULT_TIMEOUT = (10, 30)` — a `(connect_s, read_s)` tuple replaces the previous single scalar
- A slow server that connects but delivers bytes slowly can no longer block a worker thread indefinitely
- `requests.Timeout` exceptions are logged as `WARNING` before being re-raised to tenacity (up to 3 retries with exponential backoff)
- `pipeline.py` enforces `max_workers = min(max_workers, 20)` at runtime regardless of the caller argument

---

### ✅ research_universe — Full Universe Snapshot for Factor Validation *(LIVE 2026-06-01)*

`daily_picks` stores only the top-5 rows per run — too sparse for statistically meaningful factor analysis. The `research_universe` table captures the **full passing-screener cross-section** (~700–900 rows per run) so individual factors can be validated against forward returns via Alphalens before any are promoted to live scoring.

**Schema & storage** (`data/storage.py`):
- `SCHEMA_RESEARCH` — 33 columns: scores, `universe_rank`, `in_top20`, all active sleeve factors, plus audit fields `rule_version`, `normalization_method`, `is_backfill`
- `insert_research_universe()` — **`INSERT OR IGNORE`** for live daily runs; first write wins, same-day reruns are no-ops
- `rebuild_research_universe()` — **`DELETE + INSERT` in one transaction** for the maintenance/backfill path; atomic correction of a stored run

**Pipeline** (`pipeline.py`):
- `pipeline.run(persist_full_universe=True)` — enables the snapshot (default `False`; production behaviour unchanged when off)
- `pipeline.run(persist_full_universe=True, is_backfill=True)` — routes to the rebuild path and marks rows `is_backfill=1`
- `_RESEARCH_RULE_VERSION = "WEIGHTS_V2"` — stamped into every row for reproducibility
- Written at stage `[3a/7]`, after scoring/ranking and before the RAW_TOP=20 cap; `daily_picks` semantics untouched

**Factor validation:**
```
python factor_analysis.py --source research_universe
```

**Decision gate:** Run Alphalens IC analysis after **7 days** of accumulated data (≈ 2026-06-10). Promote a factor into live scoring **only** on a positive result (meaningful IC, monotonic quantile returns); otherwise keep collecting or discard.

---

### ✅ Moomoo Portfolio Sync — Read-Only Position Snapshots *(LIVE 2026-06-01)*

`sync_moomoo.py` is a **standalone, additive** utility that snapshots live brokerage state from the Moomoo OpenD gateway (`127.0.0.1:11111`) into `data/cache.db`. It imports no RICH module and **makes no change to the picker** — `universe.py`, `screener.py`, `scoring.py`, `cooldown.py`, `pipeline.py`, `run_daily.py`, and `daily_picks` are all untouched. It is **read-only against the broker**: it calls only the account/position query APIs and never places, modifies, or cancels an order.

**Purpose:** persist what is actually held versus what RICH recommends, so picks can be reconciled against real positions after the fact.

**New tables** (SQLite, `INSERT OR REPLACE`; never altered by the daily pipeline):

| Table | Grain | Key columns |
|---|---|---|
| `moomoo_account_snapshots` | one row per (run, account, currency) | `total_assets`, `cash`, `market_val`, `power`, `raw_json` |
| `moomoo_position_snapshots` | one row per (run, account, position) | `code`, `symbol`, `qty`, `cost_price`, `market_val`, `pl_ratio`, `raw_json` |

A shared `snapshot_ts` (ISO-8601) per run makes snapshots a time series; the full broker response is stored in `raw_json` for audit and forward-compatibility. Moomoo `US.AAPL` codes are normalized to the bare `symbol` (`AAPL`) to join `daily_picks`.

**CLI:**
```
python sync_moomoo.py             # snapshot + print latest-positions × latest-picks join
python sync_moomoo.py --dry-run   # fetch + preview only; no DB writes
python sync_moomoo.py --report    # join report from existing rows; no broker call
```

**Verified status:** first live sync succeeded **2026-06-01** — account and positions written, join report renders. Broker entity is set via top-of-file constants (`SECURITY_FIRM`, `TRD_MARKET = US`, `TRD_ENV = REAL`).

---

## 5. Observability & Tooling

### Log Files

| File | Contents |
|---|---|
| `logs/YYYY-MM-DD.log` | Full structured run log — stage-by-stage progress, reject code counts, rank shifts, daily pick |
| `logs/run_metrics.csv` | One row per run: full universe funnel, reject counts by code, per-stage timings, pick symbol and score |

### Scripts

| Script | Purpose |
|---|---|
| `run_daily.py` | Entry point — runs `rich_check.py` gate first, then initialises logging and calls `pipeline.run()` |
| `scripts/rich_check.py` | Pre-run integrity gate — schema, pipeline contract, config, report deps, scoring rules; `--fix` for float rounding |
| `report_today.py` | Print today's pick with valuation overlay detail |
| `daily_view.py` | Read-only pick viewer |
| `run_portfolio.py --show` | Display current portfolio engine holdings |
| `track_performance.py` | Update `pick_outcomes`; print return vs SPY for all picks |

**AWS ops note:** A `.bat` shortcut on the Windows operator workstation handles EC2 status checks (SSH + tail of `logs/cron.log`) without a manual SSH command. Email dispatch env vars (SMTP credentials) are configured on the EC2 instance; the daily pick email is fully automated.

---

## 6. Key Files Reference

### Orientation — read in this order

```
config.py          ← all constants: floors, cooldown, EXCLUDED_SECTORS, weights, API key env var
factors.py         ← canonical factor extraction; extract() returns all scored + shadow fields
screener.py        ← hard rejection rules + Beneish gate; the pass/reject boundary
scoring.py         ← RULES, WEIGHTS_V2, score_cross_sectional(); the scoring contract
data/storage.py    ← SCHEMA, COLUMNS, _MIGRATIONS; any new DB column must appear in all three
```

### Supporting files

| File | Purpose | Notes |
|---|---|---|
| `beneish.py` | 8-variable M-Score model | Do not modify unless fixing the model itself |
| `piotroski.py` | 9-signal F-Score model | Production — feeds the Quality sleeve |
| `pipeline.py` | Orchestration across 7 stages | `_build_row()` must stay in sync with `COLUMNS` |
| `valuation.py` | Sector-PE overlay | Shadow only until the 2026-06-02 decision gate |
| `universe.py` | Universe construction and filters | Rarely needs modification |
| `cooldown.py` | 60-day cooldown logic | — |
| `logging_utils.py` | `setup_logging()` + `append_run_metrics()` | Single source for all log configuration |
| `track_performance.py` | Phase 6 performance tracking | Re-runnable; uses `INSERT OR REPLACE` |
| `data/fetcher.py` | `FMPClient` — all FMP endpoints, retries, timeouts | `DEFAULT_TIMEOUT = (10, 30)` |
| `governance.py` | Read-only shadow signal governance report | `python governance.py [--json]`; zero DB writes, zero FMP calls |

### Change-control rule

> **`screener.py`, `scoring.py`, `data/storage.py`, `pipeline.py`** are production-critical.  
> Any modification to these files must be validated with a `pipeline.run(persist=False)` test run before deployment.

---

Last updated: 2026-06-01

---

## 7. Model Governance

RICH uses a **shadow → live** promotion framework for all experimental signals. Every new factor is computed and persisted from day one without touching the scoring contract. A gate date (approximately 30 trading days after deployment) triggers a mandatory promotion decision: promote, park, or demote. This section formalises the rules followed informally since the valuation overlay was introduced.

### Shadow → Live Promotion Rules

A shadow signal may be promoted to active scoring if **all** of the following hold:

1. **Null rate** is below 30% across observed picks.
2. **Return correlation**: the signal shows statistically meaningful rank-correlation (ρ ≥ 0.15, p < 0.10) with 60-day forward return over ≥ 30 observations.
3. **No regression**: a 30-day backtest on stored picks shows equal or better mean return vs. the baseline without the signal.
4. **Sleeve fit**: the signal's economic hypothesis is distinct from existing sleeve factors (no duplication of D/E, PE, or OCF signals already scored).

### Demotion / Parking Rules

A shadow signal is **parked indefinitely** if:
- Null rate exceeds 50% after 60 observations, **or**
- Gate date passes with no evidence for criteria 2 or 3 above.

A shadow signal is **removed** (column retained in DB for historical completeness, code removed from `factors.py`) if it is found to introduce bias or degrade return quality.

### Registered Shadow Signals

All shadow signals are registered in `SIGNAL_GOVERNANCE` in `config.py`. The machine-readable registry records: `status`, `sleeve`, `shadow_since`, `gate_date`, `promotion_criteria`, and `notes`.

Run `python governance.py` at any time for a live null-rate and observation-count report.

| Phase | Signal | Sleeve | Gate Date | Notes |
|---|---|---|---|---|
| 0 | `peg_ratio` | Growth | parked | High null rate; forward-looking estimate unreliable |
| 0 | `valuation_adj` | Overlay | 2026-06-02 | Sector-median PE; k=0.50 blend; rank display only |
| 0 | `total_score_adj` | Overlay | 2026-06-02 | Derived from valuation_adj |
| 0 | `recommendation_rank_with_valuation` | Overlay | 2026-06-02 | Valuation-adjusted rank; observation only |
| 1 | `altman_z_score` | ValueRisk | 2026-07-04 | Shadow-only; biased for asset-light tech |
| 1 | `interest_coverage` | ValueRisk | 2026-07-04 | NULL = neutral (zero-debt companies) |
| 1 | `fcf_negative_years_5` | ValueRisk | 2026-07-04 | Count of FCF-negative years in trailing 5 |
| 2 | `ruin_risk_score` | ValueRisk | 2026-07-04 | Composite of Phase 1 signals [0–1] |
| 3 | `revenue_cagr_10y` | Growth | 2026-07-04 | NULL when < 11 years of data |
| 3 | `eps_cagr_10y` | Growth | 2026-07-04 | NULL when < 11 years of data |
| 3 | `fcf_cagr_10y` | Growth | 2026-07-04 | NULL when < 11 years of data |
| 3 | `lindy_score` | Growth | 2026-07-04 | 40% history-length + 60% no-surge |
| 4 | `sbc_to_revenue` | Governance | 2026-07-04 | Proxy for shareholder dilution risk |
| 4 | `insider_net_shares_3m` | Governance | 2026-07-04 | Positive = net insider buying |
| 4 | `ceo_pay_ratio` | Governance | 2026-07-04 | CEO comp / revenue × 100; high null rate expected |

---

## 8. Session Log — 2026-05-24

## Summary

Two major workstreams completed on 2026-05-24:
1. **Taleb Upgrade** — 5 phases of Nassim Taleb-inspired shadow signals added to RICH
2. **AWS Deployment** — RICH migrated from local Windows machine to AWS EC2 for unattended daily execution

***

## Workstream 1: Taleb Upgrade (Phases 1–5)

All five phases implemented, validated with `pipeline.run(persist=False)`, and confirmed shadow-only (no changes to live scoring weights or the scoring contract).

### Phase 1 — Fragility Filter + Hidden Asymmetry

**Goal:** Add one new hard screener gate and three shadow signals that expose structural fragility invisible to the current Quality/Growth/VR metrics.

**Changes:**
- New hard reject gate: `OCF_NEGATIVE` — rejects any company with `operatingCashFlow ≤ 0` in the most recent year, regardless of net income. Caught **11 additional tickers** on first run.
- New shadow columns:

| Column | Type | Description |
|--------|------|-------------|
| `altman_z_score` | REAL | 5-factor Altman Z-Score (bankruptcy risk proxy) |
| `interest_coverage` | REAL | EBIT / interestExpense; NULL if no interest expense |
| `fcf_negative_years_5` | INTEGER | Count of years in trailing 5 CF rows where FCF < 0 (range 0–5) |

**Files changed:** `screener.py`, `factors.py`, `data/storage.py`, `pipeline.py`, `config.py`

***

### Phase 2 — Ruin Risk Composite

**Goal:** Aggregate Phase 1 shadow signals into a single `ruin_risk_score` composite modelling low-frequency, high-severity survival risk — the gap in the current ValueRisk sleeve.

**Formula:**
```
ruin_risk_score = (
    0.40 × normalize_inv(altman_z_score, floor=1.8, cap=5.0)
  + 0.35 × normalize_inv(interest_coverage, floor=1.5, cap=15.0)
  + 0.25 × (fcf_negative_years_5 / 5.0)
)
clipped to [0.0, 1.0]
```
NULL components redistribute weights proportionally (mirrors `_sleeve_avg()` pattern).

**Notable result:** EXE `ruin_risk_score = 0.476` — top-5 ranked company sitting in Altman grey zone.

**New column:** `ruin_risk_score REAL` — shadow only, not in `scoring.py` RULES.

**Files changed:** `factors.py`, `data/storage.py`, `pipeline.py`

***

### Phase 3 — Longevity / Lindy Effect

**Goal:** Extend CAGR calculations to 10-year windows with graceful fallback; add `lindy_score` shadow signal rewarding proven multi-decade growth consistency.

**Changes:**
- FMP fetch limit extended from 6 to 11 years
- 10-year CAGRs computed only when `n >= 11` rows available; NULL otherwise
- `lindy_score` composite: history class bonus + CAGR consistency across 5y/10y windows; penalises recent-surge (5y >> 10y) pattern

**New columns:**

| Column | Type | Description |
|--------|------|-------------|
| `revenue_cagr_10y` | REAL | 10-year revenue CAGR; NULL if < 11 rows |
| `eps_cagr_10y` | REAL | 10-year EPS CAGR; NULL if < 11 rows |
| `fcf_cagr_10y` | REAL | 10-year FCF CAGR; NULL if < 11 rows |
| `lindy_score` | REAL | Composite [0–1]; shadow only |

**Notable result:** EXE `rev_10y = -0.009` (flat long-run) vs `rev_5y = 0.175` (strong recent) — combined with `ruin_risk = 0.476`, accumulating multiple Taleb red flags.

**Files changed:** `factors.py`, `config.py`, `data/storage.py`, `pipeline.py`

***

### Phase 4 — Management Alignment

**Goal:** Add shadow signals that reflect whether decision-makers have real downside exposure (skin in the game).

**Design decision:** After verifying FMP endpoints and `km[0]` field list:
- `/acquisition-of-beneficial-ownership` rejected — 13G/D institutional threshold filings, not management ownership
- `km[0].stockBasedCompensationToRevenue` used instead — zero new API calls, already in cache; directly proxies management value extraction

**New columns:**

| Column | Type | Source | Description |
|--------|------|--------|-------------|
| `sbc_to_revenue` | REAL | `km[0]` (zero extra API calls) | Stock-based compensation as % of revenue |
| `insider_net_shares_3m` | REAL | `/insider-trading/statistics` | `totalAcquired - totalDisposed` (most recent quarter) |
| `ceo_pay_ratio` | REAL | `/governance-executive-compensation` | CEO total comp / revenue × 100 |

**Pipeline:** New stage `[3e/7]` — calls only for top-20 candidates; each call individually guarded with `try/except → None`.

**Notable results:**
- DAVE: `insider_net_shares_3m = +112,140` (heavy buying) BUT `ceo_pay_ratio = 1.84%` (highest in top-5) — Taleb extraction pattern visible
- EXE: `ceo_pay_ratio = NULL` (non-standard CEO title in filing)
- `[3e/7]` runs in ~10s — acceptable overhead

**Files changed:** `data/fetcher.py`, `factors.py`, `data/storage.py`, `pipeline.py`

***

### Phase 5 — Model Governance

**Goal:** Formalise the shadow → live promotion framework into a machine-readable rule set and read-only governance report.

**Changes:**

| Artifact | What was added |
|----------|---------------|
| `config.py` | `SIGNAL_GOVERNANCE` dict — 15 signals across 5 phases; each entry has `status`, `sleeve`, `shadow_since`, `gate_date`, `promotion_criteria`, `notes` |
| `governance.py` | New read-only script — queries `daily_picks` for null rate and first seen per signal; flags OVERDUE signals; `--json` flag supported; zero DB writes, zero FMP calls |
| `RICH_METHODOLOGY_AND_STATUS.md` | Section 7 "Model Governance" added: shadow→live rules, demotion rules, signal registry table; `Last updated` bumped to 2026-05-24 |

**Gate dates:** All new signals have `gate_date = 2026-07-04` (~30 trading days from 2026-05-24).

**Governance rules (formalised):**
- Shadow → Live: minimum 30 trading days observation + positive alpha evidence vs raw-score picks
- Live → Shadow (demotion): alpha degrades over 20-day rolling window

***

### Taleb Upgrade: Full Signal Registry

| Signal | Phase | Sleeve | Gate Date | Status |
|--------|-------|--------|-----------|--------|
| `peg_ratio` | 0 (existing) | ValueRisk | — | Shadow (existing) |
| `valuation_adj` / `total_score_adj` / `recommendation_rank_with_valuation` | 0 (existing) | Overlay | 2026-06-02 | Shadow (existing) |
| `altman_z_score` | 1 | ValueRisk | 2026-07-04 | Shadow |
| `interest_coverage` | 1 | ValueRisk | 2026-07-04 | Shadow |
| `fcf_negative_years_5` | 1 | ValueRisk | 2026-07-04 | Shadow |
| `ruin_risk_score` | 2 | ValueRisk | 2026-07-04 | Shadow |
| `revenue_cagr_10y` | 3 | Growth | 2026-07-04 | Shadow |
| `eps_cagr_10y` | 3 | Growth | 2026-07-04 | Shadow |
| `fcf_cagr_10y` | 3 | Growth | 2026-07-04 | Shadow |
| `lindy_score` | 3 | Growth | 2026-07-04 | Shadow |
| `sbc_to_revenue` | 4 | Governance | 2026-07-04 | Shadow |
| `insider_net_shares_3m` | 4 | Governance | 2026-07-04 | Shadow |
| `ceo_pay_ratio` | 4 | Governance | 2026-07-04 | Shadow |

***

## Workstream 2: AWS EC2 Deployment

RICH migrated from Windows Task Scheduler (local machine) to AWS EC2 for unattended daily execution.

### Infrastructure

| Component | Detail |
|-----------|--------|
| Instance | EC2 t3.micro, Ubuntu 24.04 LTS |
| Region | us-east-1 (N. Virginia) |
| Public IP | 3.86.86.145 (stable while instance runs) |
| Storage | EBS gp3 12GB (persistent across reboots) |
| Python | 3.14.4 (system default — no PPA required) |
| Key file | `C:\Users\chaim\Desktop\RICH\rich-prod-key.pem` |
| FMP API key | `/home/ubuntu/RICH/.env` (chmod 600) |

### Cron Schedule

```
# RICH daily run — 08:00 MYT (00:00 UTC) weekdays
0 0 * * 1-5 cd /home/ubuntu/RICH && /home/ubuntu/venv/bin/python run_daily.py >> /home/ubuntu/RICH/logs/cron.log 2>&1

# Daily DB backup to S3 — 09:00 MYT (01:00 UTC) weekdays
0 1 * * 1-5 aws s3 cp /home/ubuntu/RICH/data/cache.db s3://rich-db-backup-chaim/cache-$(date +\%Y-\%m-\%d).db
```

### Cost

| Period | Monthly cost |
|--------|-------------|
| Months 1–12 (Free Tier) | $0.00 |
| Month 13+ (always-on) | ~$8.50 USD (~RM38) |
| Month 13+ (with start/stop automation) | ~$1.35 USD (~RM6) |

### Ongoing Operations

**SSH in:**
```powershell
ssh -i "C:\Users\chaim\Desktop\RICH\rich-prod-key.pem" ubuntu@3.86.86.145
```

**View today's pick:**
```bash
cd /home/ubuntu/RICH && source /home/ubuntu/venv/bin/activate
python report_today.py
```

**Push code updates from Windows:**
```powershell
scp -i "C:\Users\chaim\Desktop\RICH\rich-prod-key.pem" -r "C:\Users\chaim\Desktop\RICH\" ubuntu@3.86.86.145:/home/ubuntu/RICH
```

**Check cron log:**
```bash
tail -50 /home/ubuntu/RICH/logs/cron.log
```

**Run governance report:**
```bash
cd /home/ubuntu/RICH && source /home/ubuntu/venv/bin/activate
python governance.py
```

***

## Action Items

| Date | Action |
|------|--------|
| **2026-06-02** ⚠️ OVERDUE | Valuation overlay gate review — promote or extend shadow period (still pending as of 2026-06-04) |
| **2026-07-04** | All Phase 1–4 signal gate review — run `governance.py`, check null rates and alpha |
| **2027-05 (before Free Tier expires)** | Add EC2 start/stop automation (EventBridge + Lambda) to reduce cost from $8.50 → $1.35/month, OR migrate to Oracle Cloud Free Tier (permanent free) |

---

## 9. Session Log — 2026-05-25

### Summary

Engineering quality workstream: built a pre-run integrity validator (`scripts/rich_check.py`) and wired it as a hard gate into `run_daily.py`.

---

### `scripts/rich_check.py` — Pre-Run Integrity Gate

**Goal:** Catch schema drift, pipeline contract breaks, and config errors before any FMP API call is made — especially important under Windows Task Scheduler where failures are silent.

**Five checks (stdlib only; no imports of project modules — uses `ast.parse`):**

| Check | What it verifies |
|---|---|
| `schema/duplicates` | No duplicate column names in `COLUMNS` |
| `schema/columns-in-schema` | Every entry in `COLUMNS` exists as a column in the `SCHEMA` DDL |
| `schema/migrations-in-schema` | Every `ADD COLUMN` in `_MIGRATIONS` exists in `SCHEMA` |
| `pipeline/buildrow` | `_build_row()` return dict keys match `COLUMNS` exactly (static AST parse) |
| `config/EXCLUDED_SECTORS` · `COOLDOWN_DAYS` · `TOP_N` | Required constants present in `config.py` (handles both `Assign` and `AnnAssign`) |
| `config/WEIGHTS_V2` | `WEIGHTS_V2` dict in `scoring.py` sums to 1.0 ±0.001 |
| `report/deps` | `report_today.py` references only columns that exist in `COLUMNS` |
| `scoring/rules` | Every metric key in `scoring.RULES` is returned by `factors.extract()` |

**Output format:** `OK   [check] — detail` / `WARN [check] — detail` / `FAIL [check] — detail`  
**Exit code:** 0 if no FAIL, 1 if any FAIL.  
**`--fix` flag:** auto-corrects `WEIGHTS_V2` sum using AST line numbers for surgical replacement.

**Engineering note:** discovered during development that `EXCLUDED_SECTORS` uses a type-annotated assignment (`frozenset[str] = frozenset(...)`) — `ast.Assign` alone misses it. Fixed by also handling `ast.AnnAssign` in `_assign()`.

**Files created:** `scripts/rich_check.py` (188 lines)

---

## 10. Session Log — 2026-06-01

### Summary

Moomoo brokerage integration (read-only) plus an additive **portfolio-awareness** layer so RICH always evaluates currently-held US positions. Three downstream fixes were required and verified end-to-end — `run_daily.py` now runs to completion (exit 0).

---

### Workstream 1 — Moomoo read-only portfolio sync (`sync_moomoo.py`)

Standalone, additive utility. Imports no RICH module; never places/modifies/cancels an order (account + position *queries* only). Two new tables in `data/cache.db`:

- `moomoo_account_snapshots`, `moomoo_position_snapshots` — `INSERT OR REPLACE`, one shared `snapshot_ts` per run (time series), full broker response in `raw_json` for audit.

**Modes:** `--dry-run` (fetch + preview, no writes) · `--report` (join latest positions ↔ latest `daily_picks`) · `--review` (Phase-5-style exit signals on held positions: `beneish_flag=1`/`f_score≤3`, gone from the last 5 trading days, or `total_score<50`).

**Per-position market:** `trd_market` is derived from the moomoo code prefix (`_market_from_code`: `US.PG`→`US`, `MY.6742`→`MY`), not the query-level `TRD_MARKET`. This makes `held_us_symbols()`'s `WHERE trd_market='US'` filter real — e.g. `6742` (YTL Power, MY) is correctly excluded.

---

### Workstream 2 — `is_held` portfolio awareness (additive; picker logic unchanged)

Guarantees held US positions are persisted to `daily_picks` even when they rank outside the top-5, without touching scoring or daily-pick selection.

- **`data/storage.py`:** two new `daily_picks` columns — `is_held INTEGER DEFAULT 0`, `held_reject_code TEXT` (added to `SCHEMA`, `COLUMNS`, `_MIGRATIONS`). New helper `held_us_symbols(db_path)` reads the latest US snapshot from `moomoo_position_snapshots` (empty frozenset if absent — graceful; WARNs if stale >1 day).
- **`pipeline.py` `[5b/7]`:** after the top-N rows are built, held symbols are merged — already-present rows get `is_held=1` stamped; scored-below-top-N symbols are appended with their true cross-sectional rank; screened / out-of-universe holdings get a NULL-score stub carrying `held_reject_code` (`BENEISH_FLAG`/`HIST_LT_5`/`OCF_NEGATIVE`/`OUTSIDE_UNIVERSE`). Per-symbol reject codes are now captured in the screen loop.
- **Invariants:** scoring cross-section (μ/σ) unchanged; no new FMP calls; `is_daily_pick` selection untouched (stubs forced `is_daily_pick=0`). Cooldown unaffected — `cooldown_map` keys on `is_daily_pick=1` only.
- **First live run:** `[5b/7] held augmentation: scored_below_top5=2 screened=0 outside_universe=3 (held set=6)`.

---

### Workstream 3 — Engineering fixes (surfaced by running the full pipeline)

| Fix | File | Why |
|---|---|---|
| `check_schema()` filters `_MIGRATIONS` to `daily_picks` ALTERs | `scripts/rich_check.py` | `research_universe` ALTERs (`rule_version`) were checked against the daily_picks schema only → `FAIL` → `run_daily.py` aborted daily. Now exit 0. |
| `_fetch_top5()` adds `AND recommendation_rank IS NOT NULL` | `notification/email_sender.py` | NULL-rank `is_held` stubs sort first under `ORDER BY recommendation_rank` → pick fell back to a NULL stub → `NoneType.__format__` crash. |
| `get_top5()` adds the same filter | `daily_view.py` | Same NULL-stub pollution of the Top-5 display. |

`report_today.py` audited — no change needed (selects `is_daily_pick=1`, never a NULL stub).

---

### Verified status

- `python scripts/rich_check.py` → exit 0, zero FAIL.
- `python run_daily.py` → `[7/7] done`, exit 0; `[5b/7]` fires; `build_email_body()` returns cleanly (no format error).
- `python sync_moomoo.py --dry-run` → `6742` shows `mkt=MY`; US holdings `mkt=US`.

### Known issues / follow-ups

- **Local email send fails** with Gmail `535 BadCredentials` — environmental (SMTP creds live on EC2, not the local box); the format bug is fixed, so it will send on EC2.
- **`RICH_Daily_0800_MYT` / `RICH_Daily_1200_MYT`** scheduled tasks show `Last Result -2147020576` (failed); `RICH_Track_Performance` = 0. Investigate (possibly the now-fixed `rich_check` abort, or `Interactive only` logon mode).
- **`RICH_Moomoo_Sync` task** (Mon–Fri 08:30, wake-to-run, append `logs/sync_moomoo.log`) — commands handed to the operator but **not yet created**. `schtasks` cannot set Wake-To-Run or Description; use the PowerShell `Register-ScheduledTask` / `Set-ScheduledTask` path.
- **`held_us_symbols()` against the stored DB** still returns `6742` until a fresh `sync_moomoo.py` run persists the corrected `trd_market`.
- **Valuation overlay decision gate (2026-06-02)** still pending.

### Files changed

`sync_moomoo.py` *(new)* · `data/storage.py` · `pipeline.py` · `scripts/rich_check.py` · `notification/email_sender.py` · `daily_view.py` · this document.

---

### `run_daily.py` — Pre-Run Gate Wired

Added 7 lines at the top of `run_daily.py` (before any project imports) that:
1. Run `scripts/rich_check.py` via `subprocess.run`
2. Print stdout + stderr if the check fails
3. Call `sys.exit("rich_check FAILED — pipeline aborted")` — Task Scheduler sees exit code 1

**Design:** subprocess isolates the check from module-level side effects (env vars, DB connections) that fire when `config.py` / `pipeline.py` are imported.

**Files changed:** `run_daily.py`

---

### MD Corrections Applied This Session

| Item | Old | New |
|---|---|---|
| `daily_picks` column count | 54 | 57 |
| Missing column groups | — | `overlay_candidate_sym`, `overlay_candidate_price`, `is_backfill` added to schema table |
| `run_daily.py` module description | "initialises logging and calls pipeline.run()" | updated to mention `rich_check.py` gate |

---

## 11. Session Log — 2026-06-04

### Summary

Status-reconciliation and unblock session. Verified the live runtime, fixed the Moomoo sync hang, and refreshed this document to match reality. No changes to architecture, scoring logic, or methodology.

### Runtime reality check

- The **live daily run is local Windows Task Scheduler**, not AWS EC2. Both pipeline tasks (`RICH_Daily_1200_MYT` @ 03:00, `RICH_Daily_0800_MYT` @ 04:00) and `RICH_Track_Performance` (@ 07:00) returned **Last Result 0** on 2026-06-04. Today produced two runs → picks **TMDX** (03:16, rank 10) and **GPOR** (04:01, rank 14, the latest/authoritative pick).
- The two `0800/1200` task names are historical and no longer match their actual start times — flagged, not yet renamed.
- Non-fatal, recurring on every run: Gmail email send fails locally (`535 BadCredentials` — SMTP creds live on EC2, not the local box); Moomoo held-set logged stale (snapshot dated 2026-06-02).

### `sync_moomoo.py` — fail-fast OpenD retry cap

`RICH_Moomoo_Sync` was failing: the OpenD gateway (`127.0.0.1:11111`) is down, and the moomoo client retried a refused connection **indefinitely** (140+ `WSAECONNREFUSED` before manual kill). Added a minimal preflight, enforced once at the top of `fetch_snapshots()`:

- `CONNECT_MAX_RETRIES = 3` TCP probes (2s timeout, 1s apart) before `OpenSecTradeContext` is ever constructed.
- On all-fail → `SystemExit` with a clear message and **non-zero exit**, in ~9s, **no DB write**. When the port is up, returns immediately — behavior unchanged.
- No schema or business-logic change. The pure-DB modes (`--report`, `--review`) are unaffected.
- The scheduled task remains **disabled pending OpenD availability**. Note: the task's separate `0xC0000142` was a non-interactive-session init failure — the retry cap fixes the *hang*, not that; OpenD must actually be running at trigger time.

### Document corrections applied this session

| Item | Old | New |
|---|---|---|
| Stack / live runtime | AWS EC2 t3.micro · cron | **Windows Task Scheduler (local)** + runtime note |
| Daily schedule table | EC2 cron + S3 backup | actual local tasks + last results |
| Moomoo sync status | ✅ Live (2026-06-01) | 🟡 code live; task created + retry cap added; **disabled pending OpenD** |
| Phase 6 results table | 2 picks (as of 2026-05-06) | 17 tracked picks from `pick_outcomes` (as of 2026-06-04) |
| Valuation overlay gate | gate 2026-06-02 (pending) | ⚠️ **OVERDUE** — decision still pending |

### Open / carried-forward

- **Valuation overlay decision (gate 2026-06-02) is overdue** — promote / extend / remove still pending.
- **`RICH_Moomoo_Sync`** stays disabled until OpenD is reliably up at trigger time; revisit the `0xC0000142` logon-mode issue then.
- **Email** send still fails on local runs (no SMTP creds locally). *(Resolved 2026-06-10 — feature removed; see Session Log 2026-06-10/11.)*
- **Task rename** — `0800/1200` names vs actual 03:00/04:00 start times (cosmetic).
- **2026-07-04** — all Phase 1–4 shadow-signal gate reviews remain scheduled.

---

## 12. Session Log — 2026-06-10/11

### Email notification removed (replaced by Discord)

The Gmail/SMTP daily-pick email — failing on every local run with `535 BadCredentials` since the EC2→local migration — was removed rather than repaired:

- **Deleted** `notification/email_sender.py` (417 lines; never committed to git, so no deletion record). `notification/__init__.py` retained (empty package).
- **`run_daily.py`**: removed only the email `try/except` block at the end of `main()`; the rich_check gate, `run()` call, `append_run_metrics`, and pick logging are unchanged. No dead imports (all email imports were block-local).
- Verified: `scripts/rich_check.py` exit 0 (all 10 checks OK), `py_compile` clean, project-wide grep for `smtp|email|EMAIL|SMTP|send_mail` in `*.py` returns zero hits.
- Closes the carried-forward "Email send still fails on local runs" item from Session Log 2026-06-04.

### `discord_daily_report.py` (new, project root)

Standalone replacement notifier. stdlib + `requests` only; no imports from any core RICH module; reads `data/cache.db` **read-only**.

- Posts a plain-text daily summary (pick line, valuation/price, why-line, top-5 with `held`/`cd` states) to `DISCORD_WEBHOOK_URL` (user env). `--dry-run` prints instead of posting; missing env var without `--dry-run` exits 1 with a clear error.
- "Today" = `MAX(run_date)` from `daily_picks` — never the system clock.
- **Multi-run-day rule:** dates with several pipeline runs store duplicate ranks and multiple `is_daily_pick=1` rows with no timestamp column; the script resolves both by highest `rowid` (latest insert wins).
- Tested: dry-run exit 0 against 2026-06-10 data (CARG); error path verified; `rich_check` still 0. **Untested:** the no-pick and no-top-5 branches (would require writing test rows).
- **Scheduling prepared but NOT registered:** `run_discord_daily.cmd` wrapper + `schtasks` line for `RICHDiscordDaily0810` (weekdays, `/RU` current user, no stored password) were drafted only. ⚠️ Timing: full pipeline runs take ~16–18 min, so an 08:10 trigger after an 08:00 pipeline start would post stale data — **08:30 recommended** if the pipeline task fires at 08:00.

### Valuation overlay gate (overdue since 2026-06-02) — decision recorded: EXTEND

Two governance artifacts created under `docs/` (documentation-only; no config, schema, or overlay code touched):

- **`docs/overlay_gate_2026-06-10.md`** — divergence analysis over 2026-05-03→2026-06-10: overlay disagrees with the raw pick on **22/24 runs (14/16 dates)**; agrees only when the raw pick was already rank-1 deep_discount (CALM, BCRX). Verdict: **EXTEND — do not promote**.
- **`docs/overlay_backtest_2026-06-10.md`** — first-pass realized-alpha comparison from `pick_outcomes`: mean raw alpha **+1.34%** vs overlay **−3.17%** (paired diff +4.52 pp, t=1.81, p≈0.10, 12 rows / 6 dates). Evidence leans *against* the overlay but the sample is confounded (effectively one WDC bet plus DUOL, duplicate intraday runs, 2–9-day horizons). Verdict: **EXTEND**.
- **New gate deadline: 2026-07-31** — before then: per-candidate, cooldown-aware outcome tracking so overlay vs raw can be compared on independent, same-horizon observations.

### Open / carried-forward (delta vs 2026-06-04)

- ~~Email send fails locally~~ → **closed** (feature removed).
- Valuation overlay gate → **decision recorded: EXTEND**, new gate 2026-07-31 (was: overdue with no decision).
- `RICHDiscordDaily*` task registration + `setx DISCORD_WEBHOOK_URL` — **pending, manual** (commands drafted; start-time decision 08:10 vs 08:30 open).
- Moomoo snapshot staleness (last 2026-06-02) and `RICH_Moomoo_Sync`/OpenD — unchanged, still open.
- ~1,700 lines of uncommitted changes across 14 tracked files (plus untracked items incl. a stray file named `=`) — flagged 2026-06-10; commit/stash recommended before further unattended runs.
