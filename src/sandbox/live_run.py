"""One live agent run with the Docker sandbox on: the "done" check for lane/sandbox.

It reuses `evals.live.make_runner` (preflight, spend ledger, budget) and hands it `create_sandbox`,
so the agent's `run_command` and `git` tools execute for real. Before any money is spent it starts
a throwaway sandbox on the target and lists it, so a missing image or an unreadable mount fails
here and not mid-run.

The run succeeds if the agent made at least one `run_command` call that ran and exited 0. The model
chooses its tools, so a run may not use it: then one more run is allowed (`--attempts`, max 2).
Each run is capped at `--max-cost-usd`; the ledger budget defaults to attempts x cap.

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
from src.agent.sandbox_protocol import SandboxFactory, SandboxUnavailableError
from src.contracts import Event, Task, ToolName
from src.sandbox.docker_sandbox import create_sandbox

DEFAULT_TARGET = "ledger_api"
MAX_ATTEMPTS = 2
EXIT_OK, EXIT_NO_COMMAND, EXIT_ERROR = 0, 1, 2


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
    args = parser.parse_args(argv)

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
            runner(args.target, out)
            stats = command_stats(out.with_name(f"{out.stem}.events.jsonl"))
            meta = _meta(out)
            print(
                f"attempt {attempt}/{args.attempts}: stop={meta.get('stop_reason', '?')} "
                f"cost=${meta.get('cost_usd', 0):.4f} "
                f"run_command ok/total={stats.ok}/{stats.total} "
                f"refused={stats.refused} -> {out.parent}"
            )
            if stats.ok:
                return EXIT_OK
    except (PreflightError, BudgetExceeded, RunFailed, SandboxUnavailableError, ValueError) as e:
        print(f"error: {type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    print(f"no run_command call succeeded in {args.attempts} run(s); stopping", file=sys.stderr)
    return EXIT_NO_COMMAND


if __name__ == "__main__":
    sys.exit(main())
