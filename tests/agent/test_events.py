from datetime import UTC, datetime
from pathlib import Path

from src.agent.events import EventLog, read_events
from src.contracts import Event, ToolName, ToolResult


def event(step: int, **kw: object) -> Event:
    fields: dict[str, object] = {"task_id": "t1", "step": step, "ts": datetime.now(UTC)}
    fields.update(kw)
    return Event.model_validate(fields)


def test_each_append_is_on_disk_before_close(tmp_path: Path):
    path = tmp_path / "run.events.jsonl"
    log = EventLog(path)
    try:
        for step in range(3):
            log.append(event(step))
            # Read through a separate handle while the log is still open.
            assert len(path.read_text().splitlines()) == step + 1
    finally:
        log.close()


def test_round_trip_keeps_full_step_data(tmp_path: Path):
    path = tmp_path / "run.events.jsonl"
    original = event(
        1,
        call_id="toolu_1",
        model="claude-haiku-5-5",
        model_output="reading app.py",
        tool=ToolName.READ_FILE,
        args={"path": "app.py", "start_line": 1},
        result=ToolResult(call_id="toolu_1", ok=True, output="1: x", duration_ms=3),
        tokens_in=100,
        tokens_out=20,
        cost_usd=0.001,
        latency_ms=450,
    )
    with EventLog(path) as log:
        log.append(original)
    [loaded] = read_events(path)
    assert loaded == original


def test_one_json_object_per_line(tmp_path: Path):
    path = tmp_path / "run.events.jsonl"
    with EventLog(path) as log:
        log.append(event(0, model_output="multi\nline"))
        log.append(event(1))
    lines = path.read_text().splitlines()
    assert len(lines) == 2
    assert [Event.from_jsonl(line).step for line in lines] == [0, 1]


def test_creates_parent_directories(tmp_path: Path):
    path = tmp_path / "a" / "b" / "run.events.jsonl"
    with EventLog(path) as log:
        log.append(event(0))
    assert path.exists()


def test_reopening_appends(tmp_path: Path):
    path = tmp_path / "run.events.jsonl"
    with EventLog(path) as log:
        log.append(event(0))
    with EventLog(path) as log:
        log.append(event(1))
    assert [e.step for e in read_events(path)] == [0, 1]


def test_close_is_idempotent(tmp_path: Path):
    log = EventLog(tmp_path / "x.jsonl")
    log.close()
    log.close()
