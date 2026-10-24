"""Shared types for tool handlers."""

from dataclasses import dataclass


class ToolError(Exception):
    """An expected tool failure. The message is safe to show the model."""


@dataclass(frozen=True)
class Outcome:
    """What a handler produced; the registry turns it into a `ToolResult`."""

    output: str = ""
    ok: bool = True
    error: str | None = None
    exit_code: int | None = None
    truncated: bool = False
