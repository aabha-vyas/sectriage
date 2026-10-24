"""Eval entry point: `python -m evals.run --summary [--findings PATH]`."""

import argparse
import json
import re
import sys
from pathlib import Path

from evals.scorer import Score, load_ground_truth, score
from src.contracts import Finding

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


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


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
    args = parser.parse_args(argv)

    if not TARGET_RE.fullmatch(args.target):
        return _fail(f"invalid --target {args.target!r}: must match {TARGET_RE.pattern}")

    findings: list[Finding] = []
    if args.findings:
        try:
            findings = _load_findings(args.findings)
        except (OSError, ValueError) as e:  # ValidationError and JSONDecodeError are ValueErrors
            first = str(e).splitlines()[0] if str(e) else type(e).__name__
            return _fail(f"bad --findings {args.findings}: {first}")

    try:
        truth = load_ground_truth(args.target)
    except (OSError, ValueError) as e:
        return _fail(f"cannot load ground truth for {args.target!r}: {type(e).__name__}")

    print(render(args.target, score(findings, truth)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
