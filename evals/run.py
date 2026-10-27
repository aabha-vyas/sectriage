"""Eval entry point.

Score a findings file:  `python -m evals.run --summary [--findings PATH] [--target NAME]`
Save one run:           `python -m evals.run --findings F.json --save [PATH] [--baseline B.json]`
Repeat live runs:       `python -m evals.run --target T --repeat N --save` (runner from evals.live)
Gate a saved summary:   `python -m evals.run --summary-file SUMMARY.json --baseline BASELINE.json`
Promote to baseline:    `python -m evals.run --summary-file SUMMARY.json --update-baseline [PATH]`

Exit codes:
  0  ok (per-class recall warnings do not change this)
  1  regression against the baseline
  2  usage or I/O error
  3  not comparable (model, PROMPT_VERSION, sandbox, tools, max_steps, target or schema differ)
"""

import argparse
import json
import re
import sys
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path

from evals.baseline import compare
from evals.results import (
    DEFAULT_MAX_STEPS,
    RESULTS_DIR,
    Stat,
    Summary,
    load_baseline,
    load_summary,
    save,
)
from evals.scorer import Score, load_ground_truth, score
from evals.summary import BASELINES_DIR, build_summary, default_save_path, to_baseline
from src.contracts import Finding

# Produces one run's artifacts: findings at `out`, plus `.meta.json` and `.events.jsonl` beside it.
Runner = Callable[[str, Path], None]

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2
EXIT_NOT_COMPARABLE = 3

DEFAULT_TARGET = "vuln_app"
TARGET_RE = re.compile(r"^[a-z0-9_]+$")
DEFAULT_RUNS_DIR = Path("logs/evals")  # gitignored; raw per-run artifacts for --repeat
AUTO = Path("")  # sentinel: `--save` / `--update-baseline` given without a path
MAX_REPEAT = 20


def _fmt(x: float | None) -> str:
    return "n/a" if x is None else f"{x:.2f}"


def render(target: str, s: Score) -> str:
    rows = [
        ("target", target),
        ("tp / fp / fn", f"{s.tp} / {s.fp} / {s.fn}"),
        ("precision", _fmt(s.precision)),
        ("recall", _fmt(s.recall)),
        ("f1", _fmt(s.f1)),
        *((f"recall[{c}]", _fmt(r)) for c, r in s.recall_by_class.items()),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


def _fmt_stat(s: Stat | None, digits: int = 2) -> str:
    return "n/a" if s is None else f"{s.mean:.{digits}f} +/- {s.stdev:.{digits}f}"


def render_summary(summary: Summary) -> str:
    a = summary.aggregate
    rows = [
        ("target", f"{summary.target}{' (tuned)' if summary.tuned else ''}"),
        ("model", summary.model),
        ("prompt_version", summary.prompt_version),
        ("sandbox / tools", f"{summary.sandbox} / {','.join(summary.tools)}"),
        ("max_steps", "n/a" if summary.max_steps is None else str(summary.max_steps)),
        ("runs (clean)", f"{summary.n_runs} ({a.clean_runs})"),
        ("precision", _fmt_stat(a.precision)),
        ("recall", _fmt_stat(a.recall)),
        ("f1", _fmt_stat(a.f1)),
        ("fp / run", _fmt_stat(a.fp)),
        ("fp-free runs", f"{a.fp_free_runs}/{summary.n_runs}"),
        ("cost usd", _fmt_stat(a.cost_usd, 4)),
        ("cost / finding usd", _fmt_stat(a.cost_per_finding_usd, 4)),
        ("latency call p50 s", _fmt_stat(a.latency_call_p50_s)),
        ("latency call p95 s", _fmt_stat(a.latency_call_p95_s)),
        ("latency wall s", _fmt_stat(a.latency_wall_s)),
        ("patch pass rate", "n/a"),
        *((f"recall[{c}]", f"{r.mean:.2f}") for c, r in a.recall_by_class.items()),
    ]
    width = max(len(k) for k, _ in rows)
    return "\n".join(f"{k:<{width}}  {v}" for k, v in rows)


def _gate(summary_file: Path, baseline_file: Path | None) -> int:
    try:
        summary = load_summary(summary_file)
    except (OSError, ValueError) as e:
        return _fail(f"bad --summary-file {summary_file}: {_first_line(e)}")
    return _show_and_gate(summary, baseline_file)


def _show_and_gate(summary: Summary, baseline_file: Path | None) -> int:
    print(render_summary(summary))
    if baseline_file is None:
        return EXIT_OK
    try:
        baseline = load_baseline(baseline_file)
    except (OSError, ValueError) as e:
        return _fail(f"bad --baseline {baseline_file}: {_first_line(e)}")

    result = compare(summary, baseline)
    for line in result.not_comparable:
        print(f"not comparable: {line}")
    for line in result.regressions:
        print(f"regression: {line}")
    for line in result.warnings:
        print(f"warning: {line}")
    if result.not_comparable:
        print("gate: NOT COMPARABLE")
        return EXIT_NOT_COMPARABLE
    if result.regressions:
        print(f"gate: FAIL ({len(result.regressions)} regression(s))")
        return EXIT_REGRESSION
    print("gate: PASS")
    return EXIT_OK


def _update_baseline(summary_file: Path, dest: Path, baselines_dir: Path) -> int:
    try:
        summary = load_summary(summary_file)
        baseline = to_baseline(summary)
    except (OSError, ValueError) as e:
        return _fail(f"cannot promote {summary_file}: {_first_line(e)}")
    path = baselines_dir / f"{summary.target}.json" if dest == AUTO else dest
    save(baseline, path)
    print(f"baseline written: {path}")
    return EXIT_OK


def _first_line(e: Exception) -> str:
    return str(e).splitlines()[0] if str(e) else type(e).__name__


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return EXIT_ERROR


def _load_findings(path: Path) -> list[Finding]:
    raw = json.loads(path.read_text())
    if not isinstance(raw, list):
        raise ValueError("expected a JSON list of findings")
    return [Finding.model_validate(f) for f in raw]


def _default_runner(args: argparse.Namespace, runs_dir: Path) -> Runner:
    """The live runner: preflight now, spend ledger under `runs_dir` for every run."""
    from evals import live  # lazy: pulls in the agent and the Anthropic SDK

    return live.make_runner(
        budget_usd=args.budget_usd,
        max_cost_usd=args.max_cost_usd,
        max_steps=args.max_steps,
        root=runs_dir,
    )


def _repeat(
    target: str, n: int, runner: Runner, runs_dir: Path, now: datetime
) -> tuple[list[Path], int]:
    """Run the agent n times; returns the findings paths and an error exit (or EXIT_OK)."""
    batch = runs_dir / f"{target}-{now:%Y%m%dT%H%M%SZ}"
    paths: list[Path] = []
    for k in range(1, n + 1):
        out = batch / f"run{k}" / "findings.json"
        try:
            runner(target, out)
        except Exception as e:  # a failed run must not hide the earlier ones' artifacts
            done = f"; {len(paths)} finished run(s) kept in {batch}" if paths else ""
            return paths, _fail(f"run {k}/{n} failed: {type(e).__name__}: {_first_line(e)}{done}")
        paths.append(out)
    return paths, EXIT_OK


def main(
    argv: list[str] | None = None,
    *,
    runner: Runner | None = None,
    results_dir: Path = RESULTS_DIR,
    baselines_dir: Path = BASELINES_DIR,
    runs_dir: Path = DEFAULT_RUNS_DIR,
) -> int:
    """`runner` and the three dirs are injection points for tests."""
    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--summary", action="store_true", help="print the summary table")
    parser.add_argument("--findings", type=Path, help="JSON list of findings to score")
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--summary-file", type=Path, help="saved summary JSON to show and gate")
    parser.add_argument("--baseline", type=Path, help="baseline JSON to gate the summary against")
    parser.add_argument(
        "--save",
        nargs="?",
        const=AUTO,
        type=Path,
        help="save the summary JSON (default: evals/results/<target>-<time>.json)",
    )
    parser.add_argument(
        "--repeat", type=int, metavar="N", help=f"run the agent N times (1-{MAX_REPEAT})"
    )
    parser.add_argument(
        "--budget-usd",
        type=float,
        default=2.00,
        help="--repeat: total spend cap across all runs in the runs dir (default: 2.00)",
    )
    parser.add_argument(
        "--max-cost-usd",
        type=float,
        default=0.50,
        help="--repeat: cap per run (default: 0.50)",
    )
    parser.add_argument(
        "--max-steps",
        type=int,
        default=DEFAULT_MAX_STEPS,
        help=f"--repeat: step budget per run (default: {DEFAULT_MAX_STEPS}); a setup field, so "
        "summaries with different values are not comparable",
    )
    parser.add_argument(
        "--update-baseline",
        nargs="?",
        const=AUTO,
        type=Path,
        help="promote --summary-file to a baseline (default: evals/baselines/<target>.json)",
    )
    args = parser.parse_args(argv)

    if args.summary_file:
        if args.findings or args.repeat is not None or args.save:
            return _fail("--summary-file cannot be combined with --findings, --repeat or --save")
        if args.update_baseline:
            if args.baseline:
                return _fail("--update-baseline cannot be combined with --baseline")
            return _update_baseline(args.summary_file, args.update_baseline, baselines_dir)
        return _gate(args.summary_file, args.baseline)
    if args.update_baseline:
        return _fail("--update-baseline requires --summary-file")

    if not TARGET_RE.fullmatch(args.target):
        return _fail(f"invalid --target {args.target!r}: must match {TARGET_RE.pattern}")

    if args.repeat is not None or args.save or args.baseline:
        return _run_to_summary(args, runner, results_dir, runs_dir)

    findings: list[Finding] = []
    if args.findings:
        try:
            findings = _load_findings(args.findings)
        except (OSError, ValueError) as e:  # ValidationError and JSONDecodeError are ValueErrors
            return _fail(f"bad --findings {args.findings}: {_first_line(e)}")

    try:
        truth = load_ground_truth(args.target)
    except (OSError, ValueError) as e:
        return _fail(f"cannot load ground truth for {args.target!r}: {type(e).__name__}")

    print(render(args.target, score(findings, truth)))
    return EXIT_OK


def _run_to_summary(
    args: argparse.Namespace, runner: Runner | None, results_dir: Path, runs_dir: Path
) -> int:
    """Build a Summary from one findings file or N agent runs, then save and/or gate it."""
    if args.repeat is not None and args.findings:
        return _fail("--repeat and --findings are mutually exclusive")
    if args.repeat is None and not args.findings:
        return _fail("--save and --baseline need --findings or --repeat (or use --summary-file)")
    now = datetime.now(UTC)
    if args.repeat is not None:
        if not 1 <= args.repeat <= MAX_REPEAT:
            return _fail(f"--repeat must be between 1 and {MAX_REPEAT}")
        if runner is None:
            try:
                runner = _default_runner(args, runs_dir)
            except (RuntimeError, ValueError) as e:  # PreflightError or a bad cap
                return _fail(str(e))
        paths, code = _repeat(args.target, args.repeat, runner, runs_dir, now)
        if code != EXIT_OK:
            return code
    else:
        paths = [args.findings]
    try:
        summary = build_summary(args.target, paths, now=now)
    except (OSError, ValueError, KeyError) as e:
        return _fail(f"cannot summarize runs: {type(e).__name__}: {_first_line(e)}")
    if args.save:
        path = default_save_path(summary, results_dir) if args.save == AUTO else args.save
        save(summary, path)
        print(f"saved: {path}")
    return _show_and_gate(summary, args.baseline)


if __name__ == "__main__":
    raise SystemExit(main())
