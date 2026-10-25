"""Eval entry point.

Score a findings file:  `python -m evals.run --summary [--findings PATH] [--target NAME]`
Gate a saved summary:   `python -m evals.run --summary-file SUMMARY.json --baseline BASELINE.json`

Exit codes:
  0  ok (per-class recall warnings do not change this)
  1  regression against the baseline
  2  usage or I/O error
  3  not comparable (model, PROMPT_VERSION, sandbox, tools, target or schema differ)
"""

import argparse
import json
import re
import sys
from pathlib import Path

from evals.baseline import compare
from evals.results import Stat, Summary, load_baseline, load_summary
from evals.scorer import Score, load_ground_truth, score
from src.contracts import Finding

EXIT_OK = 0
EXIT_REGRESSION = 1
EXIT_ERROR = 2
EXIT_NOT_COMPARABLE = 3

DEFAULT_TARGET = "vuln_app"
TARGET_RE = re.compile(r"^[a-z0-9_]+$")


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
        ("runs (clean)", f"{summary.n_runs} ({a.clean_runs})"),
        ("precision", _fmt_stat(a.precision)),
        ("recall", _fmt_stat(a.recall)),
        ("f1", _fmt_stat(a.f1)),
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="evals.run")
    parser.add_argument("--summary", action="store_true", help="print the summary table")
    parser.add_argument("--findings", type=Path, help="JSON list of findings to score")
    parser.add_argument("--target", default=DEFAULT_TARGET)
    parser.add_argument("--summary-file", type=Path, help="saved summary JSON to show and gate")
    parser.add_argument(
        "--baseline", type=Path, help="baseline JSON to gate --summary-file against"
    )
    args = parser.parse_args(argv)

    if args.summary_file:
        if args.findings:
            return _fail("--summary-file cannot be combined with --findings")
        return _gate(args.summary_file, args.baseline)
    if args.baseline:
        return _fail("--baseline requires --summary-file")

    if not TARGET_RE.fullmatch(args.target):
        return _fail(f"invalid --target {args.target!r}: must match {TARGET_RE.pattern}")

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


if __name__ == "__main__":
    raise SystemExit(main())
