"""Test doubles for the agent lane. Nothing here touches the network or runs a process."""

import copy
import itertools
from collections.abc import Callable, Iterable, Iterator, Sequence
from typing import Any

from src.agent.model import ModelError, ModelResponse, Usage
from src.agent.sandbox_protocol import ExecResult


class FakeSandbox:
    """Records every `exec` and replays canned results (then a default success)."""

    def __init__(self, *results: ExecResult):
        self.calls: list[tuple[list[str], int]] = []
        self._results = list(results)
        self.closed = False

    def exec(self, argv: Sequence[str], *, timeout_s: int) -> ExecResult:
        self.calls.append((list(argv), timeout_s))
        if self._results:
            return self._results.pop(0)
        return ExecResult(stdout="", stderr="", exit_code=0)

    def close(self) -> None:
        self.closed = True


class FakeClock:
    """A settable monotonic clock for deadline tests."""

    def __init__(self, now: float = 0.0):
        self.now = now

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


_ids = itertools.count(1)


def tool_call(
    name: str,
    args: dict[str, Any] | None = None,
    *,
    cost: float = 0.0,
    stop_reason: str = "tool_use",
    extra_calls: Sequence[tuple[str, dict[str, Any]]] = (),
) -> ModelResponse:
    """A model turn that calls one tool (or several, to exercise the one-call-per-step rule)."""
    content: list[dict[str, Any]] = [
        {"type": "tool_use", "id": f"toolu_{next(_ids)}", "name": n, "input": a}
        for n, a in [(name, args or {}), *extra_calls]
    ]
    return ModelResponse(
        content=content,
        stop_reason=stop_reason,
        usage=Usage(input_tokens=100, output_tokens=10),
        cost_usd=cost,
        latency_ms=5,
    )


def say(text: str = "thinking out loud", *, cost: float = 0.0) -> ModelResponse:
    """A model turn with text and no tool call."""
    return ModelResponse(
        content=[{"type": "text", "text": text}],
        stop_reason="end_turn",
        usage=Usage(input_tokens=100, output_tokens=10),
        cost_usd=cost,
        latency_ms=5,
    )


def refusal() -> ModelResponse:
    return ModelResponse(
        content=[],
        stop_reason="refusal",
        usage=Usage(input_tokens=100, output_tokens=0),
        latency_ms=5,
    )


class FakeModel:
    """Replays a script of `ModelResponse`s (or `ModelError`s to raise), one per `complete`.

    `script` may be an infinite iterator for endless-loop tests. `on_call` runs at the start of
    every call, which lets a test move a fake clock or inspect the event log mid-run.
    """

    model = "claude-haiku-5-5"

    def __init__(
        self,
        script: Iterable[ModelResponse | BaseException],
        *,
        on_call: Callable[[], None] | None = None,
    ):
        self._script: Iterator[ModelResponse | BaseException] = iter(script)
        self._on_call = on_call
        self.calls: list[dict[str, Any]] = []

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout_s: float,
    ) -> ModelResponse:
        self.calls.append(
            {
                "system": system,
                "tools": copy.deepcopy(tools),
                "messages": copy.deepcopy(messages),
                "timeout_s": timeout_s,
            }
        )
        if self._on_call:
            self._on_call()
        try:
            item = next(self._script)
        except StopIteration:
            raise ModelError("fake model script exhausted") from None
        if isinstance(item, BaseException):
            raise item
        return item
