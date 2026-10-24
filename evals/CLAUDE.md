# evals

- Ground truth lives in `evals/ground_truth/`; the scorer matches findings by file and line range.
- Report precision, recall, patch pass rate, cost per finding, and p50/p95 latency.
- Use temperature 0 and pinned model names so runs are comparable.
- Save each run's summary under `evals/results/` so regressions can be diffed.
- Targets are intentionally vulnerable and must stay local.
