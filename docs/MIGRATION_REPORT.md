# Migration Report — 2026-06-11

Consolidation of RICH / RICH-Shadow markdown documentation into this repo.

## Files found (status / methodology / prompts / checklist / handoff / review / progress)

**RICH** (`C:\Users\chaim\Desktop\RICH`):
- `RICH_METHODOLOGY_AND_STATUS.md` — canonical
- `README.md`
- `docs/overlay_gate_2026-06-10.md`
- `docs/overlay_backtest_2026-06-10.md`
- `CLAUDE.md` — excluded (Claude Code harness config, not project documentation)

**RICH Shadow** (`C:\Users\chaim\Desktop\RICH-shadow`):
- `PROJECT_STATUS.md` — canonical
- `CONTEXT.md`
- `MARKET-DATA-SCHEMA.md`
- `DAILY_CHECKLIST.md`
- `daily_review_prompt.md`
- `agents/data.md`, `agents/research.md`, `agents/portfolio.md`, `agents/signal.md`
- `handoff/portfolio-strategy.md`, `handoff/research-report.md`, `handoff/signal.md`
- Excluded: `mcp/mcp-fredapi/README.md` and `.venv` license files (vendored third-party)

## Files merged

No content merge was required. `RICH_METHODOLOGY_AND_STATUS.md` (last updated 2026-06-11) already consolidates all RICH-core status, including summaries of both overlay memos in Session Log 2026-06-10/11. `PROJECT_STATUS.md` (last updated 2026-06-11) already consolidates all shadow-layer status. Both were copied verbatim as the canonical docs; supporting docs were copied alongside unchanged.

## Files deleted (local, after GitHub verification)

- `C:\Users\chaim\Desktop\RICH\docs\overlay_gate_2026-06-10.md` — documentation-only governance memo, duplicated in repo at `RICH/docs/`
- `C:\Users\chaim\Desktop\RICH\docs\overlay_backtest_2026-06-10.md` — same

## Files kept locally (NOT deleted) and why

- `RICH\RICH_METHODOLOGY_AND_STATUS.md` and `RICH-shadow\PROJECT_STATUS.md` — live working copies; the session-end ("bye") workflow writes to them in place. Sync to this repo after updates.
- `RICH\README.md`, `RICH\CLAUDE.md` — code-repo files.
- `RICH-shadow\daily_review_prompt.md`, `DAILY_CHECKLIST.md`, `agents\*.md` — runtime inputs read by the scheduled unattended daily review; deleting them breaks automation.
- `RICH-shadow\handoff\*.md` — regenerated daily by the agents (runtime outputs). Repo copies are 2026-06-10/11 snapshots.
- `RICH-shadow\CONTEXT.md` — governs session-end behavior and the shadow layer's read-only contract.
- `RICH-shadow\MARKET-DATA-SCHEMA.md` — referenced by `data_agent.py` workflow.

## Final GitHub paths

```
RICH_METHODOLOGY_AND_STATUS.md        ← canonical, RICH core
PROJECT_STATUS.md                     ← canonical, RICH Shadow
README.md                             ← repo index
MIGRATION_REPORT.md                   ← this file
RICH/README.md
RICH/docs/overlay_gate_2026-06-10.md
RICH/docs/overlay_backtest_2026-06-10.md
RICH-shadow/CONTEXT.md
RICH-shadow/MARKET-DATA-SCHEMA.md
RICH-shadow/DAILY_CHECKLIST.md
RICH-shadow/daily_review_prompt.md
RICH-shadow/agents/{data,research,portfolio,signal}.md
RICH-shadow/handoff/{portfolio-strategy,research-report,signal}.md
```
