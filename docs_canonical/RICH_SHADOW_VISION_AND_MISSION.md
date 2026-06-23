# RICH Shadow — Vision & Mission

> Long-lived strategy document. Describes what the shadow layer is trying to become and how to decide what to do next. For current session state, read `PROJECT_STATUS.md`. This file should rarely change.

## Mission

Run a **read-only judgment layer beside RICH** that answers the one question RICH deliberately ignores: *given the current market regime, how much of today's pick should actually be acted on?* Output exactly one auditable sizing verdict per trading day — FULL / HALF / SKIP — and log it against RICH's pick so the verdicts can later be scored.

## Vision

The shadow layer should become a **proven regime-and-sizing overlay**: a system whose track record (the eval log of verdicts vs realized pick outcomes) demonstrates whether macro/regime context adds value on top of RICH's pure fundamentals.

- If the record shows the verdicts help, they graduate into how positions are actually sized — by the operator, never by writing into RICH.
- If the record shows they don't, the layer is honest evidence that the overlay should be dropped. Either answer is a success; an unscored opinion stream is the only failure.

## The one inviolable constraint

**The shadow layer never modifies RICH core.** It does not write to RICH's database, code, config, or outputs. It reads RICH's published artifacts (logs, picks) and sits beside them. Every agent, script, and scheduled task in this project must preserve this boundary. Anything that would violate it is out of scope by definition, regardless of how useful it looks.

## How it works (stable architecture)

A pipeline of single-responsibility agents connected by file handoffs:

- **Data agent** → market & macro snapshot (prices, vol, rates, breadth, macro series).
- **Research agent** → regime read (risk-on / risk-off, breadth, confidence) from that data.
- **Portfolio agent** → a machine-readable strategy: hypothesis, entry/exit triggers, sizing map, risk limits, review date. Refreshed on a weekly cadence; superseded, never silently edited.
- **Signal agent** → the daily verdict: evaluates today's data against the live strategy, compares with RICH's pick, emits FULL / HALF / SKIP plus an eval row.
- **Notifier** → delivers the verdict; posts nothing stale.

Each handoff file has an owner and a contract. Agents are decisive (exactly one action, stated confidence, single biggest risk) and fail loud: stale or missing inputs produce SKIP-with-reason, never a quietly degraded verdict.

## Scope

- One verdict per trading day, grounded in data no older than the current run.
- A strategy document with explicit, testable rules — hypotheses that say what must be true and what releases them.
- A locked-schema eval log: one row per trading day, never fabricated or back-filled.
- Runtime: local Windows Task Scheduler, fully unattended. Code and tasks live in `Desktop\RICH-shadow`.

## Non-goals

- No trading, order placement, or brokerage write access.
- No second stock picker. The shadow layer sizes RICH's pick; it does not propose alternatives to it.
- No modification of RICH's pick, scores, or ranking — not even "just this once".
- No backfilling missed eval rows. A gap is recorded as a gap.
- No strategy churn: stances are refreshed or superseded on the review cadence with reasons, not flipped intraday on noise.

## Improvement priorities (strategic order)

1. **Unattended reliability.** The chain (data → review → verdict → notify) must fire daily without help. Every silent failure mode found gets a loud-failure fix.
2. **Accumulate the eval record.** The whole project's value rests on an unbroken verdict-vs-outcome log. Protect it above features.
3. **Score the verdicts.** Build the comparison of FULL/HALF/SKIP calls against realized pick performance over the hold horizon. Until this exists, the layer has opinions, not evidence.
4. **Tighten the strategy loop.** Hypotheses should be specific enough that incoming data can release or refute them mechanically.
5. Only after the above: richer data sources, additional agents, or finer-grained sizing.

## Decision criteria

- **When inputs are stale, missing, or ambiguous → SKIP, with the reason logged.** A wrong confident verdict is worse than a skipped day.
- **When the shadow view and RICH diverge**, record the divergence and the reasoning; divergence is data, not a problem to suppress.
- **Change a strategy stance only at review** or on a defined trigger firing — and by superseding the document (new version, what changed, why), not editing in place.
- **Add a new component only if** it strengthens the daily verdict or its evaluation; anything else is scope creep.
- **Graduation test:** before any verdict influences real sizing, the eval record must show the verdicts would have improved risk-adjusted results versus always-FULL. The burden of proof is on the shadow layer.
