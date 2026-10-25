"""CLI: `python -m src.agent.run --target evals/targets/vuln_app --out findings.json`."""

import argparse
import json
import logging
import os
import sys
import tempfile
import uuid
from collections.abc import Callable
from pathlib import Path

from src.agent.events import EventLog
from src.agent.loop import DEFAULT_MAX_COST_USD, REPO_ROOT, RunResult, StopReason, run_task
from src.agent.model import DEFAULT_MODEL, AnthropicModelClient, ModelClient
from src.agent.pricing import VERIFIED_ON, UnknownModelError
from src.agent.prompts import PROMPT_VERSION
from src.agent.sandbox_protocol import SandboxFactory
from src.contracts import Task

EXIT_INTERRUPTED = 130
TARGETS_DIR = REPO_ROOT / "evals" / "targets"
GOAL = (
    "Review the target for security vulnerabilities. Report every vulnerability you verified in "
    "the code with report_finding, then call finish."
)


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def _check_target(raw: Path) -> Path:
    """Resolve `raw` and require a directory strictly inside `evals/targets/`."""
    try:
        target = raw.resolve(strict=True)
    except OSError:
        raise ValueError(f"target {raw} does not exist") from None
    allowed = TARGETS_DIR.resolve()
    if target == allowed or not target.is_relative_to(allowed) or not target.is_dir():
        raise ValueError(f"target must be a directory inside {TARGETS_DIR.relative_to(REPO_ROOT)}/")
    return target


def _check_output(path: Path) -> None:
    """Outputs must not land in the (read-only, scanned) targets tree."""
    if path.resolve().is_relative_to(TARGETS_DIR.resolve()):
        raise ValueError(
            f"output path {path} must not be inside {TARGETS_DIR.relative_to(REPO_ROOT)}/"
        )


def _atomic_write(path: Path, text: str) -> None:
    """Write `text` to a temp file next to `path`, then `os.replace` it into place."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_outputs(out: Path, meta_path: Path, task: Task, result: RunResult, max_cost: float):
    findings = [f.model_dump(mode="json") for f in result.findings]
    _atomic_write(out, json.dumps(findings, indent=2) + "\n")
    meta = {
        "task_id": task.task_id,
        "model": task.model,
        "prompt_version": PROMPT_VERSION,
        "price_verified_on": VERIFIED_ON,
        "stop_reason": result.stop_reason.value,
        "steps": result.steps,
        "findings": len(findings),
        "cost_usd": round(result.cost_usd, 6),
        "max_steps": task.max_steps,
        "timeout_s": task.timeout_s,
        "max_cost_usd": max_cost,
        "error": result.error,
    }
    _atomic_write(meta_path, json.dumps(meta, indent=2) + "\n")


def main(
    argv: list[str] | None = None,
    *,
    model: ModelClient | None = None,
    sandbox_factory: SandboxFactory | None = None,
    clock: Callable[[], float] | None = None,
) -> int:
    """`model`, `sandbox_factory` and `clock` are injection points for tests."""
    parser = argparse.ArgumentParser(prog="python -m src.agent.run")
    parser.add_argument("--target", type=Path, required=True, help="a dir under evals/targets/")
    parser.add_argument("--out", type=Path, default=Path("findings.json"))
    parser.add_argument("--events", type=Path, help="default: <out stem>.events.jsonl")
    parser.add_argument("--max-steps", type=int, default=30)
    parser.add_argument("--timeout-s", type=int, default=600)
    parser.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument(
        "--temperature",
        type=float,
        default=0.0,
        help="accepted and ignored (never sent to the model); logs a warning if non-zero",
    )
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")

    try:
        target = _check_target(args.target)
    except ValueError as e:
        return _fail(str(e))
    out = args.out
    events_path = args.events or out.with_name(f"{out.stem}.events.jsonl")
    meta_path = out.with_name(f"{out.stem}.meta.json")
    try:
        for path in (out, events_path, meta_path):
            _check_output(path)
    except ValueError as e:
        return _fail(str(e))
    if args.max_steps < 1 or args.timeout_s < 1 or args.max_cost_usd <= 0:
        return _fail("--max-steps, --timeout-s and --max-cost-usd must be positive")

    if model is None:
        try:
            model = AnthropicModelClient(args.model)
        except UnknownModelError as e:
            return _fail(str(e))

    events_path.unlink(missing_ok=True)  # EventLog appends; each run starts a fresh log

    task = Task(
        task_id=f"{target.name}-{uuid.uuid4().hex[:8]}",
        target_path=str(target),
        goal=GOAL,
        max_steps=args.max_steps,
        timeout_s=args.timeout_s,
        model=model.model,
        temperature=args.temperature,
    )
    kwargs = {"clock": clock} if clock else {}
    with EventLog(events_path) as events:
        result = run_task(
            task,
            model,
            events,
            sandbox_factory=sandbox_factory,
            max_cost_usd=args.max_cost_usd,
            **kwargs,
        )
    _write_outputs(out, meta_path, task, result, args.max_cost_usd)

    print(
        f"stop={result.stop_reason.value} findings={len(result.findings)} steps={result.steps} "
        f"cost=${result.cost_usd:.4f} -> {out}"
    )
    if result.stop_reason is StopReason.INTERRUPTED:
        return EXIT_INTERRUPTED
    return 0 if result.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())
