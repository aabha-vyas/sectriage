"""One registry for the five contract tools: schema listing, validation, dispatch."""

import time
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from src.agent.paths import cap_output
from src.agent.sandbox_protocol import Sandbox
from src.agent.tools import host, sandboxed
from src.agent.tools.base import Outcome, ToolError
from src.contracts import TOOL_ARGS, ToolName, ToolResult

_MAX_ERROR_CHARS = 500

DESCRIPTIONS: dict[ToolName, str] = {
    ToolName.READ_FILE: (
        "Read a file in the target, with line numbers. Paths are relative to the target root. "
        "Use start_line/end_line (1-based, inclusive) to read part of a large file."
    ),
    ToolName.SEARCH_CODE: (
        "Find lines containing a literal, case-sensitive substring (not a regex) in the target. "
        "Optional path limits the search to a file or directory. Output is path:line: text."
    ),
    ToolName.RUN_COMMAND: (
        "Run a command (argv list, no shell) in the no-network sandbox with the target mounted "
        "read-only as the working directory."
    ),
    ToolName.SEMGREP: (
        "Run semgrep in the sandbox. config and path must be existing paths inside the target; "
        "registry and URL configs are not available."
    ),
    ToolName.GIT: (
        "Run a read-only git command in the sandbox. Allowed subcommands: "
        f"{', '.join(sorted(sandboxed.GIT_SUBCOMMANDS))}. Only common read-only options are "
        "accepted; pathspecs go after `--` and must exist in the target."
    ),
}


def validation_message(e: ValidationError) -> str:
    """Field paths and messages only; never the model-supplied values."""
    parts = [
        f"{'.'.join(str(p) for p in err['loc']) or 'args'}: {err['msg']}" for err in e.errors()
    ]
    return ("invalid arguments: " + "; ".join(parts))[:_MAX_ERROR_CHARS]


class ToolRegistry:
    def __init__(self, root: Path, sandbox: Sandbox):
        self._root = root
        self._sandbox = sandbox

    def schemas(self) -> list[dict[str, Any]]:
        """Tool definitions in a fixed order so the cached prompt prefix stays byte-stable."""
        return [
            {
                "name": tool.value,
                "description": DESCRIPTIONS[tool],
                "input_schema": TOOL_ARGS[tool].model_json_schema(),
            }
            for tool in ToolName
        ]

    def execute(self, call_id: str, name: str, raw_args: Any, *, timeout_s: int) -> ToolResult:
        """Run one tool call. Never raises; failures come back as `ok=False` results.

        `timeout_s` is the time left in the task; sandboxed tools clamp their own timeout to it.
        """
        started = time.perf_counter()

        def result(outcome: Outcome) -> ToolResult:
            output, capped = cap_output(outcome.output)
            return ToolResult(
                call_id=call_id,
                ok=outcome.ok,
                output=output,
                truncated=outcome.truncated or capped,
                error=None if outcome.ok else (outcome.error or "failed"),
                exit_code=outcome.exit_code,
                duration_ms=int((time.perf_counter() - started) * 1000),
            )

        def fail(message: str) -> ToolResult:
            return result(Outcome(ok=False, error=message))

        try:
            tool = ToolName(name)
        except ValueError:
            return fail(f"unknown tool; available: {', '.join(t.value for t in ToolName)}")
        if not isinstance(raw_args, dict):
            return fail("invalid arguments: expected a JSON object")
        try:
            args = TOOL_ARGS[tool].model_validate(raw_args)
        except ValidationError as e:
            return fail(validation_message(e))

        try:
            return result(self._dispatch(tool, args, timeout_s))
        except ToolError as e:
            return fail(str(e))
        except Exception as e:  # handlers must not crash the loop; no host details in the message
            return fail(f"internal error: {type(e).__name__}")

    def _dispatch(self, tool: ToolName, args: Any, remaining_s: int) -> Outcome:
        match tool:
            case ToolName.READ_FILE:
                return host.read_file(self._root, args)
            case ToolName.SEARCH_CODE:
                return host.search_code(self._root, args)
            case ToolName.RUN_COMMAND:
                return sandboxed.run_command(self._sandbox, args, remaining_s)
            case ToolName.SEMGREP:
                return sandboxed.semgrep(self._root, self._sandbox, args, remaining_s)
            case ToolName.GIT:
                return sandboxed.git(self._root, self._sandbox, args, remaining_s)
