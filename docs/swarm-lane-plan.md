# Swarm lane plan (lane/swarm)

Status: agent lane merged; `run_task(task, model, events, ...)` runs one task and never raises on model or tool failures. This lane runs many tasks durably, with retries, backpressure and replay. No new infrastructure: SQLite only. Branch after the prep commit on main (see docs/architecture.md): it needs `Event.stop_reason` and a public `check_target`.

## Scope (src/swarm/ and tests/swarm/ only; do not change src/contracts.py, src/agent/, or evals/)
1. `TaskQueue` interface (`enqueue`, `claim`, `heartbeat`, `ack`, `nack`, `status`) with a `SqliteQueue` (WAL, `busy_timeout`, `BEGIN IMMEDIATE` claims). Task id is the primary key; enqueueing an existing id is a no-op. States: queued, running, done, dead.
2. Claim takes a lease token (attempt number) and a lease timeout. Expired running tasks are claimable again. `ack` and `heartbeat` check the token. `nack` retries with exponential backoff (no jitter) until `max_attempts`, then `dead`.
3. Results table keyed by `task_id`, `INSERT OR IGNORE`, written in the same transaction as `ack`: the first finisher wins, so a recovered task yields one result even if a zombie worker finishes late.
4. Worker pool: N spawned processes claim a task, run `run_task`, ack. A heartbeat thread extends the lease. Each attempt logs to `<events_dir>/<task_id>.<attempt>.jsonl`; the result row names the winning log. Retry on `model_error`, `internal_error` or a worker crash. `timeout`, `budget`, `refusal`, `max_steps`, `invalid_tool_calls` are final results, not retries.
5. Backpressure: `max_depth` (queued + running) raises `QueueFullError` on enqueue; concurrency is the pool size. Enqueue validates the target with the public `check_target` from `src/agent/run.py`.
6. Replay: `ReplayModelClient` implements `ModelClient` (`src/agent/model.py`). It returns each stored `model_output` in order as a `ModelResponse`, using `Event.stop_reason` (inferred only for old logs without it), and raises `ModelError` when the log runs out. No API key needed. A test shows replay yields identical findings. Limit: replay is exact for host tools on frozen targets; sandboxed tools are not stubbed, so their results may differ.
7. CLI `python -m src.swarm`: `enqueue --target T -n N --batch B` (ids `<target>-<batch>-<i>`, so reruns are no-ops), `work --workers K --lease-s S --model-factory module:callable`, `status`, `replay <task_id>`. No sandbox flag.
8. Tests (no network, no Docker, fake model): queue semantics, lease expiry, backoff, dead-letter, idempotent enqueue, backpressure, replay, and kill-a-worker: SIGKILL a worker mid-task with a short lease, start another, assert one result, `attempts == 2` and a valid events log. `src/swarm/demo.py` scripts the same run with `--lease-s 5`.

## Rules
- Tests and the demo use a fake model; live runs only after approval, `uv run --env-file .env`, $0.50 cap per task.
- Never export the API key. Workers get a model factory by import path, not a pickled object.
- Contract changes are separate small commits on main.
- The SQLite file and events live outside `evals/targets/`.

## Done when
- pytest, ruff check, ruff format --check and scripts/verify.sh are clean.
- `python -m src.swarm.demo` kills a worker mid-run and the task completes once: one result row, two attempts.

## Files to create
`src/swarm/{__init__,queue,sqlite_queue,worker,replay,cli,__main__,demo}.py`, `tests/swarm/{__init__,fakes,test_queue,test_worker,test_replay,test_crash_recovery,test_cli}.py`.

## Decisions and open questions
- Decided: defaults lease 60 s, heartbeat 15 s (lease/4 when `--lease-s` is set), `max_attempts` 3, `max_depth` 100. No replay divergence detection: the identical-findings test is the check.
- Open: none.

## Risks
- `EventLog` appends, so a crashed attempt leaves a partial log; the per-attempt filename keeps them apart.
- SQLite write contention across processes; short transactions and `busy_timeout` mitigate it.
- A stalled heartbeat thread looks like a crash; the token check and result dedupe cover it.
- A killed worker leaks its container until the sandbox's self-destruct fires.
- Replay drift when a target changes on disk, or when sandboxed tool results differ.
- Spawned workers need the fake model importable; the tests set `PYTHONPATH`.
