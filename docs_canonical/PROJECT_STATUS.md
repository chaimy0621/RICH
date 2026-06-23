# PROJECT_STATUS — RICH Shadow

> Shadow-layer session status. Updated on "bye" (see CONTEXT.md → Session-end behavior).
> This file — NOT `RICH_METHODOLOGY_AND_STATUS.md` — is the session log for the shadow project.
> Last updated: 2026-06-23

## Completed this session (2026-06-23) — Daily + Shadow pipeline failures root-caused & fixed; portfolio_advisor added
- **Triaged both failing tasks.** `RICH_Daily_Pipeline` showed Last Result `0x800710E0` (a prior run force-terminated) and was mid-run; `RICH_Shadow_Pipeline` Last Result `1` (step c `agents\run_signal_notify.cmd` exit 1). Tasks live under `\RICH\`. The runbook's assumed paths (`Desktop\logs`, `Desktop-shadow`) and a root-level `run_signal_notify.cmd` do **not** exist — real layout is `Desktop\RICH` + `Desktop\RICH-shadow`, notifier at `RICH-shadow\agents\`.
- **Root cause #1 (Daily) — transient DNS/network failure, not a code/path/venv bug.** `RICH\logs\2026-06-23.log`: run-1 (20:47) died in `universe.build_universe() → fetcher.company_screener` with `NameResolutionError: Failed to resolve 'financialmodelingprep.com' [Errno 11001] getaddrinfo failed`. A later run self-recovered; the full pipeline **completed clean at 21:39:59 (exit `0x0`)**, `2026-06-23-top5.txt` written. Today was a legitimate **no-pick / SKIP day** (no `is_daily_pick=1` row; top candidates ineligible).
- **Root cause #2 (Shadow) — `signal.md` missing the `confidence` field.** `signal_notify.py` required `confidence` unconditionally, but on a no-pick SKIP day `handoff/signal.md` legitimately omits it → fail-loud exit 1 (downstream of #1: the in-flight Daily run had no pick yet, so the review correctly emitted SKIP but dropped `confidence`).
- **Fix — Shadow validator made SKIP-tolerant.** `agents/signal_notify.py`: `confidence` now defaults to `n/a` when `action == SKIP`; **still required for FULL/HALF** picks (fail-loud preserved). Verified against today's `signal.md` (SKIP+no-confidence → passes; FULL+no-confidence → still flagged).
- **Fix — review agent always emits `confidence`.** `daily_review_prompt.md` step 4 now instructs the agent to always include `confidence` in `signal.md` front-matter (`n/a` on SKIP). Belt-and-suspenders with the validator fix.
- **Fix — RICH-core DNS resilience (logged here for cross-ref; code lives in RICH repo).** `RICH\data\fetcher.py`: connection/DNS errors now get a patient backoff (up to 7 attempts, ~2 min total) to bridge a transient blip, while ordinary request errors keep the fast 3-attempt backoff. Verified by unit-checking the per-error-type stop/wait policy.
- **New — `RICH\portfolio_advisor.py` (read-only advisor).** Capital snapshot (+account reconciliation flag), exit signals (HARD_EXIT/DEGRADED/GONE_30D/OVERWEIGHT; non-US 6742 → MANUAL_REVIEW), and **Taleb-Barbell (Phase 1) / Half-Kelly (Phase 2, gated at 50 scored eval verdicts)** sizing driven by the Shadow verdict. Appends to a new `portfolio_advice` table only; idempotent per `run_date`; prints "no orders" disclaimer. Verified exit 0 on live data (today: Phase 1, Shadow SKIP → no position). Note: the spec's `shadow_eval_log` table / `outcome` column don't exist — it reads `RICH-shadow\eval\signal_vs_pick.csv` (0 scored → Phase 1).
- **`.cmd` guard audit.** Existing wrappers already have absolute `cd /d`, full python paths (mostly), and logging; no working-dir/venv defect caused today's failures. Two optional one-line hardenings proposed (swap `py` launcher → full `python.exe` in `run_discord_daily.cmd` and `agents\run_signal_notify.cmd`) — not applied.
- **PENDING (fail-loud):** the Shadow pipeline has **not yet been re-run** to confirm the `confidence` fix posts exit 0 end-to-end (it invokes the headless review agent + posts to Discord). Daily is verified `0x0`; Shadow re-run/Discord verification is the open follow-up.

### Files changed this session (2026-06-23)
- `PROJECT_STATUS.md` (this update)
- `daily_review_prompt.md` (always emit `confidence`; `n/a` on SKIP)
- *(RICH live repo also changed: `data/fetcher.py` DNS-aware retry, new `portfolio_advisor.py` + `portfolio_advice` table — code lives in the RICH repo, logged here for cross-reference.)*

## Completed this session (2026-06-18) — Shadow pipeline failure fixed (scheduling race → event-driven trigger)
- **Diagnosed `RICH_Shadow_Pipeline` failure (06-18 04:40, Last Result 1).** Root cause = **scheduling race**, not a kill/power/time-limit event. Shadow (Daily 04:40) read RICH **core** output before core finished: core's per-ticker screen took **2403s** that day, so the run completed at **04:42:22**. Step b (`run_shadow_review`) read the still-incomplete core log at 04:40:32, correctly **fail-loud blocked** (no `daily_pick=` yet) and did not refresh `handoff/signal.md` / `eval/signal_vs_pick.csv`. Step c (`signal_notify`) then polled 20 min for today-dated artifacts that never came → clean **exit 1**. 06-18 was itself a legit **SKIP** (all 20 in cooldown). The `0xC000013A` in `signal_notify.log` is a stale 06-11 manual Ctrl+C, unrelated.
- **Fix #1 applied — trigger changed `Daily 04:40` → event-driven.** `RICH_Shadow_Pipeline` now fires on **Event 102** (task completed) for `\RICH\RICH_Daily_Pipeline` in the TaskScheduler/Operational log, with a **2-minute delay** (`<Delay>PT2M</Delay>`). Core runs Python synchronously, so 102 fires at core *completion* (~04:42) — eliminates the race regardless of how long the screen stage runs. Trigger now reads "When an event occurs", Next Run = N/A.
- **Prereq — enabled the TaskScheduler Operational log** (`wevtutil sl "Microsoft-Windows-TaskScheduler/Operational" /e:true`). It was **disabled**, so Event 102 was never recorded; the event trigger would have subscribed to a dead log and never fired. **Closes the 06-11 note** that the Operational log was off.
- **Battery conditions relaxed** — `DisallowStartIfOnBatteries=false`, `StopIfGoingOnBatteries=false` (both unchecked). The trigger now fires at an unpredictable time; on battery the run must still start.
- **Recovered today's SKIP** — manual `run_shadow_pipeline.cmd` completed clean: `signal.md` + eval CSV now dated **2026-06-18** (`NONE/SKIP/risk-on`), verdict **posted to Discord** (exit 0).
- **Backups (revert via `schtasks /create /tn "\RICH\RICH_Shadow_Pipeline" /xml <file> /f`):** `_shadow_task_backup_2026-06-18.xml` (original, pre-change), `_shadow_task_backup_2026-06-18b.xml` (post-battery-relax, current state). Both at `RICH-shadow` root.
- **Watch (first live test):** the 06-19 04:00 core run is the first to exercise the event trigger — confirm Event 102 now logs and Shadow fires ~2 min after core completes.

### Files changed this session (2026-06-18)
- `PROJECT_STATUS.md` (this update)
- Infra only (no code): `RICH_Shadow_Pipeline` task redefined (event trigger + battery conditions); TaskScheduler Operational log enabled. Task XML backups written to `RICH-shadow` root.

## Completed this session (2026-06-11) — Docs migrated to GitHub; notifier no-fire diagnosed
- **Documentation consolidated to GitHub** — `github.com/chaimy0621/RICH-Markdown-Repo`, branch `Bryan-Chai`, local clone `Desktop\RICH-Markdown-Repo` (HTTPS + credential manager; no SSH key on this machine). Canonical docs at repo root (`RICH_METHODOLOGY_AND_STATUS.md`, `PROJECT_STATUS.md`) plus supporting docs, `MIGRATION_REPORT.md`, and three new long-lived strategy docs (`RICH_VISION_AND_MISSION.md`, `RICH_SHADOW_VISION_AND_MISSION.md`, `RICH_OPERATING_PRINCIPLES.md`). Only local deletions: the two `RICH\docs\overlay_*_2026-06-10.md` memos (verified byte-identical in repo first). All runtime files untouched.
- **Session-end workflow extended** — `CONTEXT.md` → "GitHub sync step": on "bye", after writing this file, copy both canonical docs to the repo clone and commit/push (`session-end sync <date>`).
- **`RICHShadowSignalNotify0545` no-fire DIAGNOSED (read-only; nothing changed).** Task is registered (2026-06-11 01:52) and trigger is correct (Mon–Fri 05:45), `DISCORD_WEBHOOK_URL` is set in `HKCU\Environment`, but it never fired at 05:45: the task lacks `WakeToRun`/`StartWhenAvailable` (the 04:40 data agent has both and ran; machine was asleep again by 05:00). The 16:45 manual run's exit `0xC000013A` was a user Ctrl+C, and its wait was the freshness gate working — `handoff/signal.md` is still dated 06-10 because **the 05:00 Daily Review also did not fire today** (it has `WakeToRun` but no `StartWhenAvailable`; last run 06-10, no 06-11 review log or eval row).
- **Fix proposed, NOT applied:** `Set-ScheduledTask -TaskName "RICHShadowSignalNotify0545" -Settings (New-ScheduledTaskSettingsSet -WakeToRun -StartWhenAvailable)` — and the same `-StartWhenAvailable` gap exists on `RICH Shadow Daily Review`. Task Scheduler history (Operational log) is disabled, so skips leave no events.
- **Security note:** the Discord webhook URL was echoed into a session transcript while reading the registry — consider rotating it (same exposure class as the earlier FRED key).

### Files changed this session (2026-06-11)
- `CONTEXT.md` (GitHub sync step added to session-end behavior)
- `PROJECT_STATUS.md` (this update)
- `RICH-Markdown-Repo` (new clone; 4 commits: consolidation, CONTEXT sync, strategy docs, session-end sync)
- `RICH\docs\overlay_gate_2026-06-10.md`, `RICH\docs\overlay_backtest_2026-06-10.md` (deleted locally; live in repo under `RICH/docs/`)

## Completed this session (2026-06-10/11) — Signal Agent Discord notifier added
- **`agents/signal_notify.py` (new).** Standalone Discord notifier for the Signal Agent verdict. stdlib + `requests`; reads `DISCORD_WEBHOOK_URL` from user env. Shadow-only: reads `handoff/signal.md` + `eval/signal_vs_pick.csv`, writes nothing, never touches the live RICH repo or the Signal Agent itself.
  - Waits for both completion artifacts (poll 15s, max 20 min, clean non-zero timeout). **Freshness gate:** front-matter `date` must equal today, else yesterday's persisted `signal.md` would be re-posted every morning; `--any-date` overrides for testing.
  - Builds the plain-text 5-line message (run_date, action, regime, RICH alignment, confidence, biggest risk ≤220 chars). **Regime comes from the eval CSV row** (locked schema) — the front matter only has `regime_aligned`, not the regime name.
  - Fails loud on any missing required field; non-2xx webhook response → status + body printed, exit non-zero. `--dry-run` prints instead of posting.
  - Paths resolve from the script's own location (`handoff/` and `eval/` live at the repo **root**, not under `agents/`), so it works from any cwd.
- **`agents/run_signal_notify.cmd` (new).** Task Scheduler wrapper: `cd /d` into `agents\`, logs (with start/exit-code timestamps) appended to **`..\logs\signal_notify.log`** (shared root logs dir), exit code propagated.
- **Debug prints added** to `signal_notify.py` `main()` — five `flush=True` markers (args / waiting / ready / posting / posted) so the scheduled log shows where a run stalls. Temporary; strip after the first clean scheduled runs.
- **Verified:** `py signal_notify.py --dry-run --any-date` → exit 0, correct 5-line message from the real 2026-06-10 verdict (HALF / risk-on / divergent / medium). The `.cmd` wrapper and the real-post path are **not yet exercised** (no log file yet; no Discord post sent).
- **Task NOT registered yet.** Planned: `RICHShadowSignalNotify0520`, weekdays **05:20 MYT** (after the 04:40 data agent + 05:00 review). `schtasks /Create` + `/Run` verification commands drafted. Prerequisite: `setx DISCORD_WEBHOOK_URL "<url>"` (persistent user env — a session-only `$env:` set is invisible to Task Scheduler).
- **Timing caveat (fail-loud):** at 05:20 the notifier waits at most until 05:40. If the review's `signal.md` lands later than that, every run times out cleanly (exit 1, logged) — by design it never posts a stale verdict; adjust `/ST` or `_MAX_WAIT_SECONDS` if that proves common.

### Files changed this session (2026-06-10/11)
- `agents/signal_notify.py` (new; + debug prints)
- `agents/run_signal_notify.cmd` (new)
- `PROJECT_STATUS.md` (this update)
- *(RICH live repo also changed this session — email removal, Discord daily reporter, overlay gate memos — logged in `RICH\RICH_METHODOLOGY_AND_STATUS.md` Session Log 2026-06-10/11, not here.)*

## Completed this session (2026-06-09) — Open Issue #1 CLOSED
- **Unattended chain confirmed RUN on 2026-06-09.** `RICH_Shadow_DataAgent_0440` (04:40) → daily review (05:00) → one-time `RICH_Shadow_UnattendedCheck_0609` (05:15) all fired unattended. Per `logs\unattended_check_2026-06-09.txt`: **RUN/SKIP = RUN**, **ERRORS = 0**, review **exit code 0**, **data fresh** (`run_date 2026-06-09`, statuses ok). The one-time check task **self-deleted** (`SUCCESS: ... successfully deleted`). **This closes Open Issue #1** (the obligation to confirm the registered unattended task actually fires on 06-09).
- **Caveat (fail-loud):** the verdict line read `REVIEW NEEDED` only because the checker's `FRESHNESS` gate parsed `UNKNOWN` — `check_unattended.cmd` was matching `freshness`+`pass`, which the real log format (`data fresh (run_date ...)`) doesn't contain. Underlying run is clean; checker pattern fixed in `check_unattended.cmd`.
- **Noted, not actioned:** the review log carries a stray warning — `Permission deny rule "MultiEdit(//c/Users/chaim/Desktop/RICH/**)" matches no known tool`. This is the existing `.claude/settings.json` path-sandbox issue (below), not a run error.

## Monday strategy review (2026-06-08) — STRAT-20260602-01 → STRAT-20260608-01
- **Reviewed STRAT-20260602-01 on schedule** (Monday cadence). Outcome: **refresh, not reversal** → issued **`STRAT-20260608-01`** (`supersedes: STRAT-20260602-01`, review_date 2026-06-15). Written to `handoff/portfolio-strategy.md`.
- **Machine-readable rules carried forward UNCHANGED** — regime risk-on, same entry triggers (HY<3.0, VIX<20, pick present), same sizing/risk. The `half_when` print-proximity rule is **self-releasing**, so no mechanical edit was needed.
- **Hypothesis read (on data through 06-04; Jun-5 jobs result NOT yet ingested):**
  - **H1 (narrow breadth):** still binding. Gap compressing but not confirmed — XLK/SPY 21d +22.9/+5.7 (06-02) → +15.1/+4.0 (06-04).
  - **H2 (event caution):** **partially released** — Jun-5 jobs leg passed; **Jun-10 CPI is 2 trading days out → still binds today.** Added explicit release condition (post-06-10 CPI, VIX<20, not-hot → path to FULL opens if breadth confirms).
  - **H3 (sector cap):** not triggered — FANG/GPOR both energy, neither tech; energy thesis intact but unconfirmed pending fresh data.
- **Net sizing call 2026-06-08: HALF** (unchanged — CPI keeps event-caution live; first shot at FULL is post-06-10 CPI).
- **Closed the stale CFNAI note** — STRAT-20260602-01's "CFNAI missing" Data Gap is now marked RESOLVED (macro=11 incl. CFNAI), with the single-month-vs-MA3 caveat retained. (Addresses the Open Issue "stale note in portfolio-strategy.md".)
- **Fail-loud flag:** review is grounded on data through `run_date=2026-06-04`; it re-affirms stance on available data + calendar, and does **not** re-derive regime from the un-ingested jobs print. Real next re-eval = 06-09 data refresh + 06-10 CPI.

## Completed this session (2026-06-08 evening)
- **`STRAT-20260608-01` issued** (`supersedes: STRAT-20260602-01`; `review_date 2026-06-15`) — written to `handoff/portfolio-strategy.md`. Refresh, not reversal; machine rules carried forward unchanged.
- **H1 (narrow breadth): still binding** — gap compressing but unconfirmed (XLK/SPY 21d +22.9/+5.7 06-02 → +15.1/+4.0 06-04).
- **H2 (event caution): partially released** — Jun-5 jobs leg passed; **Jun-10 CPI still pending** (binds today, self-releases after the print).
- **H3 (sector cap): intact** — energy thesis holds (FANG/GPOR both energy, neither tech; not triggered).
- **Net sizing: HALF.** First **FULL** path opens **post-06-10 CPI** (requires VIX<20 + soft print, and breadth confirming).
- **Closed stale "CFNAI missing" issue** — now RESOLVED in the strategy Data Gaps (macro=11 incl. CFNAI; single-month-vs-MA3 caveat retained).
- **Registered & tested `RICH_MoomooSyncSmart_0825`** — state machine verified.
- **Deleted `RICH_Daily_1200_MYT`** — removed due to partial-volume issue.
- **Monday cadence confirmed** — next portfolio strategy review: **2026-06-15**.

## Completed this session (2026-06-08)
- Fixed `data_agent.py` VIX fetch: added `PRICE_FALLBACK_PERIOD="6mo"` + `PRICE_TRIM_DAYS=95` fallback (retry wider when a short period returns empty, then trim back to ~3mo so the table stays uniform). **13/13 tickers now pass validation** (835 rows, `^VIX` included, `yahoo_status=ok`, exit 0). RICH core untouched.
- **Registered & verified `RICH_Shadow_DataAgent_0440`** (`run_data_agent.cmd`, 04:40 MYT Mon–Fri, Python314; fixes the draft's 07:30 time and nonexistent venv/3.13 path). Manual trigger → `LastTaskResult=0`; the task's own log shows 13/13 tickers, `yahoo_status=ok`, "all validation checks passed", exit 0. `schtasks /query`: Status Ready, Next Run 2026-06-09 04:40. **Closes Open Issue #1.**

## Completed this session (2026-06-04)
- Ran `python data_agent.py` → `handoff/market-data.xlsx` refreshed to **`run_date = 2026-06-04`** (13/13 tickers, 11 macro series incl. CFNAI, `prices_end=2026-06-04`, FRED asof 2026-06-03, validation passed; archive copy written).
- **Fixed the stale RICH-pick source.** The review was pinned to `C:\Users\chaim\Desktop\RICH\logs\_run_daily_console2.txt` — a dead manual-run artifact (last 2026-06-01, symbol FANG). Updated `daily_review_prompt.md` (step 2) and `DAILY_CHECKLIST.md` (step 4) to read today's pick from RICH's live daily log `logs\<today>.log` (the **last** `daily_pick=` line is authoritative when a day has multiple runs), with `logs\run_metrics.csv` as the fallback. The allow-list permits `RICH\logs\**` reads but **not** the RICH DB, so the log is the correct read-only source. RICH core untouched.
- **Re-ran the shadow review → it now RUNs end-to-end** (previously SKIP at the stale-data gate):
  - Step 1 freshness: **PASS** (`run_date=2026-06-04`, statuses ok).
  - Step 2 RICH pick: **GPOR** rank 14, `pick_price 168.87` (04:00 run, superseding the 03:00 TMDX run) — read from today's log, not the console file.
  - Step 3 research-report.md: left unchanged (no material macro change; documented why).
  - Step 4 signal.md: recomputed vs STRAT-20260602-01 → action **HALF**, **aligned** (GPOR energy/gas vs idea #3).
  - Step 5 eval: appended the **2026-06-04** row to `eval/signal_vs_pick.csv` (locked schema).

## Current state
- Daily shadow workflow **unblocked and operational** for 2026-06-04; the four outputs are complete and mutually consistent.
- **`RICH Shadow Daily Review` scheduled task IS registered and firing daily** — Last Run 2026-06-04 05:00, **Last Result 0**. (The earlier "files only — task NOT registered / schtasks not run" status is obsolete.)
- Pick source now tracks the live RICH daily log instead of the frozen console artifact.

## Open issues
- **No 2026-06-03 eval row** — pre-existing gap in `eval/signal_vs_pick.csv`; not back-filled (out of scope; not fabricated).
- **FRED key rotation** — still outstanding; key was exposed in transcripts.
- **`.claude/settings.json` path-sandbox** — RICH-core write-block rests on deny rules + prompt; `RICH\logs\**` read confirmed working this session; `python` Bash escape hatch still can't be path-sandboxed by the allow-list.

## Exact next step
2026-06-09 unattended check **confirmed RUN** (above) — Open Issue #1 closed. Next: check **Jun-10 CPI** against the H2 release condition (VIX<20 + soft print → FULL path opens).

## Files changed this session
- `daily_review_prompt.md` (step 2 pick source → RICH live daily log)
- `DAILY_CHECKLIST.md` (step 4 pick source updated to match)
- `handoff/market-data.xlsx` + `archive/market-data_2026-06-04.xlsx` (`data_agent.py` output)
- `handoff/signal.md` (recomputed 2026-06-04; action HALF — written by the review run)
- `eval/signal_vs_pick.csv` (appended 2026-06-04 row — written by the review run)
- `PROJECT_STATUS.md` (this update)
- `data_agent.py` (2026-06-08 — VIX fetch fallback; shadow-layer, RICH core untouched)
