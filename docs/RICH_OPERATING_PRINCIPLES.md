# RICH & RICH Shadow — Operating Principles

> Long-lived rules of operation shared by both projects. These govern *how* work is done regardless of what is being built. This file should rarely change; if a principle stops being true, change it deliberately and say why.

## 1. Evidence before promotion

Nothing graduates on plausibility. New signals, overlays, and strategies run in shadow first, accumulate observations, and face a written promote / extend / park decision at a defined gate. Divergence and activity are not evidence; **realized, fairly-compared outcomes are**. When the sample is too thin to decide, the decision is EXTEND — recorded, with what evidence would settle it.

## 2. Fail loud

Unattended systems die of silent failures. Every script and scheduled task must exit non-zero on error, log what happened, and never paper over a missing input with a default. A skipped step reported as success is a lie to the future operator. "Completed" means everything ran; anything skipped is named.

## 3. Read-only boundaries are absolute

The shadow layer reads RICH; it never writes it. Broker integrations read accounts; they never place orders. Reporting tools read the database; they never mutate it. A boundary crossed "just once for convenience" no longer exists.

## 4. One source of truth per fact

- **Documentation** lives in this repo (canonical: `RICH_METHODOLOGY_AND_STATUS.md`, `PROJECT_STATUS.md`). No duplicate progress files in the project folders.
- **Runtime** — code, database, logs, scheduler tasks — lives in `Desktop\RICH` and `Desktop\RICH-shadow`.
- **Schema** is defined in one place in code; every column change goes through it.
- When two records disagree, reconcile them immediately; don't let both live.

## 5. Auditability over convenience

Every pick, score, flag, rejection, and verdict is persisted with enough context to reconstruct the decision later. Data is never fabricated or back-filled to make a record look complete — a gap is a gap. If a number can't be traced to its inputs, it doesn't count as evidence.

## 6. Surgical change control

Touch only what the task requires. Production-critical modules change one concern at a time and are validated with a non-persisting test run before deployment. Don't refactor what isn't broken, don't "improve" adjacent code in passing, and don't let a bugfix smuggle in a behavior change.

## 7. Rules decide; humans change rules

No discretionary overrides of system output. If a pick or verdict looks wrong, the response is to examine the rule that produced it and change it through governance — not to hand-edit the result. Conversely, no rule is sacred: any rule can be changed, in writing, with a reason.

## 8. Small-sample humility

Short windows and thin samples overstate everything. State the N, state the horizon, and resist conclusions a few observations cannot support. A great week proves as little as a terrible one. Decisions forced before the data matures default to the reversible option.

## 9. Code answers what code can answer

Deterministic work — fetching, transforming, scheduling, retrying, validating — is done by scripts. Model/agent judgment is reserved for genuine judgment calls: regime reads, gate decisions, strategy synthesis. If a check can be a script, it becomes a script.

## 10. Prefer no action to forced action

RICH may output no pick; the shadow layer may output SKIP; a gate review may output EXTEND. Empty is a first-class answer. Systems forced to act on bad inputs produce confident garbage.

## 11. Local runtime is the deployment reality

Both projects run unsupervised on a local Windows machine via Task Scheduler. Everything must work non-interactively: no prompts, no session-only environment variables, no paths that depend on the current directory. Infrastructure ambitions (cloud, new platforms) wait until they solve a problem the local setup actually has.

## 12. Decision criteria for new work

Before starting anything, it must pass:
1. **Does it shorten the path to trustworthy evidence** (more reliable runs, cleaner outcomes, fairer comparisons)? If not, it waits.
2. **Does it respect every boundary above** (read-only walls, schema discipline, fail-loud)? If not, redesign it.
3. **Can it be done surgically?** If it demands touching many production files at once, break it down or question it.
4. **Will it still matter in three months?** Work that only services this week's curiosity loses to work that compounds.
