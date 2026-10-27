"""Drive `run_command` through the real ToolRegistry and DockerSandbox, with no model ($0).

It shows what the agent's tool does for real: a grep and a `python3 -c` on ledger_api inside the
sandbox, printing each argv, exit code and output. Exit 0 only if every call ran and exited 0.

    uv run python -m src.sandbox.drive_tools
"""

import sys
import time
from collections.abc import Sequence
from dataclasses import dataclass

from evals import frozen
from src.agent.sandbox_protocol import SandboxFactory, SandboxUnavailableError
from src.agent.tools.registry import ToolRegistry
from src.contracts import Task, ToolName, ToolResult
from src.sandbox.docker_sandbox import create_sandbox

TARGET = "ledger_api"
CALL_TIMEOUT_S = 20
TASK_TIMEOUT_S = 120

# Counts `subprocess.<fn>(...)` calls per file under services/, from the AST.
COUNT_SUBPROCESS = (
    "import ast, pathlib\n"
    "for p in sorted(pathlib.Path('services').glob('*.py')):\n"
    "    tree = ast.parse(p.read_text())\n"
    "    n = sum(1 for x in ast.walk(tree) if isinstance(x, ast.Call)\n"
    "            and isinstance(x.func, ast.Attribute)\n"
    "            and isinstance(x.func.value, ast.Name) and x.func.value.id == 'subprocess')\n"
    "    print(p, n)\n"
)

COMMANDS: list[list[str]] = [
    ["grep", "-rn", "shell=True", "."],
    ["python3", "-c", COUNT_SUBPROCESS],
]


@dataclass(frozen=True)
class Call:
    argv: list[str]
    result: ToolResult


def drive(registry: ToolRegistry, commands: Sequence[list[str]] = COMMANDS) -> list[Call]:
    """Run each argv as a `run_command` call, as the agent loop would."""
    calls = []
    for i, argv in enumerate(commands, 1):
        args = {"argv": argv, "timeout_s": CALL_TIMEOUT_S}
        result = registry.execute(
            f"drive-{i}", ToolName.RUN_COMMAND.value, args, timeout_s=TASK_TIMEOUT_S
        )
        calls.append(Call(argv, result))
    return calls


def main(
    argv: Sequence[str] | None = None, *, sandbox_factory: SandboxFactory = create_sandbox
) -> int:
    target_dir = (frozen.TARGETS_DIR / TARGET).resolve()
    task = Task(
        task_id=f"drive-tools-{int(time.time())}",
        target_path=str(target_dir),
        goal="drive the tools by hand",
        max_steps=1,
        timeout_s=TASK_TIMEOUT_S,
        model="none",
    )
    try:
        sandbox = sandbox_factory(task)
    except SandboxUnavailableError as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    try:
        calls = drive(ToolRegistry(target_dir, sandbox))
    finally:
        sandbox.close()
    for call in calls:
        r = call.result
        shown = [a if "\n" not in a else "<script>" for a in call.argv]
        print(f"$ {' '.join(shown)}")
        print(f"ok={r.ok} exit_code={r.exit_code} error={r.error}")
        print(r.output.rstrip() or "(no output)")
        print()
    return 0 if all(c.result.ok and c.result.exit_code == 0 for c in calls) else 1


if __name__ == "__main__":
    sys.exit(main())
