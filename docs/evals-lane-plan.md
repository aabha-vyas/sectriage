# Evals lane plan (lane/evals)

Status: agent lane merged. The loop works end to end. First live run on vuln_app scored 5/5, cost $0.0026.
That target is too easy to quote. This lane makes the harness measure things that matter.

## Scope (evals/ and tests/evals/ only; do not change src/contracts.py or src/agent/)
1. Save every run under evals/results/ (summary JSON: model, PROMPT_VERSION, target, tp/fp/fn, precision, recall, cost, steps, latency, stop reason). Diff against a baseline file; exit non-zero on regression. This becomes the merge gate.
2. Summary adds cost per finding, p50/p95 latency, token totals (input, output, cache read, cache write).
3. Repeat runner: N runs per target, report mean and spread. Runs cost cents.
4. Two or three new, harder targets: more files, subtler bugs, decoy safe code. Ground truth for each. Do not tune the prompt against them.
5. Per-class recall stays. Wide-prediction cap (MAX_SPAN_LINES) stays.

## Rules
- Resume numbers come only from real runs on targets the prompt was not tuned on, with model and PROMPT_VERSION stated.
- Live runs use `uv run --env-file .env`. Never export the key. Default cost cap is $0.50 per run.
- Targets are intentionally vulnerable and stay local under evals/targets/.
- Contract changes (for example ToolName.REPORT_FINDING) are separate small commits on main.

## Done when
- pytest, ruff check, ruff format --check, scripts/verify.sh all clean.
- A saved baseline exists, and a deliberate regression makes the eval exit non-zero.
