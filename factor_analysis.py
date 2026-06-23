"""
factor_analysis.py — Alphalens Reloaded factor analysis for RICH daily_picks.

Loads factor values from data/cache.db (read-only), fetches adjusted close
prices via yfinance, and produces IC stats, quantile returns, and factor
decay charts.

Setup (one-time):
    pip install alphalens-reloaded yfinance

Usage:
    python factor_analysis.py
    python factor_analysis.py --factors f_score revenue_cagr_5y pe_vs_own_history
    python factor_analysis.py --quantiles 5 --periods 1 5 10 21

⚠ Sample-size note:
    With --source daily_picks (default), RICH stores only top-5 rows per day —
    treat results as exploratory only.

    For production-grade analysis use --source research_universe after enabling
    the full-universe snapshot: pipeline.run(persist_full_universe=True).
    This gives ~700-900 rows/day from the full passing screener cross-section.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import warnings
from pathlib import Path

import matplotlib
matplotlib.use("Agg")  # non-interactive; safe for scheduled runs
import matplotlib.pyplot as plt
import pandas as pd

try:
    import alphalens
except ImportError:
    sys.exit(
        "alphalens-reloaded not installed.\n"
        "Run: pip install alphalens-reloaded"
    )

try:
    import yfinance as yf
except ImportError:
    sys.exit(
        "yfinance not installed.\n"
        "Run: pip install yfinance"
    )

warnings.filterwarnings("ignore", category=FutureWarning)

# ── Configuration ─────────────────────────────────────────────────────────────

DB_PATH    = Path(__file__).parent / "data" / "cache.db"
TABLE      = "daily_picks"
OUTPUT_DIR = Path(__file__).parent / "factor_output"

# Top 3 defaults: composite quality signal, 5yr growth anchor, relative value
DEFAULT_FACTORS = ["f_score", "revenue_cagr_5y", "pe_vs_own_history"]

# Forward-return windows in trading days
DEFAULT_PERIODS = (1, 5, 10, 21)

# Quantile buckets — drop to 4 for small datasets (<200 rows)
DEFAULT_QUANTILES = 4

# Tolerate up to 40% data loss during Alphalens merge (needed for sparse data)
MAX_LOSS = 0.40

# All factor columns available in daily_picks
ALL_FACTORS = [
    "returnOnEquity", "returnOnAssets", "grossProfitMargin",
    "operatingProfitMargin", "operatingCashFlowToNetIncome",
    "f_score", "novy_marx_gp_assets",
    "revenueGrowth", "revenue_cagr_5y", "eps_cagr_5y",
    "fcf_cagr_5y", "asset_growth",
    "debtToEquityRatio", "priceEarningsRatio",
    "ev_ebitda", "ps_ratio", "pe_vs_own_history",
]


# ── Data loading ──────────────────────────────────────────────────────────────

def load_factor_data(db_path: Path, source: str = "daily_picks") -> pd.DataFrame:
    """Return factor DataFrame with MultiIndex (date, asset), read-only.

    source: "daily_picks"      — top-5 rows per day (sparse; default for back-compat)
            "research_universe" — full passing screener universe (~700-900 rows/day;
                                  requires pipeline.run(persist_full_universe=True))
    """
    if source not in ("daily_picks", "research_universe"):
        raise ValueError(f"Unknown source: {source!r}")

    # research_universe has universe_rank; daily_picks doesn't — request only shared cols
    common_cols = ["run_date", "symbol"] + ALL_FACTORS
    col_clause = ", ".join(common_cols)

    # For research_universe, add universe_rank so callers can filter to top-N if desired
    if source == "research_universe":
        col_clause = col_clause + ", universe_rank, in_top20"

    uri = f"file:{db_path.resolve().as_posix()}?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        df = pd.read_sql_query(
            f"SELECT {col_clause} FROM {source} "
            f"WHERE run_date IS NOT NULL AND symbol IS NOT NULL",
            conn,
        )
    finally:
        conn.close()

    df["run_date"] = pd.to_datetime(df["run_date"])
    df = df.rename(columns={"run_date": "date", "symbol": "asset"})
    df = df.set_index(["date", "asset"]).sort_index()
    return df


# ── Price fetching ─────────────────────────────────────────────────────────────

def fetch_prices(
    symbols: list[str],
    start: str,
    end: str,
    extra_days: int = 30,
) -> pd.DataFrame:
    """Download adjusted close prices via yfinance.

    Fetches `extra_days` beyond `end` so the longest forward-return period
    has price data without running off the end of history.
    """
    from datetime import datetime, timedelta
    end_ext = (
        datetime.strptime(end, "%Y-%m-%d") + timedelta(days=extra_days)
    ).strftime("%Y-%m-%d")

    print(f"  Downloading prices for {len(symbols)} symbols ({start} → {end_ext})...")
    raw = yf.download(
        symbols,
        start=start,
        end=end_ext,
        auto_adjust=True,
        progress=False,
    )

    if raw.empty:
        raise RuntimeError("yfinance returned no price data.")

    # Multi-ticker returns MultiIndex columns; single ticker returns flat columns
    prices: pd.DataFrame
    if isinstance(raw.columns, pd.MultiIndex):
        prices = raw["Close"].copy()
    else:
        prices = raw[["Close"]].rename(columns={"Close": symbols[0]}).copy()

    prices.index = pd.to_datetime(prices.index)
    prices.index.name = "date"
    prices.columns.name = "asset"

    # Drop symbols with >50% missing prices (delisted, name-change, etc.)
    coverage = prices.notna().mean()
    good = coverage[coverage >= 0.50].index.tolist()
    dropped = sorted(set(prices.columns) - set(good))
    if dropped:
        print(f"  Dropped {len(dropped)} symbols with <50% price coverage: {dropped}")
    prices = prices[good]

    print(f"  Price matrix: {prices.shape[0]} dates × {prices.shape[1]} symbols")
    return prices


# ── Analysis ───────────────────────────────────────────────────────────────────

def _build_factor_series(df: pd.DataFrame, col: str) -> pd.Series:
    s = df[col].dropna()
    s.index.names = ["date", "asset"]
    return s


def _print_ic_summary(ic: pd.DataFrame) -> None:
    n = len(ic)
    summary = pd.DataFrame({
        "Mean IC":     ic.mean(),
        "IC Std":      ic.std(),
        "IC t-stat":   ic.mean() / (ic.std() / (n ** 0.5)),
        "IC>0 (%)":    (ic > 0).mean() * 100,
    }).round(4)
    print(summary.to_string())


def _save_ic_chart(ic: pd.DataFrame, factor_name: str, out: Path) -> None:
    periods = ic.columns.tolist()[:2]  # first 2 periods for readability
    fig, axes = plt.subplots(len(periods), 1, figsize=(13, 4 * len(periods)))
    if len(periods) == 1:
        axes = [axes]
    fig.suptitle(f"IC over Time — {factor_name}", fontsize=14, fontweight="bold")
    for ax, col in zip(axes, periods):
        ic[col].plot(ax=ax, color="steelblue", alpha=0.5, linewidth=0.9, label="Daily IC")
        ic[col].rolling(min(21, len(ic))).mean().plot(
            ax=ax, color="tomato", linewidth=2, label="Rolling mean"
        )
        ax.axhline(0, color="black", linewidth=0.8, linestyle="--")
        ax.set_title(f"Period: {col}")
        ax.set_ylabel("IC")
        ax.legend(fontsize=8)
    plt.tight_layout()
    fig.savefig(out / "ic_over_time.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def _save_quantile_chart(
    mean_ret: pd.DataFrame, factor_name: str, out: Path
) -> None:
    fig, ax = plt.subplots(figsize=(11, 6))
    mean_ret.plot(kind="bar", ax=ax, colormap="RdYlGn", width=0.7)
    ax.set_title(
        f"Mean Forward Return by Quantile — {factor_name}",
        fontsize=13, fontweight="bold",
    )
    ax.set_xlabel("Quantile (1 = lowest factor, N = highest)")
    ax.set_ylabel("Mean Return")
    ax.axhline(0, color="black", linewidth=0.8)
    ax.legend(title="Forward period", bbox_to_anchor=(1.01, 1), loc="upper left", fontsize=8)
    ax.tick_params(axis="x", rotation=0)
    plt.tight_layout()
    fig.savefig(out / "quantile_returns.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def _save_decay_chart(
    factor_data: pd.DataFrame, factor_name: str, lags: tuple[int, ...], out: Path
) -> None:
    """Mean factor autocorrelation at multiple lags — proxy for signal half-life."""
    decay: dict[str, float] = {}
    for lag in lags:
        try:
            ac = alphalens.performance.factor_autocorrelation(factor_data, period=lag)
            decay[f"{lag}d"] = ac.mean()
        except Exception:
            pass

    if not decay:
        return

    fig, ax = plt.subplots(figsize=(9, 5))
    bars = ax.bar(decay.keys(), decay.values(), color="steelblue", alpha=0.85, width=0.5)
    for bar, val in zip(bars, decay.values()):
        ax.text(
            bar.get_x() + bar.get_width() / 2,
            val + (0.005 if val >= 0 else -0.015),
            f"{val:.3f}", ha="center", va="bottom", fontsize=9,
        )
    ax.axhline(0, color="black", linewidth=0.8)
    ax.set_title(
        f"Factor Autocorrelation by Lag — {factor_name}\n"
        f"(higher = signal persists longer)",
        fontsize=12, fontweight="bold",
    )
    ax.set_ylabel("Mean Autocorrelation")
    ax.set_xlabel("Lag")
    plt.tight_layout()
    fig.savefig(out / "factor_decay.png", dpi=150, bbox_inches="tight")
    plt.close(fig)


def analyse_factor(
    factor_name: str,
    factor: pd.Series,
    prices: pd.DataFrame,
    output_dir: Path,
    quantiles: int,
    periods: tuple[int, ...],
) -> None:
    print(f"\n{'='*62}")
    print(f"  FACTOR: {factor_name}   (n={len(factor):,} observations)")
    print(f"{'='*62}")

    if len(factor) < 30:
        print(
            f"  ⚠ Only {len(factor)} rows — IC stats will not be reliable.\n"
            "  See setup note at top of this file for how to fix this."
        )

    try:
        factor_data = alphalens.utils.get_clean_factor_and_forward_returns(
            factor,
            prices,
            quantiles=quantiles,
            bins=None,
            periods=periods,
            max_loss=MAX_LOSS,
        )
    except Exception as exc:
        print(f"  ERROR: could not build factor data — {exc}")
        return

    print(f"  Clean observations after merge: {len(factor_data):,}\n")

    # ── IC ────────────────────────────────────────────────────────────────────
    print("── Information Coefficient (IC) ─────────────────────────────")
    ic = alphalens.performance.factor_information_coefficient(factor_data)
    _print_ic_summary(ic)

    # ── Quantile returns ──────────────────────────────────────────────────────
    print("\n── Mean Returns by Quantile ──────────────────────────────────")
    mean_ret, _ = alphalens.performance.mean_return_by_quantile(factor_data)
    print(mean_ret.applymap(lambda x: f"{x:.4%}" if pd.notna(x) else "n/a").to_string())

    # ── Autocorrelation ───────────────────────────────────────────────────────
    print("\n── Factor Autocorrelation (signal decay) ─────────────────────")
    for lag in periods:
        try:
            ac = alphalens.performance.factor_autocorrelation(factor_data, period=lag)
            print(f"  Lag {lag:2d}d — mean={ac.mean():.4f}  std={ac.std():.4f}")
        except Exception:
            print(f"  Lag {lag:2d}d — unavailable")

    # ── Charts ────────────────────────────────────────────────────────────────
    factor_dir = output_dir / factor_name
    factor_dir.mkdir(parents=True, exist_ok=True)

    _save_ic_chart(ic, factor_name, factor_dir)
    _save_quantile_chart(mean_ret, factor_name, factor_dir)
    _save_decay_chart(factor_data, factor_name, periods, factor_dir)

    print(f"\n  Charts → {factor_dir}/")
    print(f"    ic_over_time.png   quantile_returns.png   factor_decay.png")


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(
        description="RICH factor analysis via Alphalens Reloaded",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--factors", nargs="+", default=DEFAULT_FACTORS,
        metavar="FIELD",
        help=f"Factor columns to analyse. Default: {DEFAULT_FACTORS}",
    )
    parser.add_argument(
        "--quantiles", type=int, default=DEFAULT_QUANTILES,
        help="Quantile buckets (default: 4; use 5 for large datasets)",
    )
    parser.add_argument(
        "--periods", nargs="+", type=int, default=list(DEFAULT_PERIODS),
        metavar="N",
        help="Forward-return windows in trading days (default: 1 5 10 21)",
    )
    parser.add_argument(
        "--db", type=Path, default=DB_PATH,
        help=f"Path to cache.db (default: {DB_PATH})",
    )
    parser.add_argument(
        "--source",
        choices=("daily_picks", "research_universe"),
        default="daily_picks",
        help=(
            "Table to load factor data from. "
            "'research_universe' gives ~700-900 rows/day but requires "
            "pipeline.run(persist_full_universe=True)."
        ),
    )
    args = parser.parse_args()

    # Validate
    bad = [f for f in args.factors if f not in ALL_FACTORS]
    if bad:
        sys.exit(
            f"Unknown factor(s): {bad}\n"
            f"Valid options:\n  {ALL_FACTORS}"
        )

    OUTPUT_DIR.mkdir(exist_ok=True)
    periods = tuple(sorted(args.periods))

    # ── 1. Load factor data ───────────────────────────────────────────────────
    print(f"Loading factor data from {args.db} (source={args.source})...")
    df = load_factor_data(args.db, source=args.source)

    dates = df.index.get_level_values("date")
    assets = df.index.get_level_values("asset")
    print(
        f"  {len(df):,} rows  |  "
        f"{dates.min().date()} → {dates.max().date()}  |  "
        f"{assets.nunique()} unique symbols"
    )

    if df.empty:
        sys.exit("No data found in daily_picks. Has the pipeline been run?")

    # ── 2. Fetch prices ───────────────────────────────────────────────────────
    symbols = assets.unique().tolist()
    start   = dates.min().strftime("%Y-%m-%d")
    end     = dates.max().strftime("%Y-%m-%d")

    print("\nFetching price data via yfinance...")
    try:
        prices = fetch_prices(symbols, start, end, extra_days=max(periods) + 10)
    except Exception as exc:
        sys.exit(f"Price fetch failed: {exc}")

    # ── 3. Analyse each factor ────────────────────────────────────────────────
    for name in args.factors:
        factor = _build_factor_series(df, name)
        if factor.empty:
            print(f"\nSkipping {name} — all values are null.")
            continue
        analyse_factor(name, factor, prices, OUTPUT_DIR, args.quantiles, periods)

    print(f"\n{'='*62}")
    print(f"  All output written to: {OUTPUT_DIR}/")
    print(f"{'='*62}")


if __name__ == "__main__":
    main()
