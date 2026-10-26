"""Workers: claim a task, run it with `run_task`, then ack or nack it.

`run_attempt` is one claimed task from lease to ack. `worker_loop` repeats it until the queue has
nothing queued or running. `WorkerPool` spawns K `worker_main` processes (the `spawn` start method:
the model factory is given as an import path, `module:callable`, never pickled, and no API key is
exported). Workers never build a sandbox, so `run_task` gets the host read-only tools only.

Each attempt logs to `<events_dir>/<task_id>.<attempt>.jsonl`; a crashed attempt leaves a partial
file and the retry writes a new one. Retries share the per-task cost cap: an attempt gets what the
earlier attempts' logs left of `max_cost_usd`. The model call that was in flight when a worker died
is not in its log, so that spend is not counted.

Limits:
- A heartbeat shows that the process is alive, not that it is making progress. A worker whose
  heartbeat thread keeps running while its main thread is wedged keeps the lease until the process
  is killed. The bounds are `Task.timeout_s` inside `run_task` and an operator killing the worker.
- A worker that lost its lease (stalled heartbeat, then reclaimed) cannot be cancelled: `run_task`
  has no cancel hook. It runs to its stop reason, its `ack` is refused, and the work is discarded.
- `WorkerPool` does not respawn dead workers. The pool reports each death and exits 1. With two or
  more workers the survivors pick up the dead worker's task once its lease expires.
"""

import importlib
import logging
import multiprocessing
import os
import signal
import sqlite3
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from src.agent.events import EventLog
from src.agent.loop import DEFAULT_MAX_COST_USD, StopReason, run_task
from src.agent.model import ModelClient
from src.agent.paths import PathError
from src.contracts import Event
from src.swarm.queue import Claim, TaskResult
from src.swarm.sqlite_queue import DEFAULT_BACKOFF_BASE_S, DEFAULT_BACKOFF_CAP_S, SqliteQueue

log = logging.getLogger(__name__)

POLL_S = 0.2
HEARTBEAT_PER_LEASE = 4  # heartbeat every lease/4 seconds
EXIT_INTERRUPTED = 130

ModelFactory = Callable[[str], ModelClient]

# Everything else (finish, max_steps, timeout, budget, refusal, invalid_tool_calls, model_stopped)
# is a result: retrying would not change it, or would only spend more.
RETRYABLE_STOPS = frozenset(
    {StopReason.MODEL_ERROR, StopReason.INTERNAL_ERROR, StopReason.INTERRUPTED}
)
# `run_task` raises only for a bad target; retrying cannot fix that.
BAD_TARGET_ERRORS = (PathError, FileNotFoundError, NotADirectoryError)


class AttemptOutcome(StrEnum):
    ACKED = "acked"
    NACKED = "nacked"
    LEASE_LOST = "lease_lost"


@dataclass(frozen=True)
class _Failure:
    error: str
    retryable: bool


def load_factory(spec: str) -> ModelFactory:
    """Import `package.module:callable`. The callable takes a model name and returns a client."""
    module_name, sep, attr = spec.partition(":")
    if not (module_name and sep and attr):
        raise ValueError(f"model factory {spec!r} must look like 'package.module:callable'")
    try:
        factory = getattr(importlib.import_module(module_name), attr)
    except (ImportError, AttributeError) as e:
        raise ValueError(f"cannot load model factory {spec!r}: {type(e).__name__}") from None
    if not callable(factory):
        raise ValueError(f"model factory {spec!r} is not callable")
    return factory


# attempt logs and cost ----------------------------------------------------------------------


def attempt_log_path(events_dir: Path, task_id: str, attempt: int) -> Path:
    return events_dir / f"{task_id}.{attempt}.jsonl"


def attempt_cost_usd(path: Path) -> float:
    """Sum of `cost_usd` over one attempt's event log; a missing log costs nothing.

    A killed writer can leave a torn last line, which is skipped. A bad line anywhere else means
    the log is corrupt and raises ValueError.
    """
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        return 0.0
    lines = [line for line in text.split("\n") if line.strip()]
    total = 0.0
    for i, line in enumerate(lines):
        try:
            total += Event.from_jsonl(line).cost_usd
        except ValueError:
            if i < len(lines) - 1:
                raise
            log.warning("skipping a torn last line in %s", path)
    return total


def spent_usd(events_dir: Path, task_id: str, before_attempt: int) -> float:
    return sum(
        attempt_cost_usd(attempt_log_path(events_dir, task_id, n)) for n in range(1, before_attempt)
    )


# one attempt --------------------------------------------------------------------------------


class _Heartbeat(threading.Thread):
    """Extends the lease every `interval_s`. Sets `lost` and stops if the queue refuses."""

    def __init__(
        self, queue: SqliteQueue, task_id: str, token: int, lease_s: float, interval_s: float
    ):
        super().__init__(name=f"heartbeat-{task_id}", daemon=True)
        self.queue = queue
        self.task_id = task_id
        self.token = token
        self.lease_s = lease_s
        self.interval_s = interval_s
        self.lost = False
        self._done = threading.Event()

    def run(self) -> None:
        try:
            while not self._done.wait(self.interval_s):
                try:
                    alive = self.queue.heartbeat(self.task_id, self.token, self.lease_s)
                except sqlite3.Error as e:  # a busy database is not a lost lease: try again
                    log.warning("heartbeat for %s failed: %s", self.task_id, e)
                    continue
                if not alive:
                    self.lost = True
                    log.warning("lost the lease on %s (attempt %d)", self.task_id, self.token)
                    return
        finally:
            self.queue.close()  # this thread's own connection

    def finish(self) -> None:
        self._done.set()
        self.join()


def _execute(
    claim: Claim, path: Path, factory: ModelFactory, max_cost_usd: float
) -> TaskResult | _Failure:
    task = claim.task
    try:
        model = factory(task.model)
    except Exception as e:
        return _Failure(f"model factory failed: {type(e).__name__}", retryable=True)
    path.unlink(missing_ok=True)  # EventLog appends; each attempt starts a fresh log
    try:
        with EventLog(path) as events:
            result = run_task(task, model, events, max_cost_usd=max_cost_usd)
    except BAD_TARGET_ERRORS as e:
        return _Failure(f"bad target: {e}", retryable=False)
    except Exception as e:
        return _Failure(f"worker error: {type(e).__name__}", retryable=True)
    if result.stop_reason in RETRYABLE_STOPS:
        reason = result.stop_reason.value
        return _Failure(f"{reason}: {result.error}" if result.error else reason, retryable=True)
    return TaskResult(
        stop_reason=result.stop_reason.value,
        findings=result.findings,
        steps=result.steps,
        cost_usd=result.cost_usd,
        log_path=str(path),
        error=result.error,
    )


def run_attempt(
    queue: SqliteQueue,
    claim: Claim,
    events_dir: Path,
    factory: ModelFactory,
    *,
    lease_s: float,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    heartbeat_s: float | None = None,
) -> AttemptOutcome:
    """Run one claimed task and settle it with `ack` or `nack`. Never raises for task failures."""
    task, token = claim.task, claim.token
    path = attempt_log_path(events_dir, task.task_id, token)

    decision: TaskResult | _Failure
    try:
        remaining = max_cost_usd - spent_usd(events_dir, task.task_id, token)
    except (ValueError, OSError) as e:
        decision = _Failure(f"cannot read earlier attempt logs: {type(e).__name__}", False)
    else:
        if remaining <= 0:
            decision = _Failure(
                f"the ${max_cost_usd:.2f} cost cap is spent by earlier attempts", False
            )
        else:
            beat = _Heartbeat(
                queue, task.task_id, token, lease_s, heartbeat_s or lease_s / HEARTBEAT_PER_LEASE
            )
            beat.start()
            try:
                decision = _execute(claim, path, factory, remaining)
            finally:
                beat.finish()

    if isinstance(decision, TaskResult):
        if queue.ack(task.task_id, token, decision):
            return AttemptOutcome.ACKED
    elif queue.nack(task.task_id, token, decision.error, retryable=decision.retryable):
        return AttemptOutcome.NACKED
    log.warning("discarding attempt %d of %s: its lease was taken over", token, task.task_id)
    return AttemptOutcome.LEASE_LOST


def worker_loop(
    queue: SqliteQueue,
    events_dir: Path,
    factory: ModelFactory,
    *,
    lease_s: float,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    poll_s: float = POLL_S,
    should_stop: Callable[[], bool] = lambda: False,
) -> int:
    """Claim and run tasks until none is queued or running. Returns how many attempts ran."""
    attempts = 0
    while not should_stop():
        claim = queue.claim(os.getpid(), lease_s)
        if claim is None:
            if queue.status().active == 0:
                break
            time.sleep(poll_s)  # a running task may be a dead worker's: wait for its lease
            continue
        run_attempt(queue, claim, events_dir, factory, lease_s=lease_s, max_cost_usd=max_cost_usd)
        attempts += 1
    return attempts


# processes ----------------------------------------------------------------------------------


def worker_main(
    db_path: str,
    events_dir: str,
    factory_spec: str,
    lease_s: float,
    max_cost_usd: float,
    poll_s: float,
    queue_kwargs: dict[str, Any],
) -> None:
    """Entry point of a spawned worker process: picklable arguments only."""
    stop = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stop.set())  # finish the current attempt, then exit
    factory = load_factory(factory_spec)
    queue = SqliteQueue(db_path, target_check=None, **queue_kwargs)
    try:
        worker_loop(
            queue,
            Path(events_dir),
            factory,
            lease_s=lease_s,
            max_cost_usd=max_cost_usd,
            poll_s=poll_s,
            should_stop=stop.is_set,
        )
    except KeyboardInterrupt:  # the lease expires and the task is claimed again
        pass
    finally:
        queue.close()


class WorkerPool:
    def __init__(
        self,
        db_path: str | Path,
        events_dir: str | Path,
        factory_spec: str,
        *,
        workers: int,
        lease_s: float,
        max_cost_usd: float = DEFAULT_MAX_COST_USD,
        poll_s: float = POLL_S,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
    ):
        if workers < 1 or lease_s <= 0 or max_cost_usd <= 0:
            raise ValueError("workers, lease_s and max_cost_usd must be positive")
        self._args = (
            str(db_path),
            str(events_dir),
            factory_spec,
            lease_s,
            max_cost_usd,
            poll_s,
            {"backoff_base_s": backoff_base_s, "backoff_cap_s": backoff_cap_s},
        )
        self.workers = workers
        self.processes: list[multiprocessing.process.BaseProcess] = []

    @property
    def pids(self) -> list[int]:
        return [p.pid for p in self.processes if p.pid is not None]

    def start(self) -> list[int]:
        load_factory(self._args[2])  # fail here, not in K children
        SqliteQueue(
            self._args[0]
        ).close()  # the file exists and is in WAL mode before children open it
        ctx = multiprocessing.get_context("spawn")
        self.processes = [
            ctx.Process(target=worker_main, args=self._args, daemon=True)
            for _ in range(self.workers)
        ]
        for p in self.processes:
            p.start()
        return self.pids

    def join(self, timeout: float | None = None) -> list[int | None]:
        """Exit codes in start order (negative: killed by that signal; None: still running)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        for p in self.processes:
            p.join(None if deadline is None else max(0.0, deadline - time.monotonic()))
        return [p.exitcode for p in self.processes]

    def kill(self) -> None:
        for p in self.processes:
            if p.is_alive():
                p.kill()
        self.join(5)

    def run(self) -> int:
        """Start, wait until every worker exits, report deaths. 0 if all exited cleanly."""
        self.start()
        try:
            codes = self.join()
        except KeyboardInterrupt:
            for p in self.processes:
                if p.is_alive():
                    p.terminate()  # SIGTERM: finish the attempt in hand, then exit
            self.join(30)
            self.kill()
            return EXIT_INTERRUPTED
        bad = False
        for p, code in zip(self.processes, codes, strict=True):
            if code == 0:
                continue
            bad = True
            how = f"killed by signal {-code}" if code is not None and code < 0 else f"exit {code}"
            log.error(
                "worker %s died (%s); its task is claimed again once its lease expires", p.pid, how
            )
        return 1 if bad else 0
