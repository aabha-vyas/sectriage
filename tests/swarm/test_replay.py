import os
from pathlib import Path

import pytest

from src.agent.events import read_events
from src.agent.model import ModelError
from src.contracts import Event
from src.swarm.queue import TaskResult
from src.swarm.replay import (
    REPLAY_DIR,
    ReplayError,
    ReplayModelClient,
    infer_stop_reason,
    replay_task,
)
from src.swarm.sqlite_queue import SqliteQueue
from src.swarm.worker import AttemptOutcome, run_attempt
from tests.agent.fakes import refusal
from tests.swarm.fakes import (
    FakeModel,
    clean_factory,
    expected_finding_ids,
    make_finding,
    make_task,
    tool_call,
)

LEASE_S = 30.0
COMPLETE = {"system": "", "tools": [], "messages": [], "timeout_s": 1.0}


@pytest.fixture
def events_dir(tmp_path: Path) -> Path:
    return tmp_path / "events"


@pytest.fixture
def queue(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(tmp_path / "queue.db")


def record(queue: SqliteQueue, events_dir: Path, factory, task_id: str = "r-1") -> Path:
    """Run one task through the worker path and return its winning log."""
    queue.enqueue(make_task(task_id))
    claim = queue.claim(os.getpid(), LEASE_S)
    assert claim is not None
    assert run_attempt(queue, claim, events_dir, factory, lease_s=LEASE_S) is AttemptOutcome.ACKED
    result = queue.result(task_id)
    assert result is not None
    return Path(result.log_path)


def rewrite(log: Path, keep: int | None = None, **changes: object) -> None:
    """Rewrite a recorded log in place: keep the first `keep` events, apply `changes` to each."""
    events = read_events(log)[:keep]
    log.write_text("".join(e.model_copy(update=changes).to_jsonl() + "\n" for e in events))


# the client ---------------------------------------------------------------------------------


def test_client_serves_the_recorded_turns_in_order(queue: SqliteQueue, events_dir: Path):
    log = record(queue, events_dir, clean_factory)
    events = read_events(log)
    turns = [e for e in events if e.model_output is not None]
    client = ReplayModelClient(events)

    assert client.model == events[0].model
    for event in turns:
        resp = client.complete(**COMPLETE)
        assert resp.stop_reason == event.stop_reason
        assert resp.usage.input_tokens == event.tokens_in
        assert resp.usage.output_tokens == event.tokens_out
        assert (resp.cost_usd, resp.latency_ms) == (event.cost_usd, event.latency_ms)
    assert client.remaining == 0


def test_client_past_the_end_raises_model_error(queue: SqliteQueue, events_dir: Path):
    client = ReplayModelClient(read_events(record(queue, events_dir, clean_factory)))
    while client.remaining:
        client.complete(**COMPLETE)
    with pytest.raises(ModelError, match="replay log exhausted"):
        client.complete(**COMPLETE)


def test_client_rejects_an_empty_log():
    with pytest.raises(ReplayError, match="empty"):
        ReplayModelClient([])


# a clean run --------------------------------------------------------------------------------


def test_a_clean_run_replays_with_identical_findings(queue: SqliteQueue, events_dir: Path):
    log = record(queue, events_dir, clean_factory)
    original = log.read_bytes()

    report = replay_task(queue, "r-1")

    assert report.identical and report.exit_code == 0
    assert (report.recorded_stop, report.replayed_stop) == ("finish", "finish")
    assert {f.finding_id for f in report.replayed} == expected_finding_ids()
    assert report.render().splitlines()[0] == "r-1: identical (5 recorded, 5 replayed)"
    assert log.read_bytes() == original  # the recorded log is never touched
    assert report.log_path == events_dir / REPLAY_DIR / "r-1.jsonl"
    # The replay wrote its own complete log: same steps, same ending.
    replayed, recorded = read_events(report.log_path), read_events(log)
    assert [e.step for e in replayed] == [e.step for e in recorded]
    assert replayed[-1].result_summary == "stop: finish"


def test_replaying_twice_overwrites_the_replay_log(queue: SqliteQueue, events_dir: Path):
    record(queue, events_dir, clean_factory)
    first = replay_task(queue, "r-1")
    size = first.log_path.stat().st_size
    second = replay_task(queue, "r-1")
    assert second.identical and second.log_path.stat().st_size == size


def test_replay_dir_can_be_chosen(queue: SqliteQueue, events_dir: Path, tmp_path: Path):
    record(queue, events_dir, clean_factory)
    report = replay_task(queue, "r-1", replay_dir=tmp_path / "elsewhere")
    assert report.log_path == tmp_path / "elsewhere" / "r-1.jsonl" and report.log_path.exists()


def test_a_replay_with_different_findings_reports_the_diff(queue: SqliteQueue, events_dir: Path):
    log = record(queue, events_dir, clean_factory)
    queue.enqueue(make_task("r-2"))
    claim = queue.claim(os.getpid(), LEASE_S)
    assert claim is not None
    # r-2's stored result claims a finding the recorded turns never reported.
    stored = make_finding(3)
    assert queue.ack("r-2", claim.token, TaskResult("finish", [stored], 1, 0.0, str(log)))

    report = replay_task(queue, "r-2")

    assert not report.identical and report.exit_code == 1
    text = report.render()
    assert text.splitlines()[0] == "r-2: findings differ (1 recorded, 5 replayed)"
    assert any(line.startswith("-") and '"f_3"' in line for line in text.splitlines())
    assert any(line.startswith("+") for line in text.splitlines())


# logs from before Event.stop_reason ---------------------------------------------------------


def test_infer_stop_reason_from_the_content():
    event = Event.model_validate({"task_id": "x", "step": 1, "ts": "2026-01-01T00:00:00Z"})
    text = [{"type": "text", "text": "hi"}]
    use = [{"type": "tool_use", "id": "t", "name": "finish", "input": {}}]
    assert infer_stop_reason(event, text) == "end_turn"
    assert infer_stop_reason(event, use) == "tool_use"
    assert infer_stop_reason(event, [*text, *use]) == "tool_use"
    assert infer_stop_reason(event, []) == "end_turn"
    stopped = event.model_copy(update={"result_summary": "stop: refusal"})
    assert infer_stop_reason(stopped, []) == "refusal"


def test_a_log_without_stop_reason_replays_identically(queue: SqliteQueue, events_dir: Path):
    log = record(queue, events_dir, clean_factory)
    rewrite(log, stop_reason=None)
    assert all(e.stop_reason is None for e in read_events(log))

    report = replay_task(queue, "r-1")

    assert report.identical and report.replayed_stop == "finish"
    assert all(e.stop_reason is not None for e in read_events(report.log_path) if e.model_output)


# a log that runs out ------------------------------------------------------------------------


def test_a_truncated_log_ends_in_model_error_with_the_findings_so_far(
    queue: SqliteQueue, events_dir: Path
):
    log = record(queue, events_dir, clean_factory)
    rewrite(log, keep=3)  # read_file and two report_finding turns

    report = replay_task(queue, "r-1")

    assert report.replayed_stop == "model_error"
    assert report.replayed_error is not None and "replay log exhausted" in report.replayed_error
    assert len(report.replayed) == 2 and not report.identical
    assert "note: stop finish recorded, model_error replayed" in report.render()


# refusal ------------------------------------------------------------------------------------


def refusal_factory(model_name: str) -> FakeModel:
    return FakeModel([tool_call("read_file", {"path": "app.py"}), refusal()])


def test_a_refusal_replays_as_a_refusal(queue: SqliteQueue, events_dir: Path):
    record(queue, events_dir, refusal_factory)
    recorded = queue.result("r-1")
    assert recorded is not None and recorded.stop_reason == "refusal"

    report = replay_task(queue, "r-1")

    assert report.identical and (report.recorded_stop, report.replayed_stop) == ("refusal",) * 2
    assert read_events(report.log_path)[-1].result_summary == "stop: refusal"


def test_an_old_refusal_log_is_recognised_from_the_terminal_summary(
    queue: SqliteQueue, events_dir: Path
):
    log = record(queue, events_dir, refusal_factory)
    rewrite(log, stop_reason=None)

    report = replay_task(queue, "r-1")

    assert report.replayed_stop == "refusal" and report.identical


# nothing to replay --------------------------------------------------------------------------


def test_unknown_task(queue: SqliteQueue):
    with pytest.raises(ReplayError, match="unknown task: nope"):
        replay_task(queue, "nope")


def test_task_without_a_result(queue: SqliteQueue):
    queue.enqueue(make_task("r-1"))
    with pytest.raises(ReplayError, match=r"no result yet \(state: queued\)"):
        replay_task(queue, "r-1")


def test_missing_log(queue: SqliteQueue, events_dir: Path):
    record(queue, events_dir, clean_factory).unlink()
    with pytest.raises(ReplayError, match="cannot read"):
        replay_task(queue, "r-1")


def test_corrupt_log(queue: SqliteQueue, events_dir: Path):
    log = record(queue, events_dir, clean_factory)
    log.write_text("not json\n")
    with pytest.raises(ReplayError, match="cannot read"):
        replay_task(queue, "r-1")
