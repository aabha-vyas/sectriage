import json

import pytest
from pydantic import ValidationError

from evals.results import SCHEMA_VERSION, load_baseline, load_summary, save, stat
from tests.evals.builders import CLEAN, make_baseline, make_run, make_summary


def test_stat_skips_nulls_and_handles_single_value():
    assert stat([None, None]) is None
    s = stat([None, 0.5])
    assert s is not None and s.mean == 0.5 and s.stdev == 0.0
    s = stat([0.0, 1.0])
    assert s is not None and s.min == 0.0 and s.max == 1.0 and s.mean == 0.5


def test_round_trip_summary_and_baseline(tmp_path):
    summary = make_summary()
    path = tmp_path / "s.json"
    save(summary, path)
    assert load_summary(path) == summary
    assert json.loads(path.read_text())["schema_version"] == SCHEMA_VERSION

    base_path = tmp_path / "b.json"
    save(make_baseline(summary), base_path)
    assert load_baseline(base_path).tolerances.cost_rel == 0.25


def test_patch_pass_rate_is_always_null(tmp_path):
    path = tmp_path / "s.json"
    save(make_summary(), path)
    raw = json.loads(path.read_text())
    assert raw["aggregate"]["patch_pass_rate"] is None
    raw["aggregate"]["patch_pass_rate"] = 0.9
    with pytest.raises(ValidationError):
        load_summary_from(raw)


def load_summary_from(raw: dict):
    from evals.results import Summary

    return Summary.model_validate(raw)


def test_atomic_save_leaves_no_temp_files(tmp_path):
    save(make_summary(), tmp_path / "out" / "s.json")
    assert [p.name for p in (tmp_path / "out").iterdir()] == ["s.json"]


def test_aggregate_counts_clean_runs_and_per_class_stats():
    runs = [
        make_run(by_class={"sqli": 1.0}),
        make_run(by_class={"sqli": 0.0}, stop="budget_exceeded"),
    ]
    agg = make_summary(runs).aggregate
    assert agg.clean_runs == 1 and CLEAN
    assert agg.recall_by_class["sqli"].mean == 0.5
    assert agg.recall_by_class["sqli"].min == 0.0


def test_unknown_fields_are_rejected():
    raw = make_summary().model_dump()
    raw["surprise"] = 1
    with pytest.raises(ValidationError):
        load_summary_from(raw)
