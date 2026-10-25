# evals

- Ground truth lives in `evals/ground_truth/`; the scorer matches findings by file and line range.
- Report precision, recall, patch pass rate, cost per finding, and p50/p95 latency.
- Use pinned model names so runs are comparable. The agent never sends `temperature` (the API rejects non-default sampling on current models), so comparability comes from the pinned model + `PROMPT_VERSION` (in each run's meta.json) + repeated runs, not from temperature 0.
- Save each run's summary under `evals/results/` so regressions can be diffed.
- Targets are intentionally vulnerable and must stay local.

## Scoring rules

- Matching is one-to-one: each ground-truth bug is claimed by at most one report. A second report of the same bug (for example the entry point and the sink of one taint path) is a false positive.
- Only the five classes (sqli, xss, path_traversal, command_injection, hardcoded_secret) are scored. Anything else (missing auth, CSRF, error handling, resource limits) is out of scope: `VulnClass` has no value for it, so it cannot be reported as a `Finding`, and the scorer never sees it or counts it for or against. Tools whose output is converted for scoring (see the static scanner) drop such results before scoring. A finding in a scored class that matches no ground-truth item is a false positive.
- `fp_free_runs` counts runs with zero false positives. It is not `clean_runs`, which counts runs that stopped cleanly (finish or max_steps).
- A target with no ground-truth bugs (`n_truth == 0`) has no recall; the gate checks mean false positives per run and `fp_free_runs`.

## Frozen targets

- `evals/targets/{ledger_api,notes_portal,static_builder}` are frozen (`evals/targets.lock.json`, checked by `tests/evals/test_targets_frozen.py`). Do not change a file there without telling the user first; after an approved change run `python -m evals.frozen --update` and say so in the commit. Scored runs on a changed target are not comparable with earlier ones.
