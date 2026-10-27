import sqlite3
import threading
import time
from contextlib import closing
from pathlib import Path

import pytest

from src.agent.run import TARGETS_DIR
from src.swarm.queue import QueueCounts, QueueFullError, TaskState
from src.swarm.sqlite_queue import SCHEMA_VERSION, SqliteQueue
from tests.swarm.fakes import TARGET, FakeClock, make_result, make_task

LEASE_S = 10.0


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock(1000.0)


@pytest.fixture
def make_queue(tmp_path: Path, clock: FakeClock):
    def make(**kwargs: object) -> SqliteQueue:
        return SqliteQueue(tmp_path / "queue.db", clock=clock, **kwargs)  # type: ignore[arg-type]

    return make


@pytest.fixture
def queue(make_queue) -> SqliteQueue:
    return make_queue()


def result_rows(queue: SqliteQueue) -> int:
    with closing(sqlite3.connect(queue.path)) as db:
        return db.execute("SELECT COUNT(*) FROM results").fetchone()[0]


# enqueue ------------------------------------------------------------------------------------


def test_enqueue_is_idempotent(queue: SqliteQueue):
    assert queue.enqueue(make_task("a")) is True
    assert queue.enqueue(make_task("a", goal="something else")) is False
    assert queue.status() == QueueCounts(queued=1)
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    assert claim.task.goal == "review the target"  # the first enqueue won


def test_enqueue_stores_the_resolved_target(queue: SqliteQueue, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.chdir(TARGET.parent)
    queue.enqueue(make_task("a", target_path=TARGET.name))
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    assert claim.task.target_path == str(TARGET.resolve())


@pytest.mark.parametrize("bad", ["", "a/b", "../x", ".hidden", "a b", "x" * 129, "café"])
def test_enqueue_rejects_unsafe_ids(queue: SqliteQueue, bad: str):
    with pytest.raises(ValueError, match="invalid task id"):
        queue.enqueue(make_task(bad))
    assert queue.status() == QueueCounts()


@pytest.mark.parametrize(
    "target",
    [TARGETS_DIR, TARGETS_DIR.parent, TARGETS_DIR / "does_not_exist", Path("/")],
    ids=["targets-root", "outside", "missing", "root"],
)
def test_enqueue_rejects_targets_outside_the_allowed_tree(queue: SqliteQueue, target: Path):
    with pytest.raises(ValueError):
        queue.enqueue(make_task("a", target_path=str(target)))
    assert queue.status() == QueueCounts()


# backpressure -------------------------------------------------------------------------------


def test_queue_full_counts_queued_and_running_only(make_queue):
    queue = make_queue(max_depth=2)
    queue.enqueue(make_task("a"))
    queue.enqueue(make_task("b"))
    with pytest.raises(QueueFullError):
        queue.enqueue(make_task("c"))

    claim = queue.claim(1, LEASE_S)  # running still counts
    assert claim is not None
    with pytest.raises(QueueFullError):
        queue.enqueue(make_task("c"))

    assert queue.ack(claim.task.task_id, claim.token, make_result())  # done frees a slot
    assert queue.enqueue(make_task("c")) is True

    second = queue.claim(1, LEASE_S)
    assert second is not None
    assert queue.nack(second.task.task_id, second.token, "boom", retryable=False)  # dead frees one
    assert queue.enqueue(make_task("d")) is True
    assert queue.status() == QueueCounts(queued=2, done=1, dead=1)


def test_reenqueue_on_a_full_queue_is_a_no_op(make_queue):
    queue = make_queue(max_depth=1)
    assert queue.enqueue(make_task("a")) is True
    assert queue.enqueue(make_task("a")) is False  # a rerun of a full batch must not raise


# claim --------------------------------------------------------------------------------------


def test_claim_is_fifo_and_empty_returns_none(queue: SqliteQueue, clock: FakeClock):
    assert queue.claim(1, LEASE_S) is None
    for i, task_id in enumerate(["a", "b", "c"]):
        clock.now = 1000.0 + i
        queue.enqueue(make_task(task_id))
    order = [queue.claim(1, LEASE_S) for _ in range(3)]
    assert [c.task.task_id for c in order if c] == ["a", "b", "c"]
    assert queue.claim(1, LEASE_S) is None


def test_claim_breaks_ties_in_insertion_order(queue: SqliteQueue):
    for task_id in ["z", "y", "x"]:  # same timestamp, ids not sorted
        queue.enqueue(make_task(task_id))
    claims = [queue.claim(1, LEASE_S) for _ in range(3)]
    assert [c.task.task_id for c in claims if c] == ["z", "y", "x"]


def test_claim_sets_token_lease_and_pid(queue: SqliteQueue, clock: FakeClock):
    queue.enqueue(make_task("a"))
    claim = queue.claim(4242, LEASE_S)
    assert claim is not None and claim.token == 1
    record = queue.get("a")
    assert record is not None
    assert record.state is TaskState.RUNNING
    assert record.attempts == 1
    assert record.worker_pid == 4242
    assert record.lease_expires_at == clock.now + LEASE_S
    assert queue.status() == QueueCounts(running=1)


def test_claim_rejects_a_non_positive_lease(queue: SqliteQueue):
    with pytest.raises(ValueError, match="lease_s"):
        queue.claim(1, 0)


# lease --------------------------------------------------------------------------------------


def test_running_task_is_not_claimable_until_its_lease_expires(
    queue: SqliteQueue, clock: FakeClock
):
    queue.enqueue(make_task("a"))
    first = queue.claim(1, LEASE_S)
    assert first is not None
    clock.advance(LEASE_S - 0.1)
    assert queue.claim(2, LEASE_S) is None
    clock.advance(0.1)  # expiry is inclusive
    second = queue.claim(2, LEASE_S)
    assert second is not None
    assert (second.task.task_id, second.token) == ("a", 2)
    record = queue.get("a")
    assert record is not None
    assert (record.attempts, record.worker_pid, record.last_error) == (2, 2, "lease expired")


def test_heartbeat_extends_the_lease(queue: SqliteQueue, clock: FakeClock):
    queue.enqueue(make_task("a"))
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    clock.advance(8)
    assert queue.heartbeat("a", claim.token, LEASE_S) is True  # now expires at +18
    clock.advance(9)  # +17: past the original lease, inside the extended one
    assert queue.claim(2, LEASE_S) is None
    clock.advance(1)
    assert queue.claim(2, LEASE_S) is not None


def test_stale_token_cannot_heartbeat_ack_or_nack(queue: SqliteQueue, clock: FakeClock):
    queue.enqueue(make_task("a"))
    zombie = queue.claim(1, LEASE_S)
    assert zombie is not None
    clock.advance(LEASE_S)
    current = queue.claim(2, LEASE_S)
    assert current is not None and current.token == 2

    assert queue.heartbeat("a", zombie.token, LEASE_S) is False
    assert queue.nack("a", zombie.token, "late") is False
    assert queue.ack("a", zombie.token, make_result(log_path="events/a.1.jsonl")) is False
    assert result_rows(queue) == 0
    record = queue.get("a")
    assert record is not None and record.state is TaskState.RUNNING  # untouched by the zombie

    assert queue.ack("a", current.token, make_result(log_path="events/a.2.jsonl")) is True


def test_heartbeat_on_unknown_or_finished_task_is_false(queue: SqliteQueue):
    assert queue.heartbeat("nope", 1, LEASE_S) is False
    queue.enqueue(make_task("a"))
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    queue.ack("a", claim.token, make_result())
    assert queue.heartbeat("a", claim.token, LEASE_S) is False


# ack ----------------------------------------------------------------------------------------


def test_ack_stores_one_result_and_marks_done(queue: SqliteQueue, clock: FakeClock):
    queue.enqueue(make_task("a"))
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    clock.advance(3)
    assert queue.ack("a", claim.token, make_result(error="note")) is True

    record = queue.get("a")
    assert record is not None
    assert (record.state, record.worker_pid, record.lease_expires_at) == (
        TaskState.DONE,
        None,
        None,
    )
    stored = queue.result("a")
    assert stored is not None
    assert stored.attempt == 1
    assert stored.finished_at == clock.now
    assert stored.findings == make_result().findings
    assert (stored.stop_reason, stored.steps, stored.cost_usd) == ("finish", 4, 0.01)
    assert (stored.log_path, stored.error) == ("events/t-1.1.jsonl", "note")
    assert queue.status() == QueueCounts(done=1)


def test_second_ack_is_rejected_and_the_first_result_stays(queue: SqliteQueue):
    queue.enqueue(make_task("a"))
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    assert queue.ack("a", claim.token, make_result(stop_reason="finish")) is True
    assert queue.ack("a", claim.token, make_result(stop_reason="budget")) is False
    assert result_rows(queue) == 1
    stored = queue.result("a")
    assert stored is not None and stored.stop_reason == "finish"


def test_result_is_none_until_acked(queue: SqliteQueue):
    queue.enqueue(make_task("a"))
    assert queue.result("a") is None
    assert queue.get("missing") is None
    assert queue.result("missing") is None


# nack, backoff, dead letter -----------------------------------------------------------------


def test_nack_backs_off_exponentially_then_goes_dead(queue: SqliteQueue, clock: FakeClock):
    queue.enqueue(make_task("a"))

    for attempt, delay in [(1, 1.0), (2, 2.0)]:
        claim = queue.claim(1, LEASE_S)
        assert claim is not None and claim.token == attempt
        assert queue.nack("a", claim.token, f"error {attempt}") is True
        record = queue.get("a")
        assert record is not None
        assert (record.state, record.not_before) == (TaskState.QUEUED, clock.now + delay)
        assert record.last_error == f"error {attempt}"
        clock.advance(delay - 0.1)
        assert queue.claim(1, LEASE_S) is None  # still backing off
        clock.advance(0.1)

    last = queue.claim(1, LEASE_S)
    assert last is not None and last.token == 3
    assert queue.nack("a", last.token, "error 3") is True
    record = queue.get("a")
    assert record is not None
    assert (record.state, record.attempts, record.last_error) == (TaskState.DEAD, 3, "error 3")
    clock.advance(1000)
    assert queue.claim(1, LEASE_S) is None
    assert queue.status() == QueueCounts(dead=1)
    assert result_rows(queue) == 0


def test_backoff_is_capped(make_queue, clock: FakeClock):
    queue = make_queue(max_attempts=10, backoff_base_s=10.0, backoff_cap_s=25.0)
    queue.enqueue(make_task("a"))
    delays = []
    for _ in range(4):
        claim = queue.claim(1, LEASE_S)
        assert claim is not None
        queue.nack("a", claim.token, "x")
        record = queue.get("a")
        assert record is not None
        delays.append(record.not_before - clock.now)
        clock.advance(1000)
    assert delays == [10.0, 20.0, 25.0, 25.0]


def test_non_retryable_nack_goes_dead_at_once(queue: SqliteQueue):
    queue.enqueue(make_task("a"))
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    assert queue.nack("a", claim.token, "bad target", retryable=False) is True
    record = queue.get("a")
    assert record is not None
    assert (record.state, record.attempts, record.last_error) == (
        TaskState.DEAD,
        1,
        "bad target",
    )


def test_expired_lease_on_the_final_attempt_goes_dead(make_queue, clock: FakeClock):
    queue = make_queue(max_attempts=2)
    queue.enqueue(make_task("a"))
    for _ in range(2):  # two workers claim and vanish
        assert queue.claim(1, LEASE_S) is not None
        clock.advance(LEASE_S)
    assert queue.claim(1, LEASE_S) is None  # no third attempt
    record = queue.get("a")
    assert record is not None
    assert (record.state, record.attempts) == (TaskState.DEAD, 2)
    assert record.last_error == "lease expired on the final attempt"


# persistence and concurrency ----------------------------------------------------------------


def test_state_survives_reopening_the_file(tmp_path: Path, clock: FakeClock):
    path = tmp_path / "queue.db"
    first = SqliteQueue(path, clock=clock)
    first.enqueue(make_task("a"))
    claim = first.claim(1, LEASE_S)
    assert claim is not None
    first.close()

    second = SqliteQueue(path, clock=clock)
    record = second.get("a")
    assert record is not None and record.state is TaskState.RUNNING
    assert second.ack("a", claim.token, make_result()) is True


def test_a_newer_schema_version_is_refused(tmp_path: Path):
    path = tmp_path / "queue.db"
    SqliteQueue(path).close()
    with sqlite3.connect(path) as db:
        db.execute(f"PRAGMA user_version = {SCHEMA_VERSION + 1}")
    with pytest.raises(RuntimeError, match="schema version"):
        SqliteQueue(path)


def test_threads_claim_each_task_exactly_once(tmp_path: Path):
    path = tmp_path / "queue.db"
    n_tasks, n_threads = 30, 6
    barrier = threading.Barrier(n_threads)
    claimed: list[list[str]] = [[] for _ in range(n_threads)]
    errors: list[BaseException] = []

    def worker(i: int) -> None:
        try:
            q = SqliteQueue(path)  # every thread opens the file at the same moment
            barrier.wait()
            if i == 0:
                for n in range(n_tasks):
                    q.enqueue(make_task(f"t{n:02d}"))
            while True:
                claim = q.claim(i, 60.0)
                if claim is None:
                    if q.status().queued == 0 and q.status().running + q.status().done >= n_tasks:
                        return
                    time.sleep(0.005)
                    continue
                claimed[i].append(claim.task.task_id)
                assert q.ack(claim.task.task_id, claim.token, make_result())
        except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)
            barrier.abort()

    threads = [threading.Thread(target=worker, args=(i,), daemon=True) for i in range(n_threads)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    flat = [task_id for ids in claimed for task_id in ids]
    assert sorted(flat) == [f"t{n:02d}" for n in range(n_tasks)]  # none missed, none twice
    final = SqliteQueue(path)
    assert final.status() == QueueCounts(done=n_tasks)
    assert result_rows(final) == n_tasks


def test_many_threads_opening_a_fresh_file_together_all_get_wal(tmp_path: Path):
    """Regression: converting a new file to WAL can fail "database is locked" without waiting."""
    n_threads, n_files = 8, 20
    errors: list[BaseException] = []

    def opener(path: Path, barrier: threading.Barrier) -> None:
        try:
            barrier.wait()
            SqliteQueue(path).close()
        except BaseException as e:  # noqa: BLE001 - surfaced by the assertion below
            errors.append(e)

    for n in range(n_files):
        path = tmp_path / f"fresh{n}.db"
        barrier = threading.Barrier(n_threads)
        threads = [
            threading.Thread(target=opener, args=(path, barrier), daemon=True)
            for _ in range(n_threads)
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=30)
        with closing(sqlite3.connect(path)) as db:
            assert db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    assert not errors
