"""Gate and aggregation on a target with no ground-truth findings."""

import pytest

from evals.baseline import compare
from evals.results import aggregate, save
from evals.run import main, render_summary
from tests.evals.builders import CLEAN, make_baseline, make_run, make_summary


def clean_run(fp=0, **kw):
    return make_run(
        precision=None if fp == 0 else 0.0,
        recall=None,
        by_class={},
        fp=fp,
        **kw,
    )


def clean_summary(fps, **kw):
    return make_summary([clean_run(fp) for fp in fps], n_truth=0, **kw)


def test_aggregate_counts_fp_free_runs_and_mean_fp():
    agg = aggregate([clean_run(0), clean_run(2), clean_run(0)], CLEAN)
    assert agg.fp_free_runs == 2
    assert agg.fp.mean == pytest.approx(2 / 3) and agg.fp.max == 2
    assert agg.recall is None and agg.precision is not None  # 0.0 from the run with fps
    assert agg.recall_by_class == {}


def test_all_na_aggregate_does_not_divide_by_zero():
    agg = aggregate([clean_run(0), clean_run(0)], CLEAN)
    assert (agg.precision, agg.recall, agg.f1) == (None, None, None)
    assert agg.fp_free_runs == 2


def test_clean_run_against_clean_baseline_passes():
    base = make_baseline(clean_summary([0, 0, 0]))
    c = compare(clean_summary([0, 0, 0]), base)
    assert not c.regressions and not c.not_comparable


def test_one_false_positive_in_one_run_fails_clean_target_gate():
    base = make_baseline(clean_summary([0, 0, 0, 0, 0]))
    c = compare(clean_summary([0, 0, 0, 0, 1]), base)
    assert any(r.startswith("fp:") for r in c.regressions)
    assert any(r.startswith("fp_free_runs: 4/5") for r in c.regressions)


def test_fewer_false_positives_than_baseline_passes():
    base = make_baseline(clean_summary([1, 1, 0]))
    assert not compare(clean_summary([0, 0, 0]), base).regressions


def test_equal_nonzero_false_positives_is_not_a_regression():
    base = make_baseline(clean_summary([1, 0]))
    assert not compare(clean_summary([1, 0]), base).regressions


def test_normal_target_does_not_gate_fp_directly():
    base = make_baseline(make_summary(n_truth=5))
    cur = make_summary([make_run(fp=1, precision=0.95), make_run(fp=1, precision=0.95)], n_truth=5)
    assert not compare(cur, base).regressions


def test_n_truth_mismatch_is_not_comparable():
    c = compare(clean_summary([0]), make_baseline(make_summary([make_run()], n_truth=5)))
    assert any(line.startswith("n_truth") for line in c.not_comparable)


def test_render_summary_shows_fp_rows_with_na_metrics():
    out = render_summary(clean_summary([0, 1]))
    rows = {line.split("  ")[0].strip(): line.split("  ")[-1].strip() for line in out.splitlines()}
    assert rows["fp-free runs"] == "1/2"
    assert rows["recall"] == "n/a"


def test_cli_gate_exit_codes_on_clean_target(tmp_path, capsys):
    base, good, bad = (tmp_path / n for n in ("b.json", "good.json", "bad.json"))
    save(make_baseline(clean_summary([0, 0])), base)
    save(clean_summary([0, 0]), good)
    save(clean_summary([0, 1]), bad)
    assert main(["--summary-file", str(good), "--baseline", str(base)]) == 0
    assert main(["--summary-file", str(bad), "--baseline", str(base)]) == 1
    assert "regression: fp" in capsys.readouterr().out
