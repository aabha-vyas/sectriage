"""`SqliteQueue`: the task queue on one SQLite file shared by many processes.

Every write is a short `BEGIN IMMEDIATE` transaction with no model call or file I/O inside, so
writers queue up on `busy_timeout` instead of deadlocking on a read-then-write upgrade. Each thread
gets its own connection (the heartbeat thread must not share the worker's). Do not carry an open
queue across `fork`; open a new one in the child.

Times come from an injected `clock` (default `time.time`: processes must agree, so not monotonic).
"""

import json
import sqlite3
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from pathlib import Path

from src.agent.run import check_target
from src.contracts import Finding, Task
from src.swarm.queue import (
    Claim,
    QueueCounts,
    QueueFullError,
    TaskRecord,
    TaskResult,
    TaskState,
    validate_task_id,
)

SCHEMA_VERSION = 1
DEFAULT_MAX_DEPTH = 100
DEFAULT_MAX_ATTEMPTS = 3
DEFAULT_BACKOFF_BASE_S = 1.0
DEFAULT_BACKOFF_CAP_S = 60.0
BUSY_TIMEOUT_MS = 5000

_SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
  task_id          TEXT PRIMARY KEY,
  payload          TEXT NOT NULL,
  state            TEXT NOT NULL CHECK (state IN ('queued','running','done','dead')),
  attempts         INTEGER NOT NULL DEFAULT 0,
  max_attempts     INTEGER NOT NULL,
  lease_token      INTEGER,
  lease_expires_at REAL,
  worker_pid       INTEGER,
  not_before       REAL NOT NULL DEFAULT 0,
  last_error       TEXT,
  enqueued_at      REAL NOT NULL,
  updated_at       REAL NOT NULL,
  CHECK ((state = 'running') = (lease_token IS NOT NULL))
);
CREATE INDEX IF NOT EXISTS tasks_claim ON tasks (state, not_before);
CREATE INDEX IF NOT EXISTS tasks_lease ON tasks (state, lease_expires_at);
CREATE TABLE IF NOT EXISTS results (
  task_id     TEXT PRIMARY KEY REFERENCES tasks (task_id),
  attempt     INTEGER NOT NULL,
  stop_reason TEXT NOT NULL,
  findings    TEXT NOT NULL,
  steps       INTEGER NOT NULL,
  cost_usd    REAL NOT NULL,
  log_path    TEXT NOT NULL,
  error       TEXT,
  finished_at REAL NOT NULL
);
"""


class SqliteQueue:
    def __init__(
        self,
        path: str | Path,
        *,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_attempts: int = DEFAULT_MAX_ATTEMPTS,
        backoff_base_s: float = DEFAULT_BACKOFF_BASE_S,
        backoff_cap_s: float = DEFAULT_BACKOFF_CAP_S,
        clock: Callable[[], float] = time.time,
        target_check: Callable[[Path], Path] | None = check_target,
    ):
        """`target_check` validates `task.target_path` on enqueue; None skips it (tests only)."""
        if max_depth < 1 or max_attempts < 1:
            raise ValueError("max_depth and max_attempts must be at least 1")
        self.path = Path(path)
        self.max_depth = max_depth
        self.max_attempts = max_attempts
        self.backoff_base_s = backoff_base_s
        self.backoff_cap_s = backoff_cap_s
        self._clock = clock
        self._target_check = target_check
        self._local = threading.local()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_schema()

    # connections --------------------------------------------------------------------------

    def _conn(self) -> sqlite3.Connection:
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(self.path, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute(f"PRAGMA busy_timeout = {BUSY_TIMEOUT_MS}")
            conn.execute("PRAGMA journal_mode = WAL")
            conn.execute("PRAGMA synchronous = FULL")
            self._local.conn = conn
        return conn

    def close(self) -> None:
        """Close the calling thread's connection."""
        conn: sqlite3.Connection | None = getattr(self._local, "conn", None)
        if conn is not None:
            conn.close()
            self._local.conn = None

    @contextmanager
    def _tx(self) -> Iterator[sqlite3.Connection]:
        """One `BEGIN IMMEDIATE` transaction; an exception rolls it back."""
        conn = self._conn()
        conn.execute("BEGIN IMMEDIATE")
        try:
            yield conn
        except BaseException:
            if conn.in_transaction:
                conn.execute("ROLLBACK")
            raise
        conn.execute("COMMIT")

    def _init_schema(self) -> None:
        with self._tx() as c:  # IMMEDIATE serializes processes opening a fresh file together
            version = c.execute("PRAGMA user_version").fetchone()[0]
            if version > SCHEMA_VERSION:
                raise RuntimeError(
                    f"{self.path} has schema version {version}; this code supports {SCHEMA_VERSION}"
                )
            if version < SCHEMA_VERSION:
                for statement in _SCHEMA.split(";"):
                    if statement.strip():
                        c.execute(statement)
                c.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    # queue --------------------------------------------------------------------------------

    def enqueue(self, task: Task) -> bool:
        validate_task_id(task.task_id)
        if self._target_check is not None:
            resolved = self._target_check(Path(task.target_path))
            task = task.model_copy(update={"target_path": str(resolved)})
        now = self._clock()
        with self._tx() as c:
            if c.execute("SELECT 1 FROM tasks WHERE task_id = ?", (task.task_id,)).fetchone():
                return False  # checked before the depth limit: a rerun of a full batch is a no-op
            depth = c.execute(
                "SELECT COUNT(*) FROM tasks WHERE state IN ('queued', 'running')"
            ).fetchone()[0]
            if depth >= self.max_depth:
                raise QueueFullError(f"queue is full: {depth} tasks queued or running")
            c.execute(
                "INSERT INTO tasks (task_id, payload, state, max_attempts, enqueued_at, updated_at)"
                " VALUES (?, ?, 'queued', ?, ?, ?)",
                (task.task_id, task.model_dump_json(), self.max_attempts, now, now),
            )
        return True

    def claim(self, owner_pid: int, lease_s: float) -> Claim | None:
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        now = self._clock()
        with self._tx() as c:
            # A task that keeps expiring on its last attempt is dead, not run a fourth time.
            c.execute(
                "UPDATE tasks SET state = 'dead', lease_token = NULL, lease_expires_at = NULL,"
                " worker_pid = NULL, last_error = 'lease expired on the final attempt',"
                " updated_at = ? WHERE state = 'running' AND lease_expires_at <= ?"
                " AND attempts >= max_attempts",
                (now, now),
            )
            rows = c.execute(
                "UPDATE tasks SET state = 'running', attempts = attempts + 1,"
                " lease_token = attempts + 1, lease_expires_at = ?, worker_pid = ?,"
                " last_error = CASE WHEN state = 'running' THEN 'lease expired'"
                " ELSE last_error END, updated_at = ?"
                " WHERE task_id = (SELECT task_id FROM tasks"
                "   WHERE (state = 'queued' AND not_before <= ?)"
                "      OR (state = 'running' AND lease_expires_at <= ?)"
                "   ORDER BY not_before, enqueued_at, rowid LIMIT 1)"
                " RETURNING payload, lease_token",
                (now + lease_s, owner_pid, now, now, now),
            ).fetchall()
        if not rows:
            return None
        return Claim(Task.model_validate_json(rows[0]["payload"]), rows[0]["lease_token"])

    def heartbeat(self, task_id: str, token: int, lease_s: float) -> bool:
        if lease_s <= 0:
            raise ValueError("lease_s must be positive")
        now = self._clock()
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tasks SET lease_expires_at = ?, updated_at = ?"
                " WHERE task_id = ? AND state = 'running' AND lease_token = ?",
                (now + lease_s, now, task_id, token),
            )
            return cur.rowcount == 1

    def ack(self, task_id: str, token: int, result: TaskResult) -> bool:
        now = self._clock()
        findings = json.dumps([f.model_dump(mode="json") for f in result.findings])
        with self._tx() as c:
            cur = c.execute(
                "UPDATE tasks SET state = 'done', lease_token = NULL, lease_expires_at = NULL,"
                " worker_pid = NULL, updated_at = ?"
                " WHERE task_id = ? AND state = 'running' AND lease_token = ?",
                (now, task_id, token),
            )
            if cur.rowcount == 0:
                return False
            c.execute(
                "INSERT OR IGNORE INTO results (task_id, attempt, stop_reason, findings, steps,"
                " cost_usd, log_path, error, finished_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    task_id,
                    token,
                    result.stop_reason,
                    findings,
                    result.steps,
                    result.cost_usd,
                    result.log_path,
                    result.error,
                    now,
                ),
            )
        return True

    def nack(self, task_id: str, token: int, error: str, *, retryable: bool = True) -> bool:
        now = self._clock()
        with self._tx() as c:
            row = c.execute(
                "SELECT attempts, max_attempts FROM tasks"
                " WHERE task_id = ? AND state = 'running' AND lease_token = ?",
                (task_id, token),
            ).fetchone()
            if row is None:
                return False
            if not retryable or row["attempts"] >= row["max_attempts"]:
                state, not_before = TaskState.DEAD, 0.0
            else:
                delay = min(self.backoff_cap_s, self.backoff_base_s * 2 ** (row["attempts"] - 1))
                state, not_before = TaskState.QUEUED, now + delay
            c.execute(
                "UPDATE tasks SET state = ?, not_before = ?, lease_token = NULL,"
                " lease_expires_at = NULL, worker_pid = NULL, last_error = ?, updated_at = ?"
                " WHERE task_id = ?",
                (state.value, not_before, error, now, task_id),
            )
        return True

    # reads --------------------------------------------------------------------------------

    def status(self) -> QueueCounts:
        rows = self._conn().execute("SELECT state, COUNT(*) AS n FROM tasks GROUP BY state")
        counts = {row["state"]: row["n"] for row in rows}
        return QueueCounts(**{s.value: counts.get(s.value, 0) for s in TaskState})

    def get(self, task_id: str) -> TaskRecord | None:
        row = self._conn().execute("SELECT * FROM tasks WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskRecord(
            task=Task.model_validate_json(row["payload"]),
            state=TaskState(row["state"]),
            attempts=row["attempts"],
            max_attempts=row["max_attempts"],
            not_before=row["not_before"],
            lease_expires_at=row["lease_expires_at"],
            worker_pid=row["worker_pid"],
            last_error=row["last_error"],
        )

    def result(self, task_id: str) -> TaskResult | None:
        row = self._conn().execute("SELECT * FROM results WHERE task_id = ?", (task_id,)).fetchone()
        if row is None:
            return None
        return TaskResult(
            stop_reason=row["stop_reason"],
            findings=[Finding.model_validate(f) for f in json.loads(row["findings"])],
            steps=row["steps"],
            cost_usd=row["cost_usd"],
            log_path=row["log_path"],
            error=row["error"],
            attempt=row["attempt"],
            finished_at=row["finished_at"],
        )
