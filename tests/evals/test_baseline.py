import pytest

from evals.baseline import compare
from evals.results import load_baseline
from evals.summary import BASELINES_DIR
from tests.evals.builders import make_baseline, make_run, make_summary


def _runs(n: int = 2, **kw):
    return [make_run(**kw) for _ in range(n)]


def test_identical_summary_has_no_findings():
    s = make_summary()
    c = compare(s, make_baseline(s))
    assert not c.regressions and not c.not_comparable and not c.warnings


def test_drop_within_tolerance_passes():
    base = make_baseline(make_summary(_runs(precision=1.0, recall=1.0, cost=0.010)))
    cur = make_summary(_runs(precision=0.92, recall=0.92, cost=0.012))
    c = compare(cur, base)
    assert not c.regressions and not c.warnings


def test_recall_tolerance_is_wider_than_precision():
    base = make_baseline(make_summary(_runs(precision=1.0, recall=1.0)))
    assert not compare(make_summary(_runs(precision=1.0, recall=0.87)), base).regressions
    c = compare(make_summary(_runs(precision=0.87, recall=1.0)), base)
    assert len(c.regressions) == 1 and "precision" in c.regressions[0]
    c = compare(make_summary(_runs(precision=1.0, recall=0.83)), base)
    assert len(c.regressions) == 1 and "recall:" in c.regressions[0]


@pytest.mark.parametrize(
    ("kw", "needle"),
    [
        ({"precision": 0.8}, "precision"),
        ({"recall": 0.8}, "recall:"),
        ({"cost": 0.0126}, "cost_usd"),
    ],
)
def test_gated_metrics_regress(kw, needle):
    base = make_baseline(make_summary(_runs(cost=0.010)))
    cur = make_summary(_runs(**{"cost": 0.010, **kw}))
    c = compare(cur, base)
    assert not c.not_comparable
    assert any(needle in r for r in c.regressions), c.regressions


def test_cost_per_finding_is_gated_separately():
    base = make_baseline(make_summary(_runs(cost=0.010)))
    cur = make_summary(_runs(cost=0.010))
    for r in cur.runs:
        r.cost_per_finding_usd = 0.0026  # baseline is 0.002; +30%
    cur = make_summary(cur.runs)
    c = compare(cur, base)
    assert [r.split(":")[0] for r in c.regressions] == ["cost_per_finding_usd"]


def test_cost_per_finding_within_tolerance_passes():
    base = make_baseline(make_summary(_runs(cost=0.010)))
    runs = _runs(cost=0.010)
    for r in runs:
        r.cost_per_finding_usd = 0.0024  # +20%
    assert not compare(make_summary(runs), base).regressions


def test_null_cost_against_baseline_cost_is_a_regression():
    base = make_baseline(make_summary())
    runs = _runs()
    for r in runs:
        r.cost_per_finding_usd = None
    c = compare(make_summary(runs), base)
    assert any(r.startswith("cost_per_finding_usd") and "no value" in r for r in c.regressions)


def test_null_cost_baseline_is_not_gated():
    runs = _runs()
    for r in runs:
        r.cost_per_finding_usd = None
    base = make_baseline(make_summary(runs))
    assert not compare(make_summary(), base).regressions


def test_null_precision_against_real_baseline_is_a_regression():
    base = make_baseline(make_summary())
    c = compare(make_summary(_runs(precision=None)), base)
    assert any(r.startswith("precision") for r in c.regressions)


def test_clean_run_rate_drop_is_a_regression_even_with_different_n():
    base = make_baseline(make_summary(_runs(5)))
    cur = make_summary([make_run(), make_run(), make_run(stop="budget_exceeded")])
    c = compare(cur, base)
    assert [r.split(":")[0] for r in c.regressions] == ["clean_runs"]
    assert "2/3" in c.regressions[0] and "5/5" in c.regressions[0]


def test_clean_run_rate_equal_passes_across_different_n():
    base = make_baseline(make_summary(_runs(5)))
    assert not compare(make_summary(_runs(3)), base).regressions


def test_per_class_drop_is_a_warning_not_a_regression():
    base = make_baseline(make_summary(_runs(recall=1.0)))
    cur = make_summary(_runs(recall=0.95, by_class={"sqli": 0.0, "xss": 1.0}))
    c = compare(cur, base)
    assert c.regressions == []
    assert [w.split(":")[0] for w in c.warnings] == ["recall[sqli]"]


def test_missing_class_in_current_is_a_warning():
    base = make_baseline(make_summary())
    c = compare(make_summary(_runs(by_class={"xss": 1.0})), base)
    assert c.regressions == []
    assert any("recall[sqli]" in w and "no value" in w for w in c.warnings)


def test_latency_is_not_gated():
    base = make_baseline(make_summary(_runs(wall=5.0)))
    c = compare(make_summary(_runs(wall=500.0)), base)
    assert not c.regressions and not c.warnings


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("model", "other"),
        ("prompt_version", "p2"),
        ("sandbox", "docker"),
        ("tools", ["read_file", "search_code", "run_command"]),
        ("target", "other_target"),
        ("max_steps", 15),
        ("max_steps", None),
    ],
)
def test_setup_mismatch_is_not_comparable_and_skips_metric_checks(field, value):
    base = make_baseline(make_summary())
    cur = make_summary(_runs(precision=0.0, recall=0.0), **{field: value})
    c = compare(cur, base)
    assert len(c.not_comparable) == 1 and field in c.not_comparable[0]
    assert c.regressions == [] and c.warnings == []


def test_committed_v1_baseline_is_not_comparable_with_a_v2_summary():
    base = load_baseline(BASELINES_DIR / "ledger_api.json")
    assert base.prompt_version == "v1"
    summary = base.model_copy(update={"prompt_version": "v2"})
    c = compare(summary, base)
    assert len(c.not_comparable) == 1 and "prompt_version" in c.not_comparable[0]
    assert not c.regressions and not c.warnings


def test_tuned_flag_does_not_affect_comparability():
    base = make_baseline(make_summary())
    c = compare(make_summary(tuned=True), base)
    assert not c.not_comparable and not c.regressions
