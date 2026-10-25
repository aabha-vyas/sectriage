"""The agent loop: plan, call a tool, observe, update memory, stop at a budget.

Plain Python, one tool call per step. Every step appends one Event to the log before the next step
starts, and every run ends with a terminal Event that names the stop reason. `run_task` does not
raise on model or tool failures, so findings reported so far always come back in the `RunResult`.
"""

import json
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.agent.events import EventLog
from src.agent.memory import FindingStore, ScratchMemory, call_key, make_finding_id
from src.agent.model import ModelClient, ModelError, ModelResponse, Usage
from src.agent.paths import PathError, relative_posix, resolve_root, safe_resolve_file
from src.agent.prompts import (
    CONTROL_TOOLS,
    FINISH,
    NUDGE_MESSAGE,
    REPORT_FINDING,
    SYSTEM_PROMPT,
    WRAP_UP_MESSAGE,
)
from src.agent.sandbox_protocol import SandboxFactory, UnavailableSandbox
from src.agent.tools import host
from src.agent.tools.registry import ToolRegistry, validation_message
from src.contracts import TOOL_ARGS, Event, Finding, Task, ToolName, ToolResult

log = logging.getLogger(__name__)

MODEL_TIMEOUT_CAP_S = 120
MAX_INVALID_IN_A_ROW = 3
WRAP_UP_AT_STEPS_LEFT = 2
DEFAULT_MAX_COST_USD = 0.50
REPO_ROOT = Path(__file__).resolve().parents[2]


class StopReason(StrEnum):
    FINISH = "finish"
    MAX_STEPS = "max_steps"
    TIMEOUT = "timeout"
    BUDGET = "budget"
    INVALID_TOOL_CALLS = "invalid_tool_calls"
    MODEL_STOPPED = "model_stopped"
    MODEL_ERROR = "model_error"
    REFUSAL = "refusal"
    INTERNAL_ERROR = "internal_error"
    INTERRUPTED = "interrupted"


_CLEAN_STOPS = {StopReason.FINISH, StopReason.MAX_STEPS}


@dataclass
class RunResult:
    task_id: str
    stop_reason: StopReason
    findings: list[Finding] = field(default_factory=list)
    steps: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    usage: Usage = field(default_factory=Usage)

    @property
    def clean(self) -> bool:
        return self.stop_reason in _CLEAN_STOPS


@dataclass
class _Outcome:
    """What one tool call produced, before it becomes a tool_result and an Event."""

    result: ToolResult
    tool: ToolName | None
    invalid: bool = False
    finish: bool = False


def _result(
    call_id: str,
    ok: bool,
    output: str = "",
    error: str | None = None,
    started: float | None = None,
) -> ToolResult:
    elapsed = 0.0 if started is None else time.perf_counter() - started
    return ToolResult(
        call_id=call_id,
        ok=ok,
        output=output,
        error=None if ok else (error or "failed"),
        duration_ms=int(elapsed * 1000),
    )


def _is_invalid_call(name: str, args: Any) -> bool:
    """True for an unknown tool or arguments that fail the contract schema."""
    try:
        tool = ToolName(name)
    except ValueError:
        return True
    if not isinstance(args, dict):
        return True
    try:
        TOOL_ARGS[tool].model_validate(args)
    except ValidationError:
        return True
    return False


def _summary(result: ToolResult) -> str:
    if result.ok:
        return f"ok: {len(result.output)} chars" + (" (truncated)" if result.truncated else "")
    return f"error: {result.error}"[:200]


def _tool_result_block(call_id: str, result: ToolResult) -> dict[str, Any]:
    if result.ok:
        text = result.output or "(no output)"
    else:
        text = result.error or "failed"
        if result.output:
            text += "\n" + result.output
    block: dict[str, Any] = {"type": "tool_result", "tool_use_id": call_id, "content": text}
    if not result.ok:
        block["is_error"] = True
    return block


class _Run:
    """State for one task; `run_task` is the public entry point."""

    def __init__(
        self,
        task: Task,
        model: ModelClient,
        events: EventLog,
        registry: ToolRegistry,
        root: Path,
        repo_prefix: str,
        clock: Callable[[], float],
        max_cost_usd: float,
    ):
        self.task = task
        self.model = model
        self.events = events
        self.registry = registry
        self.root = root
        self.repo_prefix = repo_prefix
        self.clock = clock
        self.max_cost_usd = max_cost_usd
        self.started = clock()
        self.store = FindingStore()
        self.memory = ScratchMemory()
        self.tools = [*registry.schemas(), *CONTROL_TOOLS]
        self.messages: list[dict[str, Any]] = []
        self.steps = 0
        self.cost = 0.0
        self.usage = Usage()

    def remaining_s(self) -> float:
        return self.task.timeout_s - (self.clock() - self.started)

    def build_result(self, reason: StopReason, error: str | None = None) -> RunResult:
        return RunResult(
            task_id=self.task.task_id,
            stop_reason=reason,
            findings=self.store.all(),
            steps=self.steps,
            cost_usd=self.cost,
            error=error,
            usage=self.usage,
        )

    # events -------------------------------------------------------------------------------

    def _event(self, step: int, resp: ModelResponse | None = None, **fields: Any) -> Event:
        usage = resp.usage if resp else None
        return Event(
            task_id=self.task.task_id,
            step=step,
            ts=datetime.now(UTC),
            model=self.model.model,
            model_output=json.dumps(resp.content) if resp else None,
            stop_reason=resp.stop_reason if resp else None,
            tokens_in=(usage.input_tokens + usage.cache_write_tokens + usage.cache_read_tokens)
            if usage
            else 0,
            tokens_out=usage.output_tokens if usage else 0,
            cost_usd=resp.cost_usd if resp else 0.0,
            latency_ms=resp.latency_ms if resp else 0,
            **fields,
        )

    def stop(
        self, reason: StopReason, resp: ModelResponse | None = None, error: str | None = None
    ) -> RunResult:
        """Write the terminal Event and build the result."""
        clean = reason in _CLEAN_STOPS
        self.events.append(
            self._event(
                self.steps + 1,
                resp,
                result_summary=f"stop: {reason.value}",
                ok=clean,
                error=None if clean else (error or reason.value),
            )
        )
        return self.build_result(reason, error)

    # messages -----------------------------------------------------------------------------

    def user_message(self, blocks: list[dict[str, Any]]) -> dict[str, Any]:
        """The next user turn. Adds the wrap-up note when the coming step is the second-to-last."""
        steps_left = self.task.max_steps - self.steps
        if steps_left == WRAP_UP_AT_STEPS_LEFT:
            blocks = [*blocks, {"type": "text", "text": WRAP_UP_MESSAGE}]
        return {"role": "user", "content": blocks}

    # tools --------------------------------------------------------------------------------

    def report_finding(self, call_id: str, args: Any) -> _Outcome:
        started = time.perf_counter()

        def bad(message: str) -> _Outcome:
            return _Outcome(_result(call_id, False, error=message, started=started), None, True)

        if not isinstance(args, dict):
            return bad("invalid arguments: expected a JSON object")
        file = args.get("file")
        if not isinstance(file, str):
            return bad("invalid arguments: file: must be a string")
        try:
            path = safe_resolve_file(self.root, file)
        except PathError as e:
            return bad(f"invalid file: {e}")
        rel = relative_posix(self.root, path)
        repo_file = f"{self.repo_prefix}/{rel}" if self.repo_prefix else rel
        try:
            draft = Finding.model_validate({**args, "finding_id": "pending", "file": repo_file})
        except ValidationError as e:
            return bad(validation_message(e))
        total = host.line_count(self.root, rel)
        if draft.line_end > total:
            return bad(f"line_end {draft.line_end} is beyond the end of the file ({total} lines)")
        finding = draft.model_copy(
            update={
                "finding_id": make_finding_id(
                    draft.file, draft.line_start, draft.line_end, draft.vuln_class
                )
            }
        )
        if self.store.add(finding):
            out = f"recorded {finding.finding_id}"
        else:
            out = "duplicate: this finding is already recorded"
        return _Outcome(_result(call_id, True, out, started=started), None)

    def run_tool(self, call_id: str, name: str, args: Any) -> _Outcome:
        if name == FINISH:
            return _Outcome(_result(call_id, True, "finished"), None, finish=True)
        if name == REPORT_FINDING:
            return self.report_finding(call_id, args)

        invalid = _is_invalid_call(name, args)
        try:
            tool: ToolName | None = ToolName(name)
        except ValueError:
            tool = None
        key = call_key(name, args) if isinstance(args, dict) else None
        seen = self.memory.seen(key) if key else None
        if seen is not None:
            note = "[repeated call: same result as before]\n"
            repeat = seen.model_copy(
                update={"call_id": call_id, "output": note + seen.output, "duration_ms": 0}
            )
            return _Outcome(repeat, tool)

        result = self.registry.execute(
            call_id, name, args, timeout_s=max(1, int(self.remaining_s()))
        )
        if result.ok and key:
            self.memory.remember(key, result)
        return _Outcome(result, tool, invalid)

    # loop ---------------------------------------------------------------------------------

    def run(self) -> RunResult:
        self.messages.append(self.user_message([{"type": "text", "text": self.task.goal}]))
        no_tool_replies = 0
        invalid_streak = 0

        for step in range(1, self.task.max_steps + 1):
            remaining = self.remaining_s()
            if remaining <= 0:
                return self.stop(StopReason.TIMEOUT)

            try:
                resp = self.model.complete(
                    system=SYSTEM_PROMPT,
                    tools=self.tools,
                    messages=self.messages,
                    timeout_s=min(remaining, MODEL_TIMEOUT_CAP_S),
                )
            except ModelError as e:
                return self.stop(StopReason.MODEL_ERROR, error=str(e))
            self.cost += resp.cost_usd
            self.usage += resp.usage

            if self.remaining_s() <= 0:  # the call ran past the deadline: log it, run no tool
                return self.stop(StopReason.TIMEOUT, resp)

            if resp.stop_reason == "refusal":
                return self.stop(StopReason.REFUSAL, resp)

            tool_uses = [b for b in resp.content if b.get("type") == "tool_use"]
            if not tool_uses:
                no_tool_replies += 1
                if no_tool_replies > 1:
                    return self.stop(StopReason.MODEL_STOPPED, resp)
                self.steps = step
                self.events.append(
                    self._event(
                        step,
                        resp,
                        result_summary="no tool call; nudged",
                        ok=False,
                        error="no tool call",
                    )
                )
                self.messages.append(
                    {"role": "assistant", "content": resp.content or [_empty_text()]}
                )
                self.messages.append(self.user_message([{"type": "text", "text": NUDGE_MESSAGE}]))
                if self.cost > self.max_cost_usd:
                    return self.stop(StopReason.BUDGET)
                continue
            no_tool_replies = 0

            first, extra = tool_uses[0], tool_uses[1:]
            if resp.stop_reason == "max_tokens":
                started = time.perf_counter()
                outcome = _Outcome(
                    _result(
                        first["id"],
                        False,
                        error="your reply was cut off before the tool call finished; "
                        "make a shorter call",
                        started=started,
                    ),
                    None,
                    invalid=True,
                )
            else:
                outcome = self.run_tool(first["id"], first["name"], first.get("input", {}))

            self.steps = step
            self.events.append(
                self._event(
                    step,
                    resp,
                    call_id=first["id"],
                    tool=outcome.tool,
                    args=first.get("input") if isinstance(first.get("input"), dict) else {},
                    result=outcome.result,
                    result_summary=_summary(outcome.result),
                    ok=outcome.result.ok,
                    error=outcome.result.error,
                )
            )

            blocks = [_tool_result_block(first["id"], outcome.result)]
            blocks += [
                _tool_result_block(
                    b["id"],
                    _result(b["id"], False, error="only one tool call per step is allowed"),
                )
                for b in extra
            ]
            self.messages.append({"role": "assistant", "content": resp.content})
            self.messages.append(self.user_message(blocks))

            if outcome.finish:
                return self.stop(StopReason.FINISH)
            invalid_streak = invalid_streak + 1 if outcome.invalid else 0
            if invalid_streak >= MAX_INVALID_IN_A_ROW:
                return self.stop(
                    StopReason.INVALID_TOOL_CALLS,
                    error=f"{MAX_INVALID_IN_A_ROW} invalid tool calls in a row",
                )
            if self.cost > self.max_cost_usd:
                return self.stop(
                    StopReason.BUDGET,
                    error=f"cost ${self.cost:.4f} exceeded the ${self.max_cost_usd:.2f} cap",
                )

        return self.stop(StopReason.MAX_STEPS)


def _empty_text() -> dict[str, Any]:
    return {"type": "text", "text": "(no content)"}


def _repo_prefix(root: Path, repo_root: Path) -> str:
    """Target path relative to the repo, so stored findings match the ground-truth `file` form."""
    try:
        return relative_posix(repo_root.resolve(), root)
    except ValueError:
        return ""


def run_task(
    task: Task,
    model: ModelClient,
    events: EventLog,
    *,
    sandbox_factory: SandboxFactory | None = None,
    clock: Callable[[], float] = time.monotonic,
    max_cost_usd: float = DEFAULT_MAX_COST_USD,
    repo_root: Path = REPO_ROOT,
) -> RunResult:
    """Run one task to a stop reason. Raises only for a bad `task.target_path`."""
    if task.temperature != 0.0:
        log.warning("temperature=%s is ignored: it is not sent to the model", task.temperature)
    root = resolve_root(task.target_path)
    sandbox = sandbox_factory(task) if sandbox_factory else UnavailableSandbox()
    try:
        run = _Run(
            task,
            model,
            events,
            ToolRegistry(root, sandbox),
            root,
            _repo_prefix(root, repo_root),
            clock,
            max_cost_usd,
        )
        try:
            return run.run()
        except KeyboardInterrupt:  # Ctrl-C keeps the findings and closes the log properly
            return run.stop(StopReason.INTERRUPTED)
        except Exception as e:  # a bug must not lose the findings reported so far
            log.exception("unexpected error in the agent loop")
            return run.stop(StopReason.INTERNAL_ERROR, error=type(e).__name__)
    finally:
        sandbox.close()
