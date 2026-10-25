# evals

- Ground truth lives in `evals/ground_truth/`; the scorer matches findings by file and line range.
- Report precision, recall, patch pass rate, cost per finding, and p50/p95 latency.
- Use pinned model names so runs are comparable. The agent never sends `temperature` (the API rejects non-default sampling on current models), so comparability comes from the pinned model + `PROMPT_VERSION` (in each run's meta.json) + repeated runs, not from temperature 0.
- Save each run's summary under `evals/results/` so regressions can be diffed.
- Targets are intentionally vulnerable and must stay local.
