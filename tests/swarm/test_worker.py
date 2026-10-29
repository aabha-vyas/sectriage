import os
import time
from collections.abc import Callable, Iterable
from pathlib import Path

import pytest

from src.agent.events import read_events
from src.agent.model import ModelError, ModelResponse
from src.contracts import Event
from src.swarm.queue import QueueCounts, TaskState
from src.swarm.sqlite_queue import SqliteQueue
from src.swarm.worker import (
    AttemptOutcome,
    WorkerPool,
    attempt_cost_usd,
    load_factory,
    run_attempt,
    spent_usd,
    worker_loop,
)
from tests.agent.fakes import refusal, say
from tests.swarm.fakes import (
    FakeModel,
    clean_factory,
    expected_finding_ids,
    make_result,
    make_task,
    tool_call,
    vuln_app_script,
)

LEASE_S = 30.0
READ = {"path": "app.py"}

Script = Iterable[ModelResponse | BaseException]


@pytest.fixture
def db(tmp_path: Path) -> Path:
    return tmp_path / "queue.db"


@pytest.fixture
def events_dir(tmp_path: Path) -> Path:
    return tmp_path / "events"


@pytest.fixture
def queue(db: Path) -> SqliteQueue:
    return SqliteQueue(db, backoff_base_s=0.0)  # retries are claimable at once; real clock


def scripted(*scripts: Script) -> Callable[[str], FakeModel]:
    """A factory whose n-th call returns a model playing the n-th script."""
    remaining = list(scripts)
    calls: list[str] = []

    def factory(model_name: str) -> FakeModel:
        calls.append(model_name)
        return FakeModel(remaining.pop(0))

    factory.calls = calls  # type: ignore[attr-defined]
    return factory


def run_one(
    queue: SqliteQueue, events_dir: Path, factory: Callable, **kwargs: object
) -> AttemptOutcome:
    claim = queue.claim(os.getpid(), LEASE_S)
    assert claim is not None
    return run_attempt(queue, claim, events_dir, factory, lease_s=LEASE_S, **kwargs)  # type: ignore[arg-type]


# one attempt --------------------------------------------------------------------------------


def test_success_acks_and_names_the_attempt_log(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("ok"))
    assert run_one(queue, events_dir, clean_factory) is AttemptOutcome.ACKED

    record = queue.get("ok")
    result = queue.result("ok")
    assert record is not None and result is not None
    assert (record.state, record.attempts) == (TaskState.DONE, 1)
    assert (result.stop_reason, result.attempt) == ("finish", 1)
    assert result.log_path == str(events_dir / "ok.1.jsonl")
    assert {f.finding_id for f in result.findings} == expected_finding_ids()

    events = read_events(result.log_path)
    assert {e.task_id for e in events} == {"ok"}
    assert events[-1].result_summary == "stop: finish"
    assert result.steps == len(events) - 1  # every step plus the terminal event


@pytest.mark.parametrize(
    ("stop", "script", "fields"),
    [
        ("finish", vuln_app_script(), {}),
        ("max_steps", [tool_call("read_file", READ)], {"max_steps": 1}),
        ("budget", [tool_call("read_file", READ, cost=0.6)], {}),
        ("refusal", [refusal()], {}),
        ("invalid_tool_calls", [tool_call("read_file", {}) for _ in range(3)], {}),
        ("model_stopped", [say(), say()], {}),
    ],
)
def test_final_stop_reasons_are_results_not_retries(
    queue: SqliteQueue, events_dir: Path, stop: str, script: Script, fields: dict
):
    queue.enqueue(make_task("t", **fields))
    assert run_one(queue, events_dir, scripted(script)) is AttemptOutcome.ACKED
    record, result = queue.get("t"), queue.result("t")
    assert record is not None and result is not None
    assert (record.state, record.attempts, result.stop_reason) == (TaskState.DONE, 1, stop)


def test_timeout_is_a_result_not_a_retry(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t", timeout_s=1))
    model_ran_long = FakeModel([tool_call("read_file", READ)], on_call=lambda: time.sleep(1.1))
    assert run_one(queue, events_dir, lambda _: model_ran_long) is AttemptOutcome.ACKED
    result = queue.result("t")
    assert result is not None and result.stop_reason == "timeout"


@pytest.mark.parametrize(
    ("stop", "failure"),
    [
        ("model_error", ModelError("boom")),
        ("internal_error", RuntimeError("bug")),
        ("interrupted", KeyboardInterrupt()),
    ],
)
def test_retryable_stops_are_nacked_with_backoff(
    queue: SqliteQueue, events_dir: Path, stop: str, failure: BaseException
):
    queue.enqueue(make_task("t"))
    assert run_one(queue, events_dir, scripted([failure])) is AttemptOutcome.NACKED
    record = queue.get("t")
    assert record is not None
    assert (record.state, record.attempts) == (TaskState.QUEUED, 1)
    assert record.last_error is not None and record.last_error.startswith(stop)
    assert queue.result("t") is None
    assert (events_dir / "t.1.jsonl").exists()


def test_a_retry_gets_its_own_log_and_the_result_names_it(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t"))
    factory = scripted([ModelError("boom")], vuln_app_script())
    assert run_one(queue, events_dir, factory) is AttemptOutcome.NACKED
    assert run_one(queue, events_dir, factory) is AttemptOutcome.ACKED

    record, result = queue.get("t"), queue.result("t")
    assert record is not None and result is not None
    assert (record.state, record.attempts, result.attempt) == (TaskState.DONE, 2, 2)
    assert result.log_path == str(events_dir / "t.2.jsonl")
    first = read_events(events_dir / "t.1.jsonl")
    assert [e.result_summary for e in first] == ["stop: model_error"]  # kept apart, not appended to


def test_three_model_errors_go_dead(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t"))
    factory = scripted(*([ModelError("boom")] for _ in range(3)))
    outcomes = [run_one(queue, events_dir, factory) for _ in range(3)]
    assert outcomes == [AttemptOutcome.NACKED] * 3
    record = queue.get("t")
    assert record is not None
    assert (record.state, record.attempts) == (TaskState.DEAD, 3)
    assert record.last_error == "model_error: boom"
    assert queue.result("t") is None
    assert queue.claim(os.getpid(), LEASE_S) is None
    assert sorted(p.name for p in events_dir.iterdir()) == [f"t.{n}.jsonl" for n in (1, 2, 3)]


@pytest.mark.parametrize("kind", ["missing", "a file"])
def test_bad_target_goes_dead_without_a_retry(
    db: Path, tmp_path: Path, events_dir: Path, kind: str
):
    queue = SqliteQueue(db, backoff_base_s=0.0, target_check=None)
    target = tmp_path / "gone"
    if kind == "a file":
        target.write_text("not a directory")
    queue.enqueue(make_task("t", target_path=str(target)))
    assert run_one(queue, events_dir, clean_factory) is AttemptOutcome.NACKED
    record = queue.get("t")
    assert record is not None
    assert (record.state, record.attempts) == (TaskState.DEAD, 1)
    assert record.last_error is not None and record.last_error.startswith("bad target")


def test_a_failing_factory_is_retried(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t"))

    def no_key(model_name: str) -> FakeModel:
        raise RuntimeError("no api key")

    assert run_one(queue, events_dir, no_key) is AttemptOutcome.NACKED
    record = queue.get("t")
    assert record is not None
    assert (record.state, record.last_error) == (
        TaskState.QUEUED,
        "model factory failed: RuntimeError",
    )


# lease --------------------------------------------------------------------------------------


def test_heartbeat_keeps_a_slow_task_from_being_reclaimed(
    db: Path, queue: SqliteQueue, events_dir: Path
):
    rival = SqliteQueue(db, target_check=None)
    seen: list[object] = []
    slept = False

    def slow_first_call() -> None:
        nonlocal slept
        if not slept:
            slept = True
            time.sleep(1.0)  # 2.5 leases long
            seen.append(rival.claim(2, 5.0))

    queue.enqueue(make_task("t"))
    claim = queue.claim(1, 0.4)
    assert claim is not None
    model = FakeModel(vuln_app_script(), on_call=slow_first_call)
    outcome = run_attempt(queue, claim, events_dir, lambda _: model, lease_s=0.4, heartbeat_s=0.1)
    assert seen == [None]  # still leased: the heartbeat kept extending it
    assert outcome is AttemptOutcome.ACKED
    record = queue.get("t")
    assert record is not None and record.attempts == 1


def test_a_zombie_attempt_is_discarded_and_the_taker_wins(
    db: Path, queue: SqliteQueue, events_dir: Path
):
    taker = SqliteQueue(db, target_check=None, clock=lambda: time.time() + 1000)  # lease long gone
    stolen = []
    first = True

    def lease_taken_over() -> None:
        nonlocal first
        if first:
            first = False
            stolen.append(taker.claim(2, 30.0))
            time.sleep(0.3)  # the heartbeat notices the loss

    queue.enqueue(make_task("t"))
    claim = queue.claim(1, LEASE_S)
    assert claim is not None
    model = FakeModel(vuln_app_script(), on_call=lease_taken_over)
    outcome = run_attempt(
        queue, claim, events_dir, lambda _: model, lease_s=LEASE_S, heartbeat_s=0.05
    )

    assert outcome is AttemptOutcome.LEASE_LOST
    assert stolen[0] is not None and stolen[0].token == 2
    assert queue.result("t") is None  # the zombie's finished work was thrown away
    assert (events_dir / "t.1.jsonl").exists()
    assert taker.ack("t", 2, make_result())
    result = queue.result("t")
    assert result is not None and result.attempt == 2


# cost cap shared across attempts ------------------------------------------------------------


def test_attempts_share_the_per_task_cost_cap(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t"))
    factory = scripted(
        [tool_call("read_file", READ, cost=0.30), ModelError("boom")],
        [tool_call("read_file", READ, cost=0.25)],  # over the $0.20 left of the $0.50 cap
    )
    assert run_one(queue, events_dir, factory) is AttemptOutcome.NACKED
    assert spent_usd(events_dir, "t", 2) == pytest.approx(0.30)
    assert run_one(queue, events_dir, factory) is AttemptOutcome.ACKED
    result = queue.result("t")
    assert result is not None
    assert (result.stop_reason, result.steps, result.attempt) == ("budget", 1, 2)


def test_a_spent_cap_ends_the_task_without_another_attempt(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t"))
    factory = scripted([tool_call("read_file", READ, cost=0.5), ModelError("boom")])
    assert run_one(queue, events_dir, factory) is AttemptOutcome.NACKED  # $0.50 is not over $0.50
    assert run_one(queue, events_dir, factory) is AttemptOutcome.NACKED  # nothing left
    record = queue.get("t")
    assert record is not None
    assert (record.state, record.attempts) == (TaskState.DEAD, 2)
    assert record.last_error is not None and "cost cap" in record.last_error
    assert len(factory.calls) == 1  # type: ignore[attr-defined]  # no model was built for attempt 2


def event_line(cost: float, step: int = 1) -> str:
    event = Event.model_validate(
        {"task_id": "t", "step": step, "ts": "2026-01-01T00:00:00Z", "cost_usd": cost}
    )
    return event.to_jsonl()


def test_attempt_cost_sums_the_events(tmp_path: Path):
    log = tmp_path / "t.1.jsonl"
    log.write_text(event_line(0.1) + "\n" + event_line(0.25, 2) + "\n")
    assert attempt_cost_usd(log) == pytest.approx(0.35)
    assert attempt_cost_usd(tmp_path / "t.2.jsonl") == 0.0  # no log: nothing spent


@pytest.mark.parametrize("tear", [10, 40, -1])
def test_attempt_cost_skips_a_torn_last_line(tmp_path: Path, tear: int):
    log = tmp_path / "t.1.jsonl"
    torn = event_line(0.9, 3)
    log.write_text(event_line(0.1) + "\n" + event_line(0.25, 2) + "\n" + torn[:tear])
    assert attempt_cost_usd(log) == pytest.approx(0.35)


def test_attempt_cost_skips_a_torn_multibyte_character(tmp_path: Path):
    log = tmp_path / "t.1.jsonl"
    torn = event_line(0.9, 2).replace('"result_summary":""', '"result_summary":"é"')
    cut = torn.index("é") + 1  # between the two bytes of é
    log.write_bytes((event_line(0.1) + "\n" + torn).encode()[: len(event_line(0.1)) + 1 + cut])
    assert attempt_cost_usd(log) == pytest.approx(0.1)


def test_attempt_cost_rejects_corruption_before_the_last_line(tmp_path: Path):
    log = tmp_path / "t.1.jsonl"
    log.write_text(event_line(0.1) + "\n{not json\n" + event_line(0.2, 2) + "\n")
    with pytest.raises(ValueError):
        attempt_cost_usd(log)


# worker loop --------------------------------------------------------------------------------


def test_worker_loop_runs_until_the_queue_is_drained(queue: SqliteQueue, events_dir: Path):
    for n in range(3):
        queue.enqueue(make_task(f"t{n}"))
    ran = worker_loop(queue, events_dir, clean_factory, lease_s=LEASE_S, poll_s=0.01)
    assert ran == 3
    assert queue.status() == QueueCounts(done=3)


def test_worker_loop_waits_out_a_dead_workers_lease(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t"))
    assert queue.claim(99999, 0.5) is not None  # a worker that claimed it and vanished
    started = time.monotonic()
    ran = worker_loop(queue, events_dir, clean_factory, lease_s=LEASE_S, poll_s=0.05)
    assert ran == 1
    assert time.monotonic() - started >= 0.4
    record = queue.get("t")
    assert record is not None and (record.state, record.attempts) == (TaskState.DONE, 2)


def test_worker_loop_stops_when_asked(queue: SqliteQueue, events_dir: Path):
    queue.enqueue(make_task("t"))
    ran = worker_loop(queue, events_dir, clean_factory, lease_s=LEASE_S, should_stop=lambda: True)
    assert ran == 0
    assert queue.status() == QueueCounts(queued=1)


# factory loading and the pool ---------------------------------------------------------------


def test_load_factory_imports_by_path():
    assert load_factory("tests.swarm.fakes:clean_factory") is clean_factory


@pytest.mark.parametrize(
    "spec",
    [
        "nocolon",
        "pkg:",
        ":name",
        "no.such.module:x",
        "tests.swarm.fakes:missing",
        "tests.swarm.fakes:TARGET",
    ],
)
def test_load_factory_rejects_bad_specs(spec: str):
    with pytest.raises(ValueError, match="model factory"):
        load_factory(spec)


def test_pool_rejects_bad_arguments_and_bad_factories_before_spawning(db: Path, events_dir: Path):
    with pytest.raises(ValueError, match="positive"):
        WorkerPool(db, events_dir, "tests.swarm.fakes:clean_factory", workers=0, lease_s=5)
    pool = WorkerPool(db, events_dir, "no.such.module:x", workers=2, lease_s=5)
    with pytest.raises(ValueError, match="cannot load"):
        pool.start()
    assert pool.processes == []


def test_pool_of_three_drains_the_queue(
    monkeypatch: pytest.MonkeyPatch, db: Path, events_dir: Path
):
    from src.agent.loop import REPO_ROOT

    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))
    queue = SqliteQueue(db)
    for n in range(6):
        queue.enqueue(make_task(f"t{n}"))
    pool = WorkerPool(db, events_dir, "tests.swarm.fakes:clean_factory", workers=3, lease_s=10)
    try:
        assert pool.run() == 0
    finally:
        pool.kill()
    assert queue.status() == QueueCounts(done=6)
    for n in range(6):
        result = queue.result(f"t{n}")
        assert result is not None and result.stop_reason == "finish"
        assert {f.finding_id for f in result.findings} == expected_finding_ids()


def test_attempt_cost_skips_a_run_header(tmp_path: Path):
    from src.agent.events import header_line
    from tests.evals.builders import make_provenance

    log = tmp_path / "t.1.jsonl"
    log.write_text(header_line(make_provenance()) + "\n" + event_line(0.1) + "\n")
    assert attempt_cost_usd(log) == pytest.approx(0.1)
