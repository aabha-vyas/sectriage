"""Small constructors for eval summaries used across tests/evals."""

from datetime import UTC, datetime

from evals.results import Baseline, Latency, RunSummary, Summary, Tokens, aggregate
from src.agent.provenance import Provenance
from src.contracts import Event

CLEAN = {"finish", "max_steps"}


def make_provenance(**overrides) -> Provenance:
    fields = {
        "model": "m",
        "prompt_version": "p1",
        "system_prompt_sha256": "a" * 64,
        "tool_schemas_sha256": "b" * 64,
        "tool_names": ["read_file", "search_code", "report_finding", "finish"],
        "effort": "medium",
        "max_tokens": 16000,
        "caching": True,
        "git_commit": "c" * 40,
        "git_dirty": False,
        "target_sha256": "d" * 64,
        "target_lock_sha256": None,
        "price_table_version": "e" * 64,
        "price_verified_on": "2026-10-24",
        "sandbox_mode": "none",
        "max_steps": 30,
        "python_version": "3.12.11",
        "anthropic_sdk_version": "2.3.0",
        "started_at": "2026-10-25T12:00:00Z",
        "finished_at": "2026-10-25T12:00:10Z",
    }
    fields.update(overrides)
    return Provenance(**fields)


def make_run(
    *,
    precision: float | None = 1.0,
    recall: float | None = 1.0,
    cost: float = 0.01,
    by_class: dict[str, float | None] | None = None,
    stop: str = "finish",
    wall: float = 5.0,
    fp: int = 0,
) -> RunSummary:
    return RunSummary(
        task_id="t-1",
        stop_reason=stop,
        steps=8,
        tp=5,
        fp=fp,
        fn=0,
        precision=precision,
        recall=recall,
        f1=None,
        recall_by_class=by_class if by_class is not None else {"sqli": 1.0, "xss": 1.0},
        cost_usd=cost,
        cost_per_finding_usd=cost / 5,
        tokens=Tokens(),
        latency_s=Latency(wall=wall, call_p50=1.0, call_p95=2.0),
        provenance=make_provenance(),
    )


def make_summary(runs: list[RunSummary] | None = None, **overrides) -> Summary:
    runs = runs if runs is not None else [make_run(), make_run()]
    fields = {
        "created_at": "2026-10-25T12:00:00Z",
        "target": "t",
        "model": "m",
        "prompt_version": "p1",
        "sandbox": "none",
        "tools": ["read_file", "search_code"],
        "max_cost_usd": 0.5,
        "max_steps": 30,
        "n_runs": len(runs),
        "runs": runs,
        "aggregate": aggregate(runs, CLEAN),
        "provenance": make_provenance(),
    }
    fields.update(overrides)
    return Summary(**fields)


def make_baseline(summary: Summary | None = None, **overrides) -> Baseline:
    base = summary or make_summary()
    return Baseline(**{**base.model_dump(), **overrides})


def make_event(*, model_output: str | None = "[]", latency_ms: int = 0, tokens_out: int = 0):
    return Event(
        task_id="t-1",
        step=1,
        ts=datetime.now(UTC),
        model_output=model_output,
        latency_ms=latency_ms,
        tokens_out=tokens_out,
    )


def write_run(
    out,
    findings,
    *,
    stop: str = "finish",
    cost: float = 0.01,
    model: str = "m",
    prompt_version: str = "p1",
    seconds: int = 10,
    max_steps: int = 30,
    provenance: Provenance | None = None,
) -> None:
    """Write findings.json plus the .meta.json and .events.jsonl that src.agent.run writes."""
    import json
    from datetime import timedelta

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([f.model_dump(mode="json") for f in findings]))
    meta = {
        "task_id": "t-1",
        "model": model,
        "prompt_version": prompt_version,
        "price_verified_on": "2026-10-24",
        "stop_reason": stop,
        "steps": 8,
        "cost_usd": cost,
        "max_steps": max_steps,
        "max_cost_usd": 0.5,
        "error": None,
        "provenance": (
            provenance
            or make_provenance(model=model, prompt_version=prompt_version, max_steps=max_steps)
        ).model_dump(mode="json"),
    }
    out.with_name(f"{out.stem}.meta.json").write_text(json.dumps(meta))
    first = make_event(latency_ms=1000, tokens_out=50)
    last = first.model_copy(update={"step": 2, "ts": first.ts + timedelta(seconds=seconds)})
    out.with_name(f"{out.stem}.events.jsonl").write_text(
        first.to_jsonl() + "\n" + last.to_jsonl() + "\n"
    )
