"""The seam between the agent and the sandbox lane.

The agent never executes model-supplied argv itself. `run_command`, `semgrep`, and `git` build and
validate an argv on the host and hand it to a `Sandbox`; the sandbox lane implements this Protocol.

Contract for implementations:
- `exec` runs `argv` directly (no shell) inside the task's container, with cwd at the target root,
  mounted read-only, with no network and CPU/memory/wall-clock limits.
- Paths in `argv` are relative to that cwd, in the same layout as the host target directory.
- `timeout_s` is a hard wall-clock cap; on expiry return `timed_out=True` instead of raising.
- One sandbox per task. `close` releases the container and is safe to call twice.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Protocol

from src.contracts import Task


@dataclass(frozen=True)
class ExecResult:
    stdout: str
    stderr: str
    exit_code: int | None
    timed_out: bool = False
    duration_ms: int = 0


class Sandbox(Protocol):
    def exec(self, argv: Sequence[str], *, timeout_s: int) -> ExecResult: ...

    def close(self) -> None: ...


SandboxFactory = Callable[[Task], Sandbox]


class SandboxUnavailableError(RuntimeError):
    """Raised by a sandbox that cannot run commands (for example, Docker is not available)."""


class UnavailableSandbox:
    """Fail-closed fallback: every exec is refused, so nothing model-supplied runs on the host."""

    def exec(self, argv: Sequence[str], *, timeout_s: int) -> ExecResult:
        raise SandboxUnavailableError("no sandbox is available in this run")

    def close(self) -> None:
        return None
