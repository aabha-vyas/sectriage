# SecTriage

A sandboxed multi-agent security triage platform with an eval harness. Agents read a codebase, find likely vulnerabilities, propose patches, and are scored against known ground truth.

Safety rule: only point this at intentionally vulnerable local targets (the seeded app in `evals/targets/`, later Juice Shop or DVWA). Never scan systems you do not own.

## Goals

1. Show a working agent loop: plan, call tools, observe, update memory, stop within a budget.
2. Run agent work safely in a per-task Docker sandbox.
3. Measure everything: precision, recall, patch pass rate, cost per finding, p50/p95 latency, and regressions across model or prompt versions.
4. Scale out (week 2): a task queue, a worker pool, retries, idempotency, and deterministic replay from the event log.

Resume rule: any number on the resume must come from a real run of this repo.

## Requirements

### Functional

| ID | Requirement |
|----|-------------|
| F1 | Agent core: planner/executor loop in plain Python with a step budget. |
| F2 | Tool registry: `read_file`, `search_code`, `run_command`, `semgrep`, `git`. Each tool has a typed schema in `src/contracts.py`. |
| F3 | Memory: short-term scratch per task; long-term finding store that dedupes. |
| F4 | Event log: every step appends a JSONL event (task id, step, tool, args, result summary, tokens, cost, latency). Written from the first commit. |
| F5 | Sandbox: one Docker container per task, no network (or an egress allowlist), CPU and memory limits, wall-clock timeout. |
| F6 | Evals: seeded vulnerable target, ground-truth file, scorer. `python -m evals.run --summary` prints one table. |
| F7 | Swarm (week 2): queue, workers, concurrency limit, retries, idempotent task ids, backpressure. |
| F8 | Replay (week 2): re-run a task from its event log without calling the model. |
| F9 | Tracing (week 2/3): OpenTelemetry span per step. |
| F10 | Routing (optional): cheap model for triage, stronger model for patching, with cost compared to a single-model baseline. |

### Non-functional

- Python 3.12+, managed by `uv`. Docker required.
- Deterministic where possible: seeded targets, pinned versions, temperature 0 for evals.
- Secrets only from environment variables. `.env` is gitignored.
- Lean agent context: small root `CLAUDE.md`, detail in `docs/`, procedures in skills.
- Each lane merges only when tests pass and the eval shows no regression.

## Stack

Python 3.12, uv, pydantic, Anthropic SDK, Docker, pytest, ruff, semgrep (optional), OpenTelemetry, Redpanda or Redis Streams (week 2).

## Layout

```
src/
  contracts.py     event, task, and tool schemas (change only on main)
  agent/           loop, planner, memory, tool registry
  sandbox/         docker runner
evals/
  targets/         seeded vulnerable apps
  ground_truth/    expected findings
  run.py           scorer and summary table
tests/
scripts/verify.sh  static environment checks
.claude/           settings, subagents, skills
docs/              architecture notes (read only when needed)
```

## Milestones

| Week | Deliverable |
|------|-------------|
| 1 | `contracts.py`, seeded target, scorer, agent loop, sandbox, first tools, JSONL log |
| 2 | Queue and workers, replay, routing, more targets |
| 3 | Full eval metrics, tracing, regression check, write-up with real numbers |

## Definition of done

- `scripts/verify.sh` passes with no FAIL lines.
- `uv run pytest -q`, `uv run ruff check .`, and `uv run ruff format --check .` are clean.
- `uv run python -m evals.run --summary` shows no regression versus the last recorded baseline.
- README results table is filled from a real run.

## Getting started

```bash
cp .env.example .env        # add your ANTHROPIC_API_KEY
uv sync
set -a; . ./.env; set +a
scripts/verify.sh
```

## Parallel work with worktrees

Write `src/contracts.py`, the seeded target, and the scorer on main first. Then:

```bash
git worktree add ../sectriage-agent   -b lane/agent
git worktree add ../sectriage-sandbox -b lane/sandbox
git worktree add ../sectriage-evals   -b lane/evals
```

Run one Claude session per worktree. Merge one lane at a time, rebasing on main first, only after the merge gate passes (tests plus no eval regression). Week 2 adds `lane/swarm` and `lane/obs`.
