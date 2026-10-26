"""The task queue seam: a small Protocol the workers depend on, plus its value types.

States are queued, running, done and dead. `claim` hands out a lease whose token is the attempt
number; `heartbeat`, `ack` and `nack` must present that token, so a worker whose lease was taken
over (a zombie) can never change the task. `ack` stores the result in the same transaction that
marks the task done, so a task has at most one result.

Limit: a heartbeat shows that the worker process is alive, not that it is making progress. A worker
whose heartbeat thread keeps running while its main thread is wedged holds the lease for as long as
it stays alive. Nothing here can detect that; the bounds are `Task.timeout_s` inside `run_task` and
an operator killing the process, after which the lease expires and the task is claimed again.
"""

import re
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from src.contracts import Finding, Task

_TASK_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}")


class QueueFullError(RuntimeError):
    """`enqueue` refused a new task: queued + running has reached `max_depth`."""


class TaskState(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    DONE = "done"
    DEAD = "dead"


def validate_task_id(task_id: str) -> None:
    """Task ids become file names (`<task_id>.<attempt>.jsonl`), so keep them to a safe alphabet."""
    if not _TASK_ID.fullmatch(task_id):
        raise ValueError(
            f"invalid task id {task_id!r}: use 1-128 of A-Z a-z 0-9 . _ - starting with a letter "
            "or digit"
        )


@dataclass(frozen=True)
class Claim:
    """A leased task. `token` is the attempt number (1 on the first claim)."""

    task: Task
    token: int


@dataclass(frozen=True)
class QueueCounts:
    queued: int = 0
    running: int = 0
    done: int = 0
    dead: int = 0

    @property
    def active(self) -> int:
        """Tasks that still need a worker: queued plus running."""
        return self.queued + self.running


@dataclass(frozen=True)
class TaskResult:
    """What a finished attempt produced. `attempt` and `finished_at` are set by the queue."""

    stop_reason: str
    findings: list[Finding]
    steps: int
    cost_usd: float
    log_path: str
    error: str | None = None
    attempt: int = 0
    finished_at: float = 0.0


@dataclass(frozen=True)
class TaskRecord:
    task: Task
    state: TaskState
    attempts: int
    max_attempts: int
    not_before: float
    lease_expires_at: float | None
    worker_pid: int | None
    last_error: str | None


class TaskQueue(Protocol):
    def enqueue(self, task: Task) -> bool:
        """Add `task`; False (a no-op) if its id already exists. Raises `QueueFullError`."""
        ...

    def claim(self, owner_pid: int, lease_s: float) -> Claim | None:
        """Lease the next runnable task, or None. Expired running tasks are runnable again."""
        ...

    def heartbeat(self, task_id: str, token: int, lease_s: float) -> bool:
        """Extend the lease to now + `lease_s`. False means the lease is lost."""
        ...

    def ack(self, task_id: str, token: int, result: TaskResult) -> bool:
        """Mark the task done and store `result`. False: the lease is lost, discard the work."""
        ...

    def nack(self, task_id: str, token: int, error: str, *, retryable: bool = True) -> bool:
        """Give the task back with backoff, or dead once out of attempts or if not retryable."""
        ...

    def status(self) -> QueueCounts: ...

    def get(self, task_id: str) -> TaskRecord | None: ...

    def result(self, task_id: str) -> TaskResult | None: ...
