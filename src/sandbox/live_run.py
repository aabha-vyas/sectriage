"""One live agent run with the Docker sandbox on: the "done" check for lane/sandbox.

It reuses `evals.live.make_runner` (preflight, spend ledger, budget) and hands it `create_sandbox`,
so the agent's `run_command` and `git` tools execute for real. Before any money is spent it starts
a throwaway sandbox on the target and lists it, so a missing image or an unreadable mount fails
here and not mid-run.

The run succeeds if the agent made at least one `run_command` call that ran and exited 0. The model
chooses its tools, so a run may not use it: then one more run is allowed (`--attempts`, max 2).
Each run is capped at `--max-cost-usd`; the ledger budget defaults to attempts x cap.

`--confirm-in-sandbox` changes the task goal so the agent is told to use `run_command`. Such a run
is labelled "instructed to use run_command": it shows the tool works for a model that is asked to
use it, not that the model chooses it. The goal used is saved as `goal.txt` beside the run.

    uv run --env-file .env python -m src.sandbox.live_run

Retire this once `evals/live.py` takes `--sandbox docker` (docs/sandbox-lane-plan.md, Follow-ups).
"""

import argparse
import json
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from evals import frozen
from evals.live import (
    DEFAULT_MAX_STEPS,
    MODEL,
    PER_RUN_CAP_USD,
    BudgetExceeded,
    PreflightError,
    RunFailed,
    make_runner,
    preflight,
)
from evals.run import DEFAULT_RUNS_DIR
from src.agent.events import is_header
from src.agent.sandbox_protocol import SandboxFactory, SandboxUnavailableError
from src.contracts import Event, Task, ToolName
from src.sandbox.docker_sandbox import create_sandbox

DEFAULT_TARGET = "ledger_api"
MAX_ATTEMPTS = 2
EXIT_OK, EXIT_NO_COMMAND, EXIT_ERROR = 0, 1, 2

# `--confirm-in-sandbox` swaps the agent's task goal for this one. The model is told to use
# run_command, so such a run says nothing about whether it would pick the tool on its own.
CONFIRM_GOAL = (
    "Review the target for security vulnerabilities. Before you report each suspected "
    "vulnerability, confirm it by running at least one command with run_command (for example "
    "grep -n for every use of the dangerous call, or python3 -c to check how the code behaves on "
    "a crafted input). The target is mounted read-only at /work and the sandbox has no network; "
    "do not start the target's server. Report with report_finding only the vulnerabilities you "
    "read in the code and confirmed with a command, then call finish."
)
INSTRUCTED = "instructed to use run_command"
GOAL_FILE = "goal.txt"


@dataclass(frozen=True)
class CommandStats:
    total: int = 0
    ok: int = 0  # ran in the sandbox and exited 0
    refused: int = 0  # the sandbox was unavailable, so nothing ran


def command_stats(events_path: Path) -> CommandStats:
    """Count `run_command` steps in an events JSONL. An unreadable log counts as no calls."""
    total = ok = refused = 0
    try:
        lines = events_path.read_text().splitlines()
        for line in filter(None, lines):
            if is_header(line):
                continue
            event = Event.model_validate_json(line)
            if event.tool is not ToolName.RUN_COMMAND or event.result is None:
                continue
            total += 1
            ok += event.result.ok
            refused += (event.result.error or "").startswith("sandbox unavailable")
    except (OSError, ValueError):
        return CommandStats()
    return CommandStats(total, ok, refused)


def _meta(out: Path) -> dict:
    try:
        return json.loads(out.with_name(f"{out.stem}.meta.json").read_text())
    except (OSError, ValueError):
        return {}


def _check_sandbox(factory: SandboxFactory, target_dir: Path) -> None:
    """Start a sandbox on the real target and list it. Raises SandboxUnavailableError if not."""
    task = Task(
        task_id="live-run-check",
        target_path=str(target_dir),
        goal="sandbox check",
        max_steps=1,
        timeout_s=60,
        model=MODEL,
    )
    sandbox = factory(task)
    try:
        listing = sandbox.exec(["ls", "-A"], timeout_s=20)
    finally:
        sandbox.close()
    if listing.exit_code != 0 or not listing.stdout.strip():
        raise SandboxUnavailableError(
            f"the target is not readable inside the sandbox (exit {listing.exit_code})"
        )


def _with_goal(agent_main: Callable[..., int] | None, goal: str) -> Callable[..., int]:
    """Wrap `agent_main` so `src.agent.run.GOAL` is `goal` for the call and restored after.

    `src.agent.run.main` has no goal option and reads that module global when it builds the Task;
    nothing in src/agent/ or evals/ is edited. A test pins that behaviour.
    """
    from src.agent import run as agent_run

    target = agent_main or agent_run.main

    def wrapped(argv: list[str], **kwargs: Any) -> int:
        original = agent_run.GOAL
        agent_run.GOAL = goal
        try:
            return target(argv, **kwargs)
        finally:
            agent_run.GOAL = original

    return wrapped


def main(
    argv: Sequence[str] | None = None,
    *,
    preflight_check: Callable[[], None] = preflight,
    agent_main: Callable[..., int] | None = None,
    sandbox_factory: SandboxFactory = create_sandbox,
    runs_dir: Path = DEFAULT_RUNS_DIR,
    now: datetime | None = None,
) -> int:
    """`preflight_check`, `agent_main`, `sandbox_factory` and `now` are injection points."""
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--target", default=DEFAULT_TARGET, help="a frozen target under evals/")
    parser.add_argument("--max-cost-usd", type=float, default=PER_RUN_CAP_USD, help="cap per run")
    parser.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    parser.add_argument("--attempts", type=int, choices=range(1, MAX_ATTEMPTS + 1), default=2)
    parser.add_argument("--budget-usd", type=float, help="ledger budget (default: attempts x cap)")
    parser.add_argument(
        "--confirm-in-sandbox",
        action="store_true",
        help=f"tell the agent to confirm findings with run_command ({INSTRUCTED}); "
        f"the goal is saved as {GOAL_FILE}",
    )
    args = parser.parse_args(argv)
    if args.confirm_in_sandbox:
        agent_main = _with_goal(agent_main, CONFIRM_GOAL)
    label = f" [{INSTRUCTED}]" if args.confirm_in_sandbox else ""

    budget = args.budget_usd if args.budget_usd is not None else args.attempts * args.max_cost_usd
    target_dir = frozen.TARGETS_DIR / args.target
    try:
        if not target_dir.is_dir():
            raise RunFailed(f"no such target: {args.target}")
        runner = make_runner(  # runs the preflight now, before anything is started
            budget_usd=budget,
            max_cost_usd=args.max_cost_usd,
            max_steps=args.max_steps,
            root=runs_dir,
            preflight_check=preflight_check,
            sandbox_factory=sandbox_factory,
            agent_main=agent_main,
        )
        _check_sandbox(sandbox_factory, target_dir)
        batch = runs_dir / f"{args.target}-sandbox-{(now or datetime.now(UTC)):%Y%m%dT%H%M%SZ}"
        for attempt in range(1, args.attempts + 1):
            out = batch / f"run{attempt}" / "findings.json"
            try:
                runner(args.target, out)
            finally:
                if args.confirm_in_sandbox:  # the meta does not record the goal
                    out.parent.mkdir(parents=True, exist_ok=True)
                    (out.parent / GOAL_FILE).write_text(f"{INSTRUCTED}\n\n{CONFIRM_GOAL}\n")
            stats = command_stats(out.with_name(f"{out.stem}.events.jsonl"))
            meta = _meta(out)
            print(
                f"attempt {attempt}/{args.attempts}{label}: stop={meta.get('stop_reason', '?')} "
                f"cost=${meta.get('cost_usd', 0):.4f} "
                f"run_command ok/total={stats.ok}/{stats.total} "
                f"refused={stats.refused} -> {out.parent}"
            )
            if stats.ok:
                return EXIT_OK
    except (PreflightError, BudgetExceeded, RunFailed, SandboxUnavailableError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    print(
        f"no run_command call succeeded in {args.attempts} run(s){label}; stopping", file=sys.stderr
    )
    return EXIT_NO_COMMAND


if __name__ == "__main__":
    sys.exit(main())
