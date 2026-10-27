"""Kill a worker mid-task with a short lease: another worker finishes it, once.

These spawn real worker processes and SIGKILL one. They use a scripted fake model, the host
read-only tools and no sandbox: no Docker, no network.
"""

import os
import signal
import sqlite3
import time
from collections.abc import Callable, Iterator
from contextlib import closing
from pathlib import Path

import pytest

from src.agent.events import read_events
from src.agent.loop import REPO_ROOT
from src.swarm.queue import QueueCounts, TaskState
from src.swarm.replay import replay_task
from src.swarm.sqlite_queue import SqliteQueue
from src.swarm.worker import WorkerPool
from tests.swarm.fakes import HANG_MARKER_ENV, expected_finding_ids, make_task

FACTORY = "tests.swarm.fakes:hang_once_factory"
LEASE_S = 2.0
TASK_ID = "crash-1"


@pytest.fixture(autouse=True)
def _spawn_env(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    monkeypatch.setenv(HANG_MARKER_ENV, str(tmp_path / "hung"))


@pytest.fixture
def pools() -> Iterator[list[WorkerPool]]:
    made: list[WorkerPool] = []
    yield made
    for pool in made:  # a failed test must not leave a worker sleeping for an hour
        pool.kill()


@pytest.fixture
def make_pool(tmp_path: Path, pools: list[WorkerPool]) -> Callable[[int], WorkerPool]:
    def make(workers: int) -> WorkerPool:
        pool = WorkerPool(
            tmp_path / "queue.db", tmp_path / "events", FACTORY, workers=workers, lease_s=LEASE_S
        )
        pools.append(pool)
        return pool

    return make


def wait_until(condition: Callable[[], bool], what: str, timeout_s: float = 30.0) -> None:
    deadline = time.monotonic() + timeout_s
    while not condition():
        assert time.monotonic() < deadline, f"timed out waiting for {what}"
        time.sleep(0.05)


def kill_the_worker_holding_the_task(queue: SqliteQueue, tmp_path: Path, pool: WorkerPool) -> int:
    """Wait until the task hangs at step 2 with one event on disk, then SIGKILL its worker."""
    first_log = tmp_path / "events" / f"{TASK_ID}.1.jsonl"
    wait_until(
        lambda: (
            (tmp_path / "hung").exists() and first_log.exists() and first_log.stat().st_size > 0
        ),
        "the worker to reach step 2",
    )
    record = queue.get(TASK_ID)
    assert record is not None and record.state is TaskState.RUNNING
    pid = record.worker_pid
    assert pid is not None and pid in pool.pids
    os.kill(pid, signal.SIGKILL)
    return pid


def assert_completed_once(queue: SqliteQueue, tmp_path: Path) -> None:
    with closing(sqlite3.connect(queue.path)) as db:
        assert db.execute("SELECT COUNT(*) FROM results").fetchone()[0] == 1
    assert queue.status() == QueueCounts(done=1)
    record = queue.get(TASK_ID)
    result = queue.result(TASK_ID)
    assert record is not None and result is not None
    assert record.attempts == 2
    assert (result.attempt, result.stop_reason) == (2, "finish")
    assert {f.finding_id for f in result.findings} == expected_finding_ids()

    # The winning log is the second attempt's, complete and valid.
    assert result.log_path == str(tmp_path / "events" / f"{TASK_ID}.2.jsonl")
    winning = read_events(result.log_path)
    assert {e.task_id for e in winning} == {TASK_ID}
    assert [e.step for e in winning] == sorted(e.step for e in winning)
    assert winning[-1].result_summary == "stop: finish"

    # The killed attempt's partial log stays, apart, and has no terminal event.
    killed = read_events(tmp_path / "events" / f"{TASK_ID}.1.jsonl")
    assert killed and not any(e.result_summary.startswith("stop:") for e in killed)

    # Replay uses the winning .2 log, never the killed attempt's partial .1 log.
    report = replay_task(queue, TASK_ID)
    assert report.identical and report.replayed_stop == "finish"
    assert [e.step for e in read_events(report.log_path)] == [e.step for e in winning]
    assert report.log_path == tmp_path / "events" / "replay" / f"{TASK_ID}.jsonl"


def test_killing_the_only_worker_then_starting_another_completes_the_task_once(
    tmp_path: Path, make_pool: Callable[[int], WorkerPool]
):
    queue = SqliteQueue(tmp_path / "queue.db")
    queue.enqueue(make_task(TASK_ID))

    first = make_pool(1)
    first.start()
    kill_the_worker_holding_the_task(queue, tmp_path, first)
    assert first.join(10) == [-signal.SIGKILL]
    assert queue.result(TASK_ID) is None  # nothing finished it yet

    second = make_pool(1)
    second.start()
    assert second.join(30) == [0]
    assert_completed_once(queue, tmp_path)


def test_a_surviving_worker_picks_up_the_dead_workers_task(
    tmp_path: Path, make_pool: Callable[[int], WorkerPool]
):
    queue = SqliteQueue(tmp_path / "queue.db")
    queue.enqueue(make_task(TASK_ID))

    pool = make_pool(2)
    pool.start()
    kill_the_worker_holding_the_task(queue, tmp_path, pool)
    codes = pool.join(30)  # the survivor waits out the lease, runs the task, sees it drained
    assert sorted(c for c in codes if c is not None) == [-signal.SIGKILL, 0]
    assert_completed_once(queue, tmp_path)
