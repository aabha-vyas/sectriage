---
name: verify
description: Verify SecTriage changes before committing. Runs environment checks, tests, lint, format check, and the eval smoke run.
---

# Verify

Run these in order and stop to fix anything that fails:

1. `scripts/verify.sh --quiet` (environment and static checks)
2. `uv run pytest -q`
3. `uv run ruff check .`
4. `uv run ruff format --check .`
5. `uv run python -m evals.run --summary` (smoke run; compare with the last result in `evals/results/`)

Then check terminal output for errors or warnings. There is no browser UI, so skip UI checks.

Report exactly what passed, what failed, and what was not run (for example, Docker tests when the daemon is down).

If you hit a blocker not covered here, solve it and update this file for next time.
