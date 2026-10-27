"""Live runner behind `evals.run --repeat`: real agent runs, a spend ledger and a preflight.

Each call runs `src.agent.run.main` in-process on a fresh task, with the model pinned and the
current PROMPT_VERSION, writing findings, `.meta.json` and `.events.jsonl` where the caller says
(`logs/evals/<run-id>/`, outside evals/targets/). Before any run:

  preflight  the frozen targets match their lock, `git status` is clean for evals/targets, and
             ANTHROPIC_API_KEY came in through the env file (`uv run --env-file .env ...`)
  ledger     spend is summed from every run's meta (cost_usd) into <root>/spend.json; a run
             is refused when spent + the per-run cap would pass --budget-usd

Runs with an error stop (model_error, internal_error, interrupted) or a nonzero CLI exit raise
`RunFailed`; capped or refused runs (budget, timeout, refusal, ...) are results, not errors, and
show up as stop reasons and in clean_runs.
"""

import json
import os
import subprocess
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from evals import frozen
from evals.results import DEFAULT_MAX_STEPS
from evals.run import DEFAULT_RUNS_DIR, Runner
from src.agent.loop import DEFAULT_MAX_COST_USD, StopReason
from src.agent.model import ModelClient
from src.agent.sandbox_protocol import SandboxFactory

MODEL = "claude-haiku-5-5"  # pinned; not the CLI default, so a changed default cannot slip in
PER_RUN_CAP_USD = DEFAULT_MAX_COST_USD
DEFAULT_BUDGET_USD = 2.00
SPEND_FILE = "spend.json"
KEY_NAME = "ANTHROPIC_API_KEY"
ENV_FILE = ".env"
ERROR_STOPS = {StopReason.MODEL_ERROR, StopReason.INTERNAL_ERROR, StopReason.INTERRUPTED}
_EPS = 1e-9


class PreflightError(RuntimeError):
    """A precondition for live runs does not hold; nothing was started."""


class BudgetExceeded(RuntimeError):
    """The next run could pass the budget; it was not started."""


class RunFailed(RuntimeError):
    """A run ended in an error. Its artifacts and the earlier runs' stay on disk."""


# --- spend ledger ----------------------------------------------------------------------------


@dataclass(frozen=True)
class Spend:
    total_usd: float
    runs: list[dict]  # {"run": <dir relative to root>, "cost_usd": float, "estimated": bool}


def sum_spend(root: Path, assumed_cost_usd: float) -> Spend:
    """Sum cost_usd over every run's meta under `root`.

    A run that left no readable meta (killed mid-run) has an unknown cost, so it counts at
    `assumed_cost_usd`, the per-run cap, which is the most it could have spent.
    """
    runs: list[dict] = []

    def add(path: Path, cost: float, estimated: bool) -> None:
        runs.append(
            {
                "run": path.parent.relative_to(root).as_posix(),
                "cost_usd": cost,
                "estimated": estimated,
            }
        )

    for meta_path in sorted(root.rglob("*.meta.json")):
        try:
            add(meta_path, float(json.loads(meta_path.read_text())["cost_usd"]), False)
        except (OSError, ValueError, KeyError, TypeError):
            add(meta_path, assumed_cost_usd, True)
    for events in sorted(root.rglob("*.events.jsonl")):
        meta_path = events.with_name(events.name.removesuffix(".events.jsonl") + ".meta.json")
        if not meta_path.exists():
            add(events, assumed_cost_usd, True)
    total = round(sum(r["cost_usd"] for r in runs), 6)
    return Spend(total_usd=total, runs=runs)


def update_ledger(root: Path, assumed_cost_usd: float) -> Spend:
    """Recompute the spend from the run metas and write <root>/spend.json."""
    spend = sum_spend(root, assumed_cost_usd) if root.exists() else Spend(0.0, [])
    root.mkdir(parents=True, exist_ok=True)
    payload = {"total_usd": spend.total_usd, "runs": spend.runs}
    (root / SPEND_FILE).write_text(json.dumps(payload, indent=2) + "\n")
    return spend


def check_budget(spend: Spend, *, per_run_cap_usd: float, budget_usd: float) -> None:
    if spend.total_usd + per_run_cap_usd > budget_usd + _EPS:
        raise BudgetExceeded(
            f"refusing to start a run: spent ${spend.total_usd:.4f} + per-run cap "
            f"${per_run_cap_usd:.2f} would pass the ${budget_usd:.2f} budget"
        )


# --- preflight -------------------------------------------------------------------------------


def frozen_problems() -> list[str]:
    diff = frozen.differences(frozen.load_lock(), frozen.current_hashes())
    return [f"frozen targets differ from {frozen.LOCK.name}: {d}" for d in diff]


def git_problems(repo: Path, targets: str = "evals/targets") -> list[str]:
    try:
        out = subprocess.run(
            ["git", "status", "--porcelain", "--untracked-files=all", "--", targets],
            cwd=repo,
            capture_output=True,
            text=True,
            timeout=60,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"cannot run git status: {type(e).__name__}"]
    if out.returncode != 0:
        return [f"git status failed in {repo}"]
    return [f"uncommitted change in {targets}: {line}" for line in out.stdout.splitlines()]


def key_problems(env: Mapping[str, str], env_file: Path) -> list[str]:
    """The key must be in the process env AND defined in the env file, so it was loaded from
    there (`uv run --env-file .env`) rather than exported. Its value is never read into a message.
    """
    problems = []
    try:
        in_file = any(
            line.startswith(f"{KEY_NAME}=") and line.partition("=")[2].strip()
            for line in env_file.read_text().splitlines()
        )
    except OSError:
        in_file = False
    if not in_file:
        problems.append(f"{KEY_NAME} is not defined in {env_file} (copy .env.example)")
    if not env.get(KEY_NAME):
        problems.append(f"{KEY_NAME} is not in the environment; run with `uv run --env-file .env`")
    return problems


def preflight(
    *,
    repo: Path | None = None,
    env: Mapping[str, str] | None = None,
    env_file: Path | None = None,
) -> None:
    """Raise PreflightError listing every failed check."""
    repo = repo or frozen.ROOT.parent
    problems = [
        *frozen_problems(),
        *git_problems(repo),
        *key_problems(os.environ if env is None else env, env_file or repo / ENV_FILE),
    ]
    if problems:
        raise PreflightError("preflight failed:\n  - " + "\n  - ".join(problems))


# --- the runner ------------------------------------------------------------------------------


def make_runner(
    *,
    budget_usd: float = DEFAULT_BUDGET_USD,
    max_cost_usd: float = PER_RUN_CAP_USD,
    max_steps: int = DEFAULT_MAX_STEPS,
    root: Path = DEFAULT_RUNS_DIR,
    preflight_check: Callable[[], None] = preflight,
    model_factory: Callable[[], ModelClient] | None = None,
    sandbox_factory: SandboxFactory | None = None,
    agent_main: Callable[..., int] | None = None,
) -> Runner:
    """A `Runner` for `evals.run --repeat`. Preflight runs here, before the first run.

    `model_factory`, `sandbox_factory` and `agent_main` are injection points for tests; by
    default the CLI builds the Anthropic client itself and the sandbox is the CLI default.
    """
    if budget_usd <= 0 or max_cost_usd <= 0 or max_steps < 1:
        raise ValueError("--budget-usd, --max-cost-usd and --max-steps must be positive")
    preflight_check()
    if agent_main is None:
        from src.agent.run import main as agent_main

    def runner(target: str, out: Path) -> None:
        target_dir = frozen.TARGETS_DIR / target
        if not target_dir.is_dir():
            raise RunFailed(f"no such target: {target}")
        if root.resolve() not in out.resolve().parents:
            raise RunFailed(f"{out} is outside {root}; its cost would not be in the ledger")
        check_budget(
            update_ledger(root, max_cost_usd),
            per_run_cap_usd=max_cost_usd,
            budget_usd=budget_usd,
        )
        argv = [
            "--target",
            str(target_dir),
            "--out",
            str(out),
            "--model",
            MODEL,
            "--max-cost-usd",
            str(max_cost_usd),
            "--max-steps",
            str(max_steps),
        ]
        kwargs: dict = {}
        if model_factory:
            kwargs["model"] = model_factory()
        if sandbox_factory:
            kwargs["sandbox_factory"] = sandbox_factory
        try:
            code = agent_main(argv, **kwargs)
        finally:  # a run that dies still spent money
            spend = update_ledger(root, max_cost_usd)
            print(
                f"spend: ${spend.total_usd:.4f} of ${budget_usd:.2f} "
                f"({len(spend.runs)} run(s) in {root / SPEND_FILE})"
            )
        _check_exit(code, out)

    return runner


def _check_exit(code: int, out: Path) -> None:
    if code == 0:
        return
    meta_path = out.with_name(f"{out.stem}.meta.json")
    try:
        stop = StopReason(json.loads(meta_path.read_text())["stop_reason"])
    except (OSError, ValueError, KeyError):
        raise RunFailed(f"agent exited {code} and left no readable {meta_path.name}") from None
    if code == 1 and stop not in ERROR_STOPS:
        return  # capped or refused: a result, recorded as its stop reason
    raise RunFailed(f"agent exited {code} (stop_reason={stop.value}); see {meta_path}")
