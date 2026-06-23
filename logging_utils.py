"""Shared logging helpers: daily-rotating log file + run-metrics CSV.

Replaces the inline setup_logging() in run_daily.py and the run_log.py
append_run_metrics() with a single module that owns both concerns.

Metrics tracked per run
-----------------------
Timing        : elapsed_s, t_universe_s, t_screen_s, t_valuation_s,
                t_cooldown_s, t_persist_s
Universe funnel: universe_raw_total, universe_dropped_sym,
                universe_dropped_vol, universe_eligible
Screen/score  : total_screened, scored, FETCH_ERROR, MISSING_FIELDS,
                NI_OCF_NONPOSITIVE, HIST_LT_5, REV_NONPOSITIVE, fetch_error_pct
Portfolio     : raw_top_count, in_cooldown_count, daily_pick,
                daily_pick_rank, daily_pick_score
"""
from __future__ import annotations

import csv
import logging
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from config import LOG_DIR, TIMEZONE


def setup_logging(log_dir: Path = LOG_DIR, timezone: str = TIMEZONE) -> None:
    """Configure root logger: logs/<YYYY-MM-DD>.log (MYT date) + stdout."""
    log_dir.mkdir(parents=True, exist_ok=True)
    today = datetime.now(ZoneInfo(timezone)).strftime("%Y-%m-%d")
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-8s %(name)s %(message)s",
        handlers=[
            logging.FileHandler(log_dir / f"{today}.log", encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
        force=True,
    )


def append_run_metrics(
    result: dict,
    log_dir: Path = LOG_DIR,
    timezone: str = TIMEZONE,
) -> Path:
    """Append one pipeline run record to logs/run_metrics.csv.

    Expects result dict as returned by pipeline.run().  Any missing key is
    written as an empty string so the CSV column count stays consistent.
    Returns the path to the CSV file.
    """
    log_dir.mkdir(parents=True, exist_ok=True)
    csv_path = log_dir / "run_metrics.csv"

    now = datetime.now(ZoneInfo(timezone))
    rejects = result.get("rejects") or {}
    scored = result.get("scored") or 0
    ucounts = result.get("universe_counts") or {}
    total_screened = scored + sum(rejects.values())
    fetch_err = rejects.get("FETCH_ERROR", 0)

    raw_top: list = result.get("raw_top") or []
    pick = result.get("daily_pick")
    stage_t = result.get("stage_timings") or {}

    row = {
        # ── wall-clock ──────────────────────────────────────────────────────
        "run_datetime":         now.isoformat(),
        "run_date":             now.date().isoformat(),
        "run_time_my":          now.strftime("%H:%M:%S"),
        "elapsed_s":            result.get("elapsed_s", 0),
        # ── universe funnel ─────────────────────────────────────────────────
        "universe_raw_total":    ucounts.get("raw_total", ""),
        "universe_dropped_sym":  ucounts.get("dropped_symbol_filter", ""),
        "universe_dropped_sec":  ucounts.get("dropped_sector", ""),
        "universe_dropped_adr":  ucounts.get("dropped_adr", ""),
        "universe_dropped_vol":  ucounts.get("dropped_dollar_volume", ""),
        "universe_eligible":     ucounts.get("eligible", ""),
        # ── screen / score ──────────────────────────────────────────────────
        "total_screened":       total_screened,
        "scored":               scored,
        # ── reject breakdown ────────────────────────────────────────────────
        "FETCH_ERROR":          fetch_err,
        "MISSING_FIELDS":       rejects.get("MISSING_FIELDS", 0),
        "NI_OCF_NONPOSITIVE":   rejects.get("NI_OCF_NONPOSITIVE", 0),
        "HIST_LT_5":            rejects.get("HIST_LT_5", 0),
        "REV_NONPOSITIVE":      rejects.get("REV_NONPOSITIVE", 0),
        "fetch_error_pct":      (
            round(100.0 * fetch_err / total_screened, 1) if total_screened else 0.0
        ),
        # ── portfolio selection ─────────────────────────────────────────────
        "raw_top_count":        len(raw_top),
        "in_cooldown_count":    sum(1 for c in raw_top if c.get("in_cooldown")),
        "daily_pick":           pick["symbol"] if pick else "-",
        "daily_pick_rank":      pick.get("recommendation_rank", "") if pick else "",
        "daily_pick_score":     (
            pick["scoring"]["total_score"] if pick else ""
        ),
        # ── per-stage timings (seconds; empty when pipeline doesn't track) ──
        "t_universe_s":         stage_t.get("universe", ""),
        "t_screen_s":           stage_t.get("screen", ""),
        "t_score_s":            stage_t.get("score", ""),
        "t_valuation_s":        stage_t.get("valuation", ""),
        "t_cooldown_s":         stage_t.get("cooldown", ""),
        "t_persist_s":          stage_t.get("persist", ""),
        # ── §3 Beneish growth exemption (log-only count; appended last so the
        #    existing column order is preserved) ──────────────────────────────
        "BENEISH_GROWTH_EXEMPT": result.get("beneish_growth_exempt", 0),
    }

    exists = csv_path.exists()
    with open(csv_path, "a", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=row.keys())
        if not exists:
            w.writeheader()
        w.writerow(row)

    return csv_path
