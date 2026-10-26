"""Replay a finished task from its event log, with no model call and no spend.

`ReplayModelClient` serves the recorded model turns back to `run_task` in order. `replay_task`
runs the task again with it and compares the findings with the ones stored in the queue.

Why the log is enough: `loop.py` writes `model_output` on every step event and on the terminal
event of `timeout` (after a call), `refusal` and `model_stopped`. `finish`, `max_steps`, `budget`
and `model_error` terminals carry none. So the events that have a `model_output`, in order, are
exactly the model calls, and the recorded tool calls (with their ids) drive the tools again.

Limits, by design:
- Exact for clean runs (`finish`, `max_steps`) whose tools are the host read-only ones over
  targets that have not changed. Sandboxed tools are not stubbed: a replay that reaches one runs
  it against `UnavailableSandbox` and gets a different tool result than the original run.
- Stops driven by the clock or by an error are not reproduced. A `timeout` before any call, a
  `budget` stop or a `model_error` leaves no recorded turn to serve, so replay runs out of log
  and ends in `model_error` ("replay log exhausted") with the same findings found so far.
- Only a complete log replays. Replay the winning log (`results.log_path`), not a killed
  attempt's partial `.N.jsonl`.
"""

import difflib
import json
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from src.agent.events import EventLog, read_events
from src.agent.loop import run_task
from src.agent.model import ModelError, ModelResponse, Usage
from src.contracts import Event, Finding
from src.swarm.queue import TaskQueue

REPLAY_DIR = "replay"
REFUSAL_SUMMARY = "stop: refusal"


class ReplayError(RuntimeError):
    """The task cannot be replayed (unknown, unfinished, or its log is unusable)."""


def infer_stop_reason(event: Event, content: list[dict[str, Any]]) -> str:
    """The model's stop reason for a log written before `Event.stop_reason` existed."""
    if event.result_summary == REFUSAL_SUMMARY:
        return "refusal"
    if any(block.get("type") == "tool_use" for block in content):
        return "tool_use"
    return "end_turn"


class ReplayModelClient:
    """A `ModelClient` that returns the recorded turns of one task, in order."""

    def __init__(self, events: Sequence[Event]):
        if not events:
            raise ReplayError("the event log is empty")
        self.model: str = events[0].model or "replay"
        self._turns = [e for e in events if e.model_output is not None]
        self._next = 0

    @property
    def remaining(self) -> int:
        return len(self._turns) - self._next

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout_s: float,
    ) -> ModelResponse:
        if self._next >= len(self._turns):
            raise ModelError("replay log exhausted")
        event = self._turns[self._next]
        self._next += 1
        assert event.model_output is not None
        content = json.loads(event.model_output)
        return ModelResponse(
            content=content,
            stop_reason=event.stop_reason or infer_stop_reason(event, content),
            # The log keeps one input figure (cache tokens are folded in), so all of it goes here.
            usage=Usage(input_tokens=event.tokens_in, output_tokens=event.tokens_out),
            cost_usd=event.cost_usd,
            latency_ms=event.latency_ms,
        )


@dataclass(frozen=True)
class ReplayReport:
    task_id: str
    recorded_stop: str
    replayed_stop: str
    replayed_error: str | None
    recorded: list[Finding]
    replayed: list[Finding]
    log_path: Path

    @property
    def identical(self) -> bool:
        """Same findings. A different stop reason is reported but does not decide this."""
        return _dump(self.recorded) == _dump(self.replayed)

    @property
    def exit_code(self) -> int:
        return 0 if self.identical else 1

    def render(self) -> str:
        lines = [
            f"{self.task_id}: "
            + ("identical" if self.identical else "findings differ")
            + f" ({len(self.recorded)} recorded, {len(self.replayed)} replayed)"
        ]
        if not self.identical:
            lines.extend(
                difflib.unified_diff(
                    _lines(self.recorded),
                    _lines(self.replayed),
                    "recorded",
                    "replayed",
                    lineterm="",
                    n=0,
                )
            )
        if self.recorded_stop != self.replayed_stop:
            how = f" ({self.replayed_error})" if self.replayed_error else ""
            lines.append(
                f"note: stop {self.recorded_stop} recorded, {self.replayed_stop} replayed{how}"
            )
        lines.append(f"replay log: {self.log_path}")
        return "\n".join(lines)


def _dump(findings: Sequence[Finding]) -> list[dict[str, Any]]:
    return [f.model_dump(mode="json") for f in sorted(findings, key=lambda f: f.finding_id)]


def _lines(findings: Sequence[Finding]) -> list[str]:
    return [json.dumps(d, sort_keys=True) for d in _dump(findings)]


def replay_task(
    queue: TaskQueue, task_id: str, *, replay_dir: str | Path | None = None
) -> ReplayReport:
    """Re-run a finished task against its winning log and compare the findings.

    The replay's own log goes to `<events dir>/replay/<task_id>.jsonl` (overwritten), so the
    original attempt logs are never touched. Raises `ReplayError` if there is nothing to replay.
    """
    record = queue.get(task_id)
    if record is None:
        raise ReplayError(f"unknown task: {task_id}")
    result = queue.result(task_id)
    if result is None:
        raise ReplayError(f"{task_id} has no result yet (state: {record.state.value})")
    source = Path(result.log_path)
    try:
        events = read_events(source)
    except (OSError, ValueError) as e:
        raise ReplayError(f"cannot read {source}: {e}") from e

    out_dir = Path(replay_dir) if replay_dir is not None else source.parent / REPLAY_DIR
    out = out_dir / f"{task_id}.jsonl"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.unlink(missing_ok=True)  # EventLog appends; a replay starts a fresh log
    client = ReplayModelClient(events)
    with EventLog(out) as log:
        run = run_task(record.task, client, log)
    return ReplayReport(
        task_id=task_id,
        recorded_stop=result.stop_reason,
        replayed_stop=run.stop_reason.value,
        replayed_error=run.error,
        recorded=result.findings,
        replayed=run.findings,
        log_path=out,
    )
