import itertools
import json
import logging
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Any

import pytest

from src.agent.events import EventLog, read_events
from src.agent.loop import RunResult, StopReason, run_task
from src.agent.model import ModelError
from src.agent.prompts import NUDGE_MESSAGE, SYSTEM_PROMPT, WRAP_UP_MESSAGE
from src.contracts import Event, Task
from tests.agent.fakes import FakeClock, FakeModel, FakeSandbox, refusal, say, tool_call

FINDING = {
    "file": "app.py",
    "line_start": 4,
    "line_end": 4,
    "vuln_class": "command_injection",
    "severity": "high",
    "title": "os.system on user input",
    "explanation": "cmd reaches os.system unsanitised",
    "confidence": 0.9,
}


def report(*, cost: float = 0.0, **over: Any):
    return tool_call("report_finding", {**FINDING, **over}, cost=cost)


def search(i: int = 0, **kw: Any):
    return tool_call("search_code", {"pattern": f"nomatch{i}"}, **kw)


def finish():
    return tool_call("finish", {"summary": "done"})


def endless_searches():
    return (search(i) for i in itertools.count())


class Run:
    """Everything a test wants to look at after one `run_task`."""

    def __init__(self, result: RunResult, events: list[Event], model: FakeModel):
        self.result = result
        self.events = events
        self.model = model

    @property
    def terminal(self) -> Event:
        return self.events[-1]

    def last_user_blocks(self, call: int) -> list[dict[str, Any]]:
        """The content blocks of the final user message the model saw on `call` (0-based)."""
        content = self.model.calls[call]["messages"][-1]["content"]
        assert isinstance(content, list)
        return content


def go(
    target: Path,
    tmp_path: Path,
    script: Iterable[Any],
    *,
    max_steps: int = 10,
    timeout_s: int = 600,
    temperature: float = 0.0,
    clock: Callable[[], float] | None = None,
    on_call: Callable[[], None] | None = None,
    max_cost_usd: float = 0.50,
    sandbox: FakeSandbox | None = None,
) -> Run:
    task = Task(
        task_id="t1",
        target_path=str(target),
        goal="review the target",
        max_steps=max_steps,
        timeout_s=timeout_s,
        model="claude-haiku-5-5",
        temperature=temperature,
    )
    model = FakeModel(script, on_call=on_call)
    log_path = tmp_path / "events.jsonl"
    kwargs: dict[str, Any] = {"repo_root": target.parent, "max_cost_usd": max_cost_usd}
    if clock:
        kwargs["clock"] = clock
    if sandbox:
        kwargs["sandbox_factory"] = lambda _task: sandbox
    with EventLog(log_path) as events:
        result = run_task(task, model, events, **kwargs)
    return Run(result, read_events(log_path), model)


# --- stopping ------------------------------------------------------------------------------


def test_finish_stops_cleanly(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(), finish()])
    assert run.result.stop_reason is StopReason.FINISH
    assert run.result.clean
    assert run.result.steps == 2
    assert len(run.result.findings) == 1
    assert [e.step for e in run.events] == [1, 2, 3]
    assert run.terminal.tool is None and run.terminal.ok


def test_endless_tool_calls_end_as_max_steps(target: Path, tmp_path: Path):
    run = go(target, tmp_path, endless_searches(), max_steps=3)
    assert run.result.stop_reason is StopReason.MAX_STEPS
    assert run.result.clean
    assert run.result.steps == 3
    assert len(run.model.calls) == 3
    assert len(run.events) == 4  # 3 steps + the terminal event
    assert run.terminal.step == 4 and run.terminal.result_summary == "stop: max_steps"


def test_timeout_checked_before_each_model_call(target: Path, tmp_path: Path):
    log_path = tmp_path / "events.jsonl"

    def clock() -> float:  # jumps past the deadline once a step has been logged
        return 1000.0 if log_path.exists() and log_path.stat().st_size else 0.0

    run = go(target, tmp_path, endless_searches(), timeout_s=100, clock=clock)
    assert run.result.stop_reason is StopReason.TIMEOUT
    assert not run.result.clean
    assert len(run.model.calls) == 1
    assert [e.step for e in run.events] == [1, 2]
    assert run.terminal.ok is False and run.terminal.error == "timeout"


def test_deadline_passing_during_the_model_call_runs_no_tool(target: Path, tmp_path: Path):
    clock = FakeClock()
    script = [report(cost=0.01), finish()]
    run = go(target, tmp_path, script, timeout_s=60, clock=clock, on_call=lambda: clock.advance(61))
    assert run.result.stop_reason is StopReason.TIMEOUT
    assert run.result.findings == []  # the report_finding in that reply was not executed
    assert run.result.steps == 0
    assert run.result.cost_usd == pytest.approx(0.01)
    assert len(run.events) == 1
    ev = run.terminal
    assert (ev.step, ev.tokens_in, ev.tokens_out) == (1, 100, 10)
    assert ev.cost_usd == pytest.approx(0.01)
    assert ev.ok is False and ev.error == "timeout"


def test_model_timeout_is_the_remaining_time_capped_at_120(target: Path, tmp_path: Path):
    long = go(target, tmp_path, [finish()], timeout_s=600, clock=FakeClock())
    short = go(target, tmp_path, [finish()], timeout_s=30, clock=FakeClock())
    assert long.model.calls[0]["timeout_s"] == 120
    assert short.model.calls[0]["timeout_s"] == 30


def test_tool_timeout_is_clamped_to_the_time_left(target: Path, tmp_path: Path):
    sandbox = FakeSandbox()
    call = tool_call("run_command", {"argv": ["ls"], "timeout_s": 500})
    run = go(target, tmp_path, [call, finish()], timeout_s=50, sandbox=sandbox, clock=FakeClock())
    assert run.result.stop_reason is StopReason.FINISH
    assert [t for _, t in sandbox.calls] == [50]


def test_budget_stop_when_step_cost_crosses_the_cap(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(cost=0.3), search(cost=0.3), finish()], max_cost_usd=0.50)
    assert run.result.stop_reason is StopReason.BUDGET
    assert not run.result.clean
    assert run.result.steps == 2
    assert run.result.cost_usd == pytest.approx(0.6)
    assert len(run.result.findings) == 1  # kept
    assert len(run.model.calls) == 2
    assert run.terminal.ok is False and "cap" in (run.terminal.error or "")


def test_cost_at_the_cap_does_not_stop(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [search(cost=0.5), finish()], max_cost_usd=0.50)
    assert run.result.stop_reason is StopReason.FINISH


# --- events ---------------------------------------------------------------------------------


def test_each_step_is_on_disk_before_the_next_model_call(target: Path, tmp_path: Path):
    log_path = tmp_path / "events.jsonl"
    seen: list[int] = []
    run = go(
        target,
        tmp_path,
        [search(0), search(1), finish()],
        on_call=lambda: seen.append(len(read_events(log_path))),
    )
    assert seen == [0, 1, 2]
    assert run.result.steps == 3


def test_events_carry_tokens_cost_and_tool(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [tool_call("read_file", {"path": "app.py"}, cost=0.02), finish()])
    first = run.events[0]
    assert first.tool is not None and first.tool.value == "read_file"
    assert first.args == {"path": "app.py"}
    assert first.result is not None and first.result.ok
    assert (first.tokens_in, first.tokens_out) == (100, 10)
    assert first.cost_usd == pytest.approx(0.02)
    assert first.model == "claude-haiku-5-5"
    assert json.loads(first.model_output or "")[0]["name"] == "read_file"


def test_control_tools_log_events_with_no_tool(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(), finish()])
    assert [e.tool for e in run.events] == [None, None, None]
    assert run.events[0].args["file"] == "app.py"


def test_first_request_has_system_goal_and_all_tools(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [finish()])
    call = run.model.calls[0]
    assert call["system"] == SYSTEM_PROMPT
    names = {t["name"] for t in call["tools"]}
    assert {"read_file", "search_code", "report_finding", "finish"} <= names
    assert call["messages"][0]["content"][0]["text"] == "review the target"


# --- tools through the loop ----------------------------------------------------------------


def test_traversal_and_symlink_escape_are_refused(target: Path, tmp_path: Path):
    script = [
        tool_call("read_file", {"path": "../outside/secret.txt"}),
        tool_call("read_file", {"path": "link_file"}),
        tool_call("read_file", {"path": "/etc/passwd"}),
        finish(),
    ]
    run = go(target, tmp_path, script)
    assert run.result.stop_reason is StopReason.FINISH
    assert [e.ok for e in run.events[:3]] == [False, False, False]
    assert "TOP SECRET" not in json.dumps(run.model.calls[-1]["messages"])
    assert run.last_user_blocks(1)[0]["is_error"] is True


def test_oversized_output_is_truncated(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [tool_call("read_file", {"path": "big.txt"}), finish()])
    result = run.events[0].result
    assert result is not None and result.ok and result.truncated


def test_repeated_call_is_logged_uses_a_step_and_returns_the_cached_result(
    target: Path, tmp_path: Path
):
    same = {"pattern": "os.system"}
    script = [tool_call("search_code", same), tool_call("search_code", same), finish()]
    run = go(target, tmp_path, script)
    assert run.result.steps == 3
    assert len(run.events) == 4
    first = run.last_user_blocks(1)[0]["content"]
    second = run.last_user_blocks(2)[0]["content"]
    assert "app.py" in first and "[repeated call" not in first
    assert second.startswith("[repeated call") and first in second


def test_several_tool_calls_in_one_reply_run_only_the_first(target: Path, tmp_path: Path):
    reply = tool_call(
        "search_code", {"pattern": "os.system"}, extra_calls=[("read_file", {"path": "app.py"})]
    )
    run = go(target, tmp_path, [reply, finish()])
    blocks = run.last_user_blocks(1)
    assert len(blocks) == 2
    assistant_ids = [b["id"] for b in run.model.calls[1]["messages"][-2]["content"]]
    assert [b["tool_use_id"] for b in blocks] == assistant_ids
    assert blocks[1]["is_error"] is True and "one tool call" in blocks[1]["content"]
    assert len(run.events) == 3  # step 1, finish, terminal


# --- report_finding -------------------------------------------------------------------------


def test_report_finding_stores_a_repo_relative_path(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(), finish()])
    (finding,) = run.result.findings
    assert finding.file == "target/app.py"  # repo_root is target.parent in these tests
    assert finding.finding_id
    assert run.last_user_blocks(1)[0]["content"] == f"recorded {finding.finding_id}"


def test_duplicate_finding_is_stored_once(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(), report(title="same spot again"), finish()])
    assert len(run.result.findings) == 1
    assert run.last_user_blocks(2)[0]["content"].startswith("duplicate")
    assert "is_error" not in run.last_user_blocks(2)[0]


def test_report_finding_rejects_bad_locations_and_the_model_can_retry(target: Path, tmp_path: Path):
    # a valid call between the rejections keeps the invalid-in-a-row streak below its limit
    script = [
        report(file="nope.py"),
        report(line_start=4, line_end=99),
        search(),
        report(file="../outside/secret.txt"),
        report(line_start=3, line_end=2),
        report(),
        finish(),
    ]
    run = go(target, tmp_path, script)
    assert run.result.stop_reason is StopReason.FINISH
    for call in (1, 2, 4, 5):
        assert run.last_user_blocks(call)[0]["is_error"] is True
    assert "4 lines" in run.last_user_blocks(2)[0]["content"]
    assert run.last_user_blocks(6)[0]["content"].startswith("recorded")
    assert len(run.result.findings) == 1


def test_three_rejected_findings_in_a_row_stop_the_run(target: Path, tmp_path: Path):
    script = [report(file="nope.py"), report(line_end=99), report(file="x.py"), finish()]
    run = go(target, tmp_path, script)
    assert run.result.stop_reason is StopReason.INVALID_TOOL_CALLS
    assert run.result.steps == 3


def test_report_finding_rejects_bad_enum_and_missing_fields(target: Path, tmp_path: Path):
    bad_class = tool_call("report_finding", {**FINDING, "vuln_class": "rce"})
    missing = tool_call("report_finding", {"file": "app.py"})
    run = go(target, tmp_path, [bad_class, missing, finish()])
    assert run.result.findings == []
    assert run.last_user_blocks(1)[0]["is_error"] is True
    assert run.last_user_blocks(2)[0]["is_error"] is True


# --- invalid calls and replies without a tool -----------------------------------------------


def test_invalid_calls_do_not_crash_and_use_a_step(target: Path, tmp_path: Path):
    script = [
        tool_call("rm_rf", {"path": "/"}),
        tool_call("read_file", {"path": 5}),
        tool_call("read_file", {"path": "app.py"}, stop_reason="max_tokens"),
        finish(),
    ]
    run = go(target, tmp_path, script, max_steps=3)
    # three invalid calls in a row: stopped before the model could finish
    assert run.result.stop_reason is StopReason.INVALID_TOOL_CALLS
    assert run.result.steps == 3
    assert [e.ok for e in run.events[:3]] == [False, False, False]
    assert "cut off" in (run.events[2].error or "")


def test_the_invalid_streak_resets_after_a_valid_call(target: Path, tmp_path: Path):
    bad = lambda: tool_call("rm_rf", {})  # noqa: E731
    script = [bad(), bad(), search(), bad(), bad(), finish()]
    run = go(target, tmp_path, script)
    assert run.result.stop_reason is StopReason.FINISH


def test_ordinary_tool_failures_do_not_count_as_invalid(target: Path, tmp_path: Path):
    script = [tool_call("read_file", {"path": f"missing{i}.py"}) for i in range(5)] + [finish()]
    run = go(target, tmp_path, script)
    assert run.result.stop_reason is StopReason.FINISH


def test_a_reply_without_a_tool_gets_one_nudge_then_stops(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [say(), say(), finish()])
    assert run.result.stop_reason is StopReason.MODEL_STOPPED
    assert not run.result.clean
    assert len(run.model.calls) == 2
    assert run.last_user_blocks(1) == [{"type": "text", "text": NUDGE_MESSAGE}]
    assert run.events[0].ok is False and run.events[0].error == "no tool call"
    assert run.terminal.step == 2 and run.terminal.error == "model_stopped"


def test_the_nudge_works_and_the_counter_resets(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [say(), search(), say(), finish()])
    assert run.result.stop_reason is StopReason.FINISH
    assert run.result.steps == 4


# --- model failures keep findings ----------------------------------------------------------


def test_model_error_keeps_earlier_findings(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(), ModelError("APIStatusError (status 500)")])
    assert run.result.stop_reason is StopReason.MODEL_ERROR
    assert len(run.result.findings) == 1
    assert run.result.error == "APIStatusError (status 500)"
    assert run.terminal.error == "APIStatusError (status 500)"


def test_refusal_keeps_earlier_findings(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(), refusal()])
    assert run.result.stop_reason is StopReason.REFUSAL
    assert len(run.result.findings) == 1
    assert run.terminal.ok is False


def test_an_unexpected_bug_becomes_internal_error_and_keeps_findings(
    target: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    sandbox = FakeSandbox()
    with caplog.at_level(logging.ERROR):
        run = go(target, tmp_path, [report(), RuntimeError("boom")], sandbox=sandbox)
    assert run.result.stop_reason is StopReason.INTERNAL_ERROR
    assert run.result.error == "RuntimeError"
    assert len(run.result.findings) == 1
    assert sandbox.closed
    assert run.terminal.error == "RuntimeError"


def test_keyboard_interrupt_is_recorded_as_interrupted(target: Path, tmp_path: Path):
    run = go(target, tmp_path, [report(), KeyboardInterrupt()])
    assert run.result.stop_reason is StopReason.INTERRUPTED
    assert not run.result.clean
    assert len(run.result.findings) == 1
    assert run.terminal.error == "interrupted"


# --- prompts and housekeeping ---------------------------------------------------------------


def test_wrap_up_note_appears_only_when_two_steps_remain(target: Path, tmp_path: Path):
    run = go(target, tmp_path, endless_searches(), max_steps=4)
    has_note = [
        any(b.get("text") == WRAP_UP_MESSAGE for b in run.last_user_blocks(i)) for i in range(4)
    ]
    assert has_note == [False, False, True, False]


def test_nonzero_temperature_is_ignored_with_a_warning(
    target: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.WARNING, logger="src.agent.loop"):
        run = go(target, tmp_path, [finish()], temperature=0.7)
    assert any("ignored" in r.message for r in caplog.records)
    assert run.result.stop_reason is StopReason.FINISH


def test_zero_temperature_does_not_warn(
    target: Path, tmp_path: Path, caplog: pytest.LogCaptureFixture
):
    with caplog.at_level(logging.WARNING, logger="src.agent.loop"):
        go(target, tmp_path, [finish()])
    assert not caplog.records


def test_the_sandbox_is_closed_after_a_run(target: Path, tmp_path: Path):
    sandbox = FakeSandbox()
    go(target, tmp_path, [finish()], sandbox=sandbox)
    assert sandbox.closed
