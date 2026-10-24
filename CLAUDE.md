# SecTriage

Sandboxed multi-agent security triage with an eval harness. Python 3.12, uv, Docker.

## Commands
- Tests: `uv run pytest -q`
- Lint/format: `uv run ruff check .` and `uv run ruff format --check .`
- Eval: `uv run python -m evals.run --summary` (prints one table; never paste raw logs)
- Environment check: `scripts/verify.sh`

## Layout
- `src/agent/` loop and tools, `src/sandbox/` docker runner, `evals/` scorer and seeded targets
- `src/contracts.py` holds event, task, and tool schemas. Do not change it without asking.

## Rules
- Every agent step writes a JSONL event.
- The sandbox has no network.
- Only intentionally vulnerable local targets may be scanned.
- Run the eval before saying a change is done; report the delta only.
- Give file paths instead of pasting large output.
- Details live in `docs/architecture.md`; read it only when needed.
