import pytest

from evals.metrics import cost_per_finding, percentile, run_summary, tokens_from
from evals.scorer import Score
from tests.evals.builders import make_event

META = {"task_id": "t-1", "stop_reason": "finish", "steps": 3, "cost_usd": 0.02}


def _score(tp: int = 2) -> Score:
    return Score(
        tp=tp, fp=0, fn=0, precision=1.0, recall=1.0, f1=1.0, recall_by_class={"sqli": 1.0}
    )


def test_percentile_interpolates_and_handles_empty():
    assert percentile([], 50) is None
    assert percentile([4.0], 95) == 4.0
    assert percentile([1.0, 2.0, 3.0, 4.0], 50) == pytest.approx(2.5)
    assert percentile([1.0, 2.0, 3.0, 4.0], 95) == pytest.approx(3.85)


def test_cost_per_finding_is_null_without_true_positives():
    assert cost_per_finding(0.02, 0) is None
    assert cost_per_finding(0.02, 4) == pytest.approx(0.005)


def test_latency_counts_only_events_with_a_model_response():
    events = [
        make_event(latency_ms=1000),
        make_event(latency_ms=3000),
        make_event(model_output=None, latency_ms=0),
    ]
    run = run_summary(META, events, _score(), wall_s=7.5)
    assert run.latency_s.wall == 7.5
    assert run.latency_s.call_p50 == pytest.approx(2.0)


def test_no_model_calls_gives_null_latency_percentiles():
    run = run_summary(META, [make_event(model_output=None)], _score(), wall_s=1.0)
    assert run.latency_s.call_p50 is None and run.latency_s.call_p95 is None


def test_tokens_cache_fields_null_without_meta_totals():
    t = tokens_from(META, [make_event(tokens_out=10), make_event(tokens_out=5)])
    assert t.model_dump() == {"input": None, "output": 15, "cache_read": None, "cache_write": None}


def test_tokens_read_from_meta_when_present():
    meta = {**META, "tokens": {"input": 100, "output": 20, "cache_read": 300, "cache_write": 50}}
    t = tokens_from(meta, [make_event(tokens_out=999)])
    assert t.model_dump() == {"input": 100, "output": 20, "cache_read": 300, "cache_write": 50}


def test_run_summary_zero_tp_has_null_cost_per_finding():
    run = run_summary(META, [], _score(tp=0), wall_s=1.0)
    assert run.cost_per_finding_usd is None
    assert run.cost_usd == 0.02
