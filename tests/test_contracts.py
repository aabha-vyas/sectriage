from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from src.contracts import (
    Event,
    Finding,
    ReadFileArgs,
    Task,
    ToolCall,
    ToolName,
    ToolResult,
)


def make_finding(**over) -> Finding:
    base = dict(
        finding_id="f1",
        file="app.py",
        line_start=3,
        line_end=5,
        vuln_class="sqli",
        severity="high",
        title="t",
        explanation="e",
        confidence=0.5,
    )
    return Finding(**{**base, **over})


def test_finding_rejects_reversed_range():
    with pytest.raises(ValidationError):
        make_finding(line_start=5, line_end=3)


@pytest.mark.parametrize(
    "bad",
    [
        "/etc/passwd",
        "a\\b.py",
        "../x.py",
        "a/../b.py",
        "./a.py",
        "a/./b.py",
        "a//b.py",
        "a/",
        "",
        "C:/x.py",
    ],
)
def test_finding_rejects_unsafe_paths(bad):
    with pytest.raises(ValidationError):
        make_finding(file=bad)


def test_finding_accepts_relative_posix_and_dedupe_key():
    f = make_finding(file="evals/targets/vuln_app/app.py")
    assert f.dedupe_key == ("evals/targets/vuln_app/app.py", 3, 5, "sqli")


def test_extra_fields_rejected():
    with pytest.raises(ValidationError):
        make_finding(bogus=1)


def test_toolcall_validates_args_against_tool():
    ToolCall(call_id="c1", tool=ToolName.READ_FILE, args={"path": "a.py"})
    with pytest.raises(ValidationError):
        ToolCall(call_id="c1", tool=ToolName.READ_FILE, args={"pattern": "x"})
    with pytest.raises(ValidationError):
        ToolCall(call_id="c1", tool=ToolName.RUN_COMMAND, args={"argv": [], "timeout_s": 5})


def test_read_file_args_range():
    ReadFileArgs(path="a.py", start_line=2, end_line=2)
    ReadFileArgs(path="a.py", start_line=2)
    with pytest.raises(ValidationError):
        ReadFileArgs(path="a.py", start_line=5, end_line=2)


@pytest.mark.parametrize("t", [-0.1, 1.1])
def test_task_temperature_bounds(t):
    with pytest.raises(ValidationError):
        Task(
            task_id="t",
            target_path=".",
            goal="g",
            max_steps=1,
            timeout_s=1,
            model="m",
            temperature=t,
        )


def test_event_jsonl_round_trip_with_replay_fields():
    ev = Event(
        task_id="t1",
        step=2,
        ts=datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC),
        trace_id="abc",
        call_id="c1",
        model="m",
        model_output="raw",
        tool=ToolName.READ_FILE,
        args={"path": "a.py"},
        result=ToolResult(call_id="c1", ok=True, output="x", duration_ms=3),
        result_summary="read a.py",
        tokens_in=10,
        tokens_out=5,
        cost_usd=0.01,
        latency_ms=7,
    )
    assert Event.from_jsonl(ev.to_jsonl()) == ev


def test_event_rejects_naive_datetime():
    with pytest.raises(ValidationError):
        Event(task_id="t", step=0, ts=datetime(2026, 1, 1))


def test_old_event_log_without_new_fields_still_loads():
    line = '{"task_id":"t","step":0,"ts":"2026-01-01T00:00:00Z"}'
    ev = Event.from_jsonl(line)
    assert ev.call_id is None and ev.result is None and ev.model_output is None
    assert ev.trace_id is None


def test_old_event_log_without_stop_reason_loads_with_none():
    line = '{"task_id":"t","step":1,"ts":"2026-01-01T00:00:00Z","model_output":"[]"}'
    assert Event.from_jsonl(line).stop_reason is None


def test_event_stop_reason_round_trips():
    ev = Event(task_id="t", step=1, ts=datetime(2026, 1, 1, tzinfo=UTC), stop_reason="tool_use")
    assert Event.from_jsonl(ev.to_jsonl()).stop_reason == "tool_use"


def test_tool_args_cover_every_tool_name():
    from src.contracts import TOOL_ARGS

    assert set(TOOL_ARGS) == set(ToolName)


def test_tool_result_ok_requires_no_error():
    ToolResult(call_id="c", ok=False, error="boom", duration_ms=1)
    ToolResult(call_id="c", ok=True, duration_ms=1)
    with pytest.raises(ValidationError):
        ToolResult(call_id="c", ok=True, error="boom", duration_ms=1)


def test_event_ok_requires_no_error():
    ts = datetime(2026, 1, 1, tzinfo=UTC)
    Event(task_id="t", step=0, ts=ts, ok=False, error="boom")
    with pytest.raises(ValidationError):
        Event(task_id="t", step=0, ts=ts, ok=True, error="boom")
