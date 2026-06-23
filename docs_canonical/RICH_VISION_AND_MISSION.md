# RICH — Vision & Mission

> Long-lived strategy document. Describes what RICH is trying to become and how to decide what to do next. For current implementation status, read `RICH_METHODOLOGY_AND_STATUS.md`. This file should rarely change.

## Mission

Run a fully automated, pure-fundamentals US-equity recommender that outputs **one defensible daily pick, or none**, with every decision — score, flag, and rejection — persisted and traceable after the fact.

RICH produces recommendations only. It never places, modifies, or cancels an order.

## Vision

RICH should become a **self-governing systematic picker whose every signal has earned its place with evidence**. The end state:

- Every scoring input was promoted through the shadow → live framework on measured results, not intuition.
- Pick quality is judged by one number: **realized alpha vs SPY over the hold horizon**, on a sample large enough to mean something.
- The system runs unattended for months; a human looks at it to review gates and outcomes, not to keep it alive.
- A new reader can reconstruct why any historical pick was made from the database alone.

## Core beliefs (the model's identity — change these only deliberately)

1. **Pure fundamentals.** No momentum, no sentiment, no macro inputs in scoring. Audited financial-statement data only.
2. **Earnings quality over earnings level.** Manipulation and cash-conversion gates (Beneish, OCF rules, Piotroski) are identity, not tuning parameters.
3. **Anti-glamour by construction.** High-multiple story stocks should score poorly; that is the design working, not a bug.
4. **An empty pick is a valid pick.** The system is never forced to recommend.
5. **Survival before returns.** Fragility and ruin-risk signals exist because one blow-up erases many small wins.

## Scope

- Universe: liquid US-listed common equities (NASDAQ/NYSE/AMEX), large enough to trade, sectors compatible with the factor model.
- Cadence: one scoring run per trading day; outcomes tracked against SPY.
- Runtime: **local Windows machine via Task Scheduler.** Code, database, logs, and scheduled tasks live in `Desktop\RICH`. Documentation lives in this repo. Cloud deployment is history, not the plan.

## Non-goals

- No brokerage connection, order routing, or execution logic — ever, in this codebase.
- No intraday signals, technical analysis, or news/sentiment ingestion.
- No discretionary overrides: if a rule produces a bad pick, fix the rule through governance, don't hand-edit the output.
- No universe expansion (international, small-cap, other asset classes) before the core model has proven alpha on its home turf.
- No new data vendors or schema rewrites while open questions can be answered with FMP data already collected.

## Improvement priorities (strategic order)

1. **Accumulate clean outcome history.** Almost every open question (overlay, Taleb signals, portfolio engine) is blocked on sample size, not on new code. Protecting the unattended daily run is the highest-leverage work.
2. **Resolve shadow gates with evidence.** Each shadow signal gets a promote / extend / park decision at its gate, recorded in writing. Undecided gates are debt.
3. **Make comparisons fair before making them.** Per-candidate, cooldown-aware, same-horizon outcome tracking (the overlay lesson: divergence counts are not evidence; paired realized alpha is).
4. **Promote the portfolio engine only after its entry/exit rules are validated** against accumulated pick history.
5. **Factor validation pipeline.** Use the full-universe research snapshot to test factors against forward returns before any enters live scoring.
6. Only after the above: new factors, new sleeves, or weight changes.

## Decision criteria

**Promote a shadow signal to live scoring only when all hold:**
- Null rate acceptably low across observed picks.
- Statistically meaningful rank-correlation with forward returns over enough observations.
- Backtest on stored picks shows no regression vs the baseline.
- Its economic hypothesis is distinct from factors already scored.

**Park or remove a signal when** the gate passes with no supporting evidence, or null rates make it unusable. Keep the DB column; remove the code.

**Touch production-critical code (screener, scoring, storage, pipeline) only when:**
- The change is validated with a no-persist pipeline run before deployment.
- The change is surgical — one concern, no adjacent "improvements".

**When two analyses disagree** (e.g., divergence looks interesting but realized alpha is negative), realized outcomes win. When the sample is too thin to decide, the decision is **EXTEND**, written down with what evidence would settle it.

**Default answer to "should we add X?"** is no, unless X shortens the path to a trustworthy alpha measurement.
