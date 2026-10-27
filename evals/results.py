"""Saved eval summaries: the on-disk schema, aggregation across repeats, atomic save/load."""

import json
import os
import statistics
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = 2
DEFAULT_MAX_STEPS = 30  # the agent CLI's own default; it is a setup field of every summary
RESULTS_DIR = Path(__file__).parent / "results"


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid")


class Stat(_Model):
    mean: float
    stdev: float
    min: float
    max: float


class ClassStat(_Model):
    mean: float
    min: float


class Tokens(_Model):
    input: int | None = None
    output: int | None = None
    cache_read: int | None = None
    cache_write: int | None = None


class Latency(_Model):
    wall: float
    call_p50: float | None = None
    call_p95: float | None = None


class RunSummary(_Model):
    task_id: str
    stop_reason: str
    steps: int
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None
    f1: float | None
    recall_by_class: dict[str, float | None]
    cost_usd: float
    cost_per_finding_usd: float | None
    tokens: Tokens
    latency_s: Latency
    error: str | None = None


class Aggregate(_Model):
    precision: Stat | None
    recall: Stat | None
    f1: Stat | None
    fp: Stat | None
    cost_usd: Stat | None
    cost_per_finding_usd: Stat | None
    latency_call_p50_s: Stat | None
    latency_call_p95_s: Stat | None
    latency_wall_s: Stat | None
    steps: Stat | None
    recall_by_class: dict[str, ClassStat]
    patch_pass_rate: None = None  # not measured in this lane; always null
    clean_runs: int  # runs whose stop reason is clean (finish / max_steps)
    fp_free_runs: int  # runs with zero false positives


class Tolerances(_Model):
    precision: float = 0.10
    recall: float = 0.15
    recall_by_class: float = 0.10
    cost_rel: float = 0.25
    cost_per_finding_rel: float = 0.25


class Summary(_Model):
    schema_version: int = SCHEMA_VERSION
    created_at: str
    git_sha: str | None = None
    target: str
    n_truth: int | None = None  # ground-truth finding count when scored; 0 = clean target
    tuned: bool = False
    model: str
    prompt_version: str
    price_verified_on: str | None = None
    sandbox: str
    tools: list[str]
    max_cost_usd: float
    max_steps: int | None = None  # step budget per run; None = not recorded
    n_runs: int
    runs: list[RunSummary]
    aggregate: Aggregate


class Baseline(Summary):
    tolerances: Tolerances = Tolerances()


def stat(values: list[float | None]) -> Stat | None:
    """Mean / sample stdev / min / max over the non-null values; None if there are none."""
    xs = [v for v in values if v is not None]
    if not xs:
        return None
    return Stat(
        mean=statistics.fmean(xs),
        stdev=statistics.stdev(xs) if len(xs) > 1 else 0.0,
        min=min(xs),
        max=max(xs),
    )


def aggregate(runs: list[RunSummary], clean_stops: set[str]) -> Aggregate:
    classes = sorted({c for r in runs for c in r.recall_by_class})
    by_class: dict[str, ClassStat] = {}
    for c in classes:
        xs = [r.recall_by_class[c] for r in runs if r.recall_by_class.get(c) is not None]
        if xs:
            by_class[c] = ClassStat(mean=statistics.fmean(xs), min=min(xs))
    return Aggregate(
        precision=stat([r.precision for r in runs]),
        recall=stat([r.recall for r in runs]),
        f1=stat([r.f1 for r in runs]),
        fp=stat([float(r.fp) for r in runs]),
        cost_usd=stat([r.cost_usd for r in runs]),
        cost_per_finding_usd=stat([r.cost_per_finding_usd for r in runs]),
        latency_call_p50_s=stat([r.latency_s.call_p50 for r in runs]),
        latency_call_p95_s=stat([r.latency_s.call_p95 for r in runs]),
        latency_wall_s=stat([r.latency_s.wall for r in runs]),
        steps=stat([float(r.steps) for r in runs]),
        recall_by_class=by_class,
        clean_runs=sum(r.stop_reason in clean_stops for r in runs),
        fp_free_runs=sum(r.fp == 0 for r in runs),
    )


def atomic_write(path: Path, text: str) -> None:
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


def save(obj: Summary, path: Path) -> None:
    atomic_write(path, json.dumps(obj.model_dump(mode="json"), indent=2) + "\n")


def load_summary(path: Path) -> Summary:
    return Summary.model_validate(json.loads(path.read_text()))


def load_baseline(path: Path) -> Baseline:
    return Baseline.model_validate(json.loads(path.read_text()))
