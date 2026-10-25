"""Turns one agent run (meta.json + events.jsonl + a Score) into a RunSummary."""

import json
from pathlib import Path

from evals.results import Latency, RunSummary, Tokens
from evals.scorer import Score
from src.agent.events import read_events
from src.contracts import Event


def percentile(values: list[float], p: float) -> float | None:
    """Linear-interpolated percentile, p in [0, 100]; None for an empty list."""
    if not values:
        return None
    xs = sorted(values)
    k = (len(xs) - 1) * p / 100
    lo = int(k)
    hi = min(lo + 1, len(xs) - 1)
    return xs[lo] + (xs[hi] - xs[lo]) * (k - lo)


def call_latencies_s(events: list[Event]) -> list[float]:
    """One latency per model call: events that carry a model response."""
    return [e.latency_ms / 1000 for e in events if e.model_output is not None]


def tokens_from(meta: dict, events: list[Event]) -> Tokens:
    """Totals from meta.json when the agent wrote them, else only the exact output total.

    Event `tokens_in` sums input and cache tokens, so it is not reported as `input`.
    """
    given = meta.get("tokens")
    if isinstance(given, dict):
        return Tokens(**{k: given.get(k) for k in Tokens.model_fields})
    return Tokens(output=sum(e.tokens_out for e in events))


def cost_per_finding(cost_usd: float, tp: int) -> float | None:
    return cost_usd / tp if tp else None


def run_summary(meta: dict, events: list[Event], score: Score, wall_s: float) -> RunSummary:
    lat = call_latencies_s(events)
    cost = float(meta.get("cost_usd", 0.0))
    return RunSummary(
        task_id=meta["task_id"],
        stop_reason=meta["stop_reason"],
        steps=int(meta["steps"]),
        tp=score.tp,
        fp=score.fp,
        fn=score.fn,
        precision=score.precision,
        recall=score.recall,
        f1=score.f1,
        recall_by_class=score.recall_by_class,
        cost_usd=cost,
        cost_per_finding_usd=cost_per_finding(cost, score.tp),
        tokens=tokens_from(meta, events),
        latency_s=Latency(wall=wall_s, call_p50=percentile(lat, 50), call_p95=percentile(lat, 95)),
        error=meta.get("error"),
    )


def load_run(meta_path: Path, events_path: Path, score: Score, wall_s: float) -> RunSummary:
    meta = json.loads(meta_path.read_text())
    return run_summary(meta, read_events(events_path), score, wall_s)
