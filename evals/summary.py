"""Builds a saved Summary from agent run artifacts (findings.json, .meta.json, .events.jsonl)."""

import json
import subprocess
from datetime import UTC, datetime
from pathlib import Path

from evals.metrics import load_run
from evals.results import Baseline, RunSummary, Summary, aggregate
from evals.scorer import load_ground_truth, score
from src.agent.events import read_events
from src.contracts import Finding

# The CLI runs without a sandbox factory, so only the host read-only tools work.
SANDBOX = "none"
TOOLS = ["read_file", "search_code"]
CLEAN_STOPS = {"finish", "max_steps"}
# The prompt was developed against these; their numbers never go on a resume.
# ledger_api: the prompt v2 experiment was developed on its v1 sweep (tag prompt-v2-experiment)
TUNED_TARGETS = {"vuln_app", "ledger_api"}
BASELINES_DIR = Path(__file__).parent / "baselines"

# Fields that must agree across the runs of one summary.
_SAME_ACROSS_RUNS = (
    "model",
    "prompt_version",
    "price_verified_on",
    "max_cost_usd",
    "max_steps",
)


def sidecar(findings_path: Path, suffix: str) -> Path:
    """`f.json` -> `f.<suffix>`, matching the paths src.agent.run writes."""
    return findings_path.with_name(f"{findings_path.stem}.{suffix}")


def wall_seconds(events_path: Path) -> float:
    events = read_events(events_path)
    if len(events) < 2:
        return 0.0
    return (max(e.ts for e in events) - min(e.ts for e in events)).total_seconds()


def score_run(target: str, findings_path: Path) -> tuple[RunSummary, dict]:
    """Score one run's findings file; returns its RunSummary and raw meta."""
    raw = json.loads(findings_path.read_text())
    if not isinstance(raw, list):
        raise ValueError("expected a JSON list of findings")
    findings = [Finding.model_validate(f) for f in raw]
    meta_path, events_path = (
        sidecar(findings_path, "meta.json"),
        sidecar(findings_path, "events.jsonl"),
    )
    meta = json.loads(meta_path.read_text())
    result = score(findings, load_ground_truth(target))
    return load_run(meta_path, events_path, result, wall_seconds(events_path)), meta


def git_sha() -> str | None:
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.SubprocessError):
        return None
    if out.returncode != 0:
        return None
    return out.stdout.strip() or None


def build_summary(
    target: str, findings_paths: list[Path], *, now: datetime | None = None
) -> Summary:
    if not findings_paths:
        raise ValueError("no runs to summarize")
    n_truth = len(load_ground_truth(target).findings)
    scored = [score_run(target, p) for p in findings_paths]
    runs, metas = [r for r, _ in scored], [m for _, m in scored]
    for name in _SAME_ACROSS_RUNS:
        values = {json.dumps(m.get(name)) for m in metas}
        if len(values) > 1:
            raise ValueError(f"runs disagree on {name}: {sorted(values)}")
    meta = metas[0]
    return Summary(
        created_at=(now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        git_sha=git_sha(),
        target=target,
        n_truth=n_truth,
        tuned=target in TUNED_TARGETS,
        model=meta["model"],
        prompt_version=meta["prompt_version"],
        price_verified_on=meta.get("price_verified_on"),
        sandbox=SANDBOX,
        tools=TOOLS,
        max_cost_usd=float(meta["max_cost_usd"]),
        max_steps=meta.get("max_steps"),
        n_runs=len(runs),
        runs=runs,
        aggregate=aggregate(runs, CLEAN_STOPS),
    )


def default_save_path(summary: Summary, results_dir: Path) -> Path:
    stamp = summary.created_at.replace("-", "").replace(":", "")
    return results_dir / f"{summary.target}-{stamp}.json"


def to_baseline(summary: Summary) -> Baseline:
    """Promote a summary to a baseline. Refuses runs that did not stop cleanly."""
    if summary.aggregate.clean_runs < summary.n_runs:
        raise ValueError(
            f"only {summary.aggregate.clean_runs}/{summary.n_runs} runs stopped cleanly"
        )
    return Baseline(**summary.model_dump())
