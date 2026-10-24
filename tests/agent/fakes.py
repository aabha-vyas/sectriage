"""Test doubles for the agent lane. Nothing here touches the network or runs a process."""

from collections.abc import Sequence

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
