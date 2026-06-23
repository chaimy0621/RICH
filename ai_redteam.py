"""
AI red team analysis for the RICH daily pick.

Reads: daily_picks (SQLite), FMP day-cache (JSON)
Writes: nothing to DB — purely read-only against existing tables
Saves: optional markdown to redteam/YYYY-MM-DD_TICKER_redteam.md

Requires:
    ANTHROPIC_API_KEY env var
    pip install anthropic  (one-time)

Usage:
    py ai_redteam.py                                       # today's pick
    py ai_redteam.py --date 2026-05-14                     # specific date
    py ai_redteam.py --notes "CEO sold 200k shares; FDA hearing next month"
    py ai_redteam.py --notes-file my_research.txt
    py ai_redteam.py --ticker BKNG                         # red-team a non-pick from top-5
    py ai_redteam.py --save                                # write markdown to redteam/
    py ai_redteam.py --model claude-sonnet-4-6             # deeper analysis, higher cost
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from datetime import date
from pathlib import Path

try:
    import anthropic
except ImportError:
    sys.exit(
        "ERROR: pip install anthropic\n"
        "       ANTHROPIC_API_KEY must also be set in .env or your environment."
    )

# ── paths (no imports from RICH core) ────────────────────────────────────────
ROOT    = Path(__file__).parent

# Load .env so ANTHROPIC_API_KEY can live alongside FMP_API_KEY
try:
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
except ImportError:
    pass  # dotenv optional; fall back to os.environ
DB_PATH = ROOT / "data" / "cache.db"
FMP_DIR = ROOT / "data"
OUT_DIR = ROOT / "redteam"

DEFAULT_MODEL = "claude-haiku-4-5-20251001"   # fast + cheap for daily use
MAX_TOKENS    = 1400


# ── formatting helpers ────────────────────────────────────────────────────────

def _pct(v) -> str:
    if v is None:
        return "n/a"
    try:
        return f"{round(float(v) * 100, 1)}%"
    except (TypeError, ValueError):
        return "n/a"


def _num(v, d: int = 2) -> str:
    if v is None:
        return "n/a"
    try:
        return str(round(float(v), d))
    except (TypeError, ValueError):
        return "n/a"


def _s(v) -> str:
    return str(v) if v is not None else "n/a"


# ── data loading ──────────────────────────────────────────────────────────────

def load_top5(run_date: str) -> list[dict]:
    if not DB_PATH.exists():
        sys.exit(f"ERROR: database not found: {DB_PATH}")
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    try:
        rows = conn.execute(
            "SELECT * FROM daily_picks"
            " WHERE run_date = ?"
            " ORDER BY recommendation_rank LIMIT 5",
            (run_date,),
        ).fetchall()
    finally:
        conn.close()
    if not rows:
        sys.exit(
            f"ERROR: no picks found for {run_date}.\n"
            "       Has the pipeline run today? Try --date YYYY-MM-DD."
        )
    return [dict(r) for r in rows]


def load_fmp(run_date: str, symbol: str) -> dict:
    path = FMP_DIR / f"fmp_cache_{run_date}.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8")).get(symbol, {})
    except Exception:
        return {}


# ── prompt construction ───────────────────────────────────────────────────────

# System prompt is static → eligible for prompt caching on every repeated run.
_SYSTEM = """\
You are a professional short-seller and adversarial risk analyst. Your assignment \
is to produce the strongest possible bear case against a stock that a quantitative \
model has just selected as its top pick.

Ground rules:
- Assume the quantitative data given to you is accurate. Do not re-argue the numbers.
- Focus exclusively on what financial statements CANNOT capture: competitive moat \
erosion, key-person risk, customer concentration, regulatory exposure, technology \
disruption, accounting edge cases the Beneish score may miss, and macro dependencies \
the model is blind to.
- Be specific and direct. No hedging phrases like "it is possible that" or "there \
may be risk." Make concrete, falsifiable claims.
- Do not repeat numerical data already supplied. The analyst already has it.
- Your output will be used to stress-test conviction — not to override the model.

Respond using EXACTLY these five section headers, in order, nothing else:

BEARISH THESIS
[2–3 sentences: the strongest plain-language bear case]

KEY RISKS  (most dangerous first)
1. [Label]: [1–2 sentences]
2. ...
[4–6 risks total]

INVALIDATION TRIGGERS
- [Specific event or data point that would kill this bear thesis]
[3–5 triggers]

MONITORING CHECKLIST
- [Specific metric or event to track — state frequency: weekly / monthly / quarterly]
[4–6 items]

VERDICT
[Single line: HOLD THE PICK / REDUCE CONVICTION / AVOID — one concise reason]"""


def _build_user_message(pick: dict, top5: list[dict], fmp: dict, notes: str) -> str:
    sym     = pick["symbol"]
    company = _s(pick.get("company_name"))
    rank    = _s(pick.get("recommendation_rank"))
    score   = _num(pick.get("total_score"), 1)
    q       = _num(pick.get("quality_score"), 1)
    g       = _num(pick.get("growth_score"), 1)
    vr      = _num(pick.get("value_risk_score"), 1)
    val_raw = pick.get("valuation_label") or "n/a"

    # FMP ratios
    r0  = (fmp.get("r")  or [{}])[0]
    g0  = (fmp.get("g")  or [{}])[0]
    km0 = (fmp.get("km") or [{}])[0]

    roe = _pct(r0.get("returnOnEquity") or km0.get("returnOnEquity"))
    roa = _pct(r0.get("returnOnAssets") or km0.get("returnOnAssets"))
    gm  = _pct(r0.get("grossProfitMargin"))
    opm = _pct(r0.get("operatingProfitMargin"))
    rg  = _pct(g0.get("revenueGrowth"))
    pe  = _num(r0.get("priceToEarningsRatio") or r0.get("priceEarningsRatio"))
    de  = _num(r0.get("debtToEquityRatio"))
    cr  = _num(r0.get("currentRatio"))
    npm = _pct(r0.get("netProfitMargin"))

    # daily_picks shadow columns
    rc  = _pct(pick.get("revenue_cagr_5y"))
    ec  = _pct(pick.get("eps_cagr_5y"))
    fc  = _pct(pick.get("fcf_cagr_5y"))
    ag  = _pct(pick.get("asset_growth"))
    ev  = _num(pick.get("ev_ebitda"))
    ps  = _num(pick.get("ps_ratio"))
    peg = _num(pick.get("peg_ratio"))
    pvh = _num(pick.get("pe_vs_own_history"), 2)
    fs  = _s(pick.get("f_score"))
    ms  = _num(pick.get("m_score"), 3)
    top_d = _s(pick.get("top_drivers"))
    bot_d = _s(pick.get("bottom_drivers"))
    hist  = _s(pick.get("historical_years_available"))
    pp    = _num(pick.get("pick_price"))

    top5_lines = "\n".join(
        "  #{rank} {sym:<6} score={score}  {tag:<12} {name}".format(
            rank=r.get("recommendation_rank"),
            sym=r.get("symbol") or "",
            score=_num(r.get("total_score"), 1),
            tag="[PICK]" if r.get("is_daily_pick") else
                "[COOLDOWN]" if r.get("in_cooldown") else "",
            name=r.get("company_name") or "",
        )
        for r in top5
    )

    notes_block = notes.strip() if notes.strip() else "None — use public knowledge only."

    return (
        f"Stock to red-team: {sym} ({company})\n"
        f"Entry price: ${pp}  |  RICH Rank: #{rank}  |  RICH Score: {score}/100\n"
        f"Valuation label: {val_raw}  |  Historical data: {hist} years\n"
        f"\n"
        f"RICH SCORECARD\n"
        f"  Quality  {q}/100 : ROE={roe}  ROA={roa}  Gross Margin={gm}"
        f"  Op Margin={opm}  Net Margin={npm}  F-Score={fs}/9\n"
        f"  Growth   {g}/100 : Rev Growth(YoY)={rg}  Rev CAGR 5y={rc}"
        f"  EPS CAGR 5y={ec}  FCF CAGR 5y={fc}  Asset Growth={ag}\n"
        f"  Val/Risk {vr}/100: P/E={pe}  EV/EBITDA={ev}  P/S={ps}"
        f"  PEG={peg}  D/E={de}  Current Ratio={cr}  PE vs Own History={pvh}\n"
        f"  Beneish M-Score: {ms}  (manipulation threshold: -1.78; lower is cleaner)\n"
        f"\n"
        f"RICH SCORE DRIVERS\n"
        f"  Top factors:    {top_d}\n"
        f"  Bottom factors: {bot_d}\n"
        f"\n"
        f"TODAY'S TOP 5 (context)\n"
        f"{top5_lines}\n"
        f"\n"
        f"ANALYST RESEARCH NOTES\n"
        f"{notes_block}\n"
        f"\n"
        f"Produce the red team analysis now."
    )


# ── API call ──────────────────────────────────────────────────────────────────

def call_claude(user_message: str, model: str) -> tuple[str, dict]:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        sys.exit("ERROR: ANTHROPIC_API_KEY is not set in your environment.")

    client = anthropic.Anthropic(api_key=key)
    resp = client.messages.create(
        model=model,
        max_tokens=MAX_TOKENS,
        system=[
            {
                "type": "text",
                "text": _SYSTEM,
                "cache_control": {"type": "ephemeral"},
            }
        ],
        messages=[{"role": "user", "content": user_message}],
    )

    text = resp.content[0].text
    u = resp.usage
    usage = {
        "input":          u.input_tokens,
        "output":         u.output_tokens,
        "cache_read":     getattr(u, "cache_read_input_tokens", 0),
        "cache_creation": getattr(u, "cache_creation_input_tokens", 0),
    }
    return text, usage


# ── rendering ─────────────────────────────────────────────────────────────────

def render(pick: dict, analysis: str, usage: dict, model: str, run_date: str) -> str:
    sym     = pick["symbol"]
    company = pick.get("company_name") or sym
    score   = _num(pick.get("total_score"), 1)
    SEP     = "=" * 62
    DASH    = "-" * 42

    cache_note = ""
    if usage["cache_read"]:
        cache_note = f"  cache_hit={usage['cache_read']}"
    elif usage["cache_creation"]:
        cache_note = f"  cache_written={usage['cache_creation']}"

    header = (
        f"{SEP}\n"
        f"RICH AI RED TEAM — {sym}  ({company})\n"
        f"Date: {run_date}  |  RICH Score: {score}/100  |  Model: {model}\n"
        f"Tokens: {usage['input']} in / {usage['output']} out{cache_note}\n"
        f"{SEP}\n"
    )
    disclaimer = (
        f"\n{DASH}\n"
        f"Red team output is adversarial by design.\n"
        f"It challenges conviction — it does not override RICH's quantitative verdict.\n"
        f"{DASH}"
    )
    return header + "\n" + analysis.strip() + disclaimer


# ── optional file save ────────────────────────────────────────────────────────

def save_markdown(content: str, pick: dict, run_date: str) -> Path:
    OUT_DIR.mkdir(exist_ok=True)
    path = OUT_DIR / f"{run_date}_{pick['symbol']}_redteam.md"
    path.write_text(content, encoding="utf-8")
    return path


# ── CLI ───────────────────────────────────────────────────────────────────────

def main() -> None:
    p = argparse.ArgumentParser(
        description="AI red team for the RICH daily pick — read-only."
    )
    p.add_argument(
        "--date", default=date.today().isoformat(),
        help="Run date YYYY-MM-DD (default: today)",
    )
    p.add_argument(
        "--ticker", default="",
        help="Override: red-team a specific ticker from today's top-5 instead of the pick",
    )
    p.add_argument(
        "--notes", default="",
        help="Inline qualitative research notes (quoted string)",
    )
    p.add_argument(
        "--notes-file", dest="notes_file", default="",
        help="Path to a plain-text file of research notes",
    )
    p.add_argument(
        "--save", action="store_true",
        help="Save output to redteam/YYYY-MM-DD_TICKER_redteam.md",
    )
    p.add_argument(
        "--model", default=DEFAULT_MODEL,
        help=f"Claude model ID (default: {DEFAULT_MODEL})",
    )
    args = p.parse_args()

    # Resolve notes
    notes = args.notes
    if args.notes_file:
        nf = Path(args.notes_file)
        if not nf.exists():
            sys.exit(f"ERROR: notes file not found: {nf}")
        notes = nf.read_text(encoding="utf-8")

    # Load data
    top5 = load_top5(args.date)

    if args.ticker:
        target = next((r for r in top5 if r["symbol"] == args.ticker.upper()), None)
        if target is None:
            available = ", ".join(r["symbol"] for r in top5)
            sys.exit(
                f"ERROR: {args.ticker} not in top-5 for {args.date}.\n"
                f"       Available: {available}"
            )
    else:
        target = next((r for r in top5 if r.get("is_daily_pick") == 1), top5[0])

    fmp = load_fmp(args.date, target["symbol"])
    if not fmp:
        print(
            f"  [warn] FMP cache not found for {args.date} — "
            "financial ratios will show n/a in the prompt.",
            file=sys.stderr,
        )

    # Build prompt and call API
    user_msg = _build_user_message(target, top5, fmp, notes)
    print(
        f"Red-teaming {target['symbol']} ({args.date}) via {args.model}...",
        file=sys.stderr,
    )
    analysis, usage = call_claude(user_msg, args.model)

    # Output
    output = render(target, analysis, usage, args.model, args.date)
    print(output)

    if args.save:
        path = save_markdown(output, target, args.date)
        print(f"Saved: {path}", file=sys.stderr)


if __name__ == "__main__":
    main()
