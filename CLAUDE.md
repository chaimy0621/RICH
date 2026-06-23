# CLAUDE.md — RICH System & 12-Rule Template

These rules apply to every task in this project unless explicitly overridden.
Bias: caution over speed on non-trivial work. Use judgment on trivial tasks.

## Rule 1-4: The Karpathy Baseline
1. **Think Before Coding**: State assumptions explicitly. Push back when a simpler approach exists. Stop when confused.
2. **Simplicity First**: Minimum code that solves the problem. No speculative features.
3. **Surgical Changes**: Touch only what you must. Don't "improve" adjacent code or refactor what isn't broken.
4. **Goal-Driven Execution**: Define success criteria. Loop until verified.

## Rule 5-8: System & Context Constraints
5. **Use the model only for judgment calls**: Do NOT use me for routing, retries, or deterministic transforms. If code can answer, code answers.
6. **Token budgets are not advisory**: Per-task: 4,000 tokens. Per-session: 30,000 tokens. Summarize and start fresh if approaching budget. Surface the breach.
7. **Surface conflicts, don't average them**: If two patterns contradict, pick one. Don't blend conflicting patterns.
8. **Read before you write**: Before adding code, read exports, immediate callers, shared utilities. "Looks orthogonal" is dangerous.

## Rule 9-12: Execution & Validation
9. **Tests verify intent, not just behavior**: Tests must encode WHY behavior matters.
10. **Checkpoint after every significant step**: Summarize what was done, what's verified, what's left. Stop and restate if you lose track.
11. **Match conventions**: Conformance > taste. If you genuinely think a convention is harmful, surface it. Don't fork silently.
12. **Fail loud**: "Completed" or "Tests pass" is wrong if anything was skipped silently. Default to surfacing uncertainty.

---

## RICH Project Specific Rules
- **Source of Truth**: Always respect existing SQLite schema and `RICH_Project_Summary_Update.docx` logic.
- **Architecture Limits**: 
  - FMP API fetching lives in `fetcher.py`.
  - Factor logic lives in `factors.py`. 
  - Scoring (Quality/Growth/ValueRisk, Valuation overlay) lives in `scoring.py`.
  - Filtering/Cooldown logic lives in `cooldown.py`.
- **Database Rules**: Do not alter dual-ranking fields or valuation overlay schema without explicit instruction.
- **Automation**: Code must run unsupervised via Windows Task Scheduler (`rundaily.py`). Fail loud and log properly for batch execution.
