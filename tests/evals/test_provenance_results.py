import json
from pathlib import Path

import pytest
from pydantic import ValidationError

from evals.baseline import compare
from evals.results import (
    RESULTS_DIR,
    SCHEMA_VERSION,
    Summary,
    load_baseline,
    load_summary,
    save,
)
from evals.run import EXIT_ERROR, EXIT_NOT_COMPARABLE, EXIT_OK, main, render_summary
from evals.summary import BASELINES_DIR, build_summary, to_baseline
from tests.evals.builders import make_baseline, make_provenance, make_run, make_summary, write_run

# A real schema 2 result file (a copy of evals/results/vuln_app-20261025T170847Z.json).
LEGACY = Path(__file__).parents[1] / "fixtures" / "legacy_summary_v2.json"
LEGACY_BASELINE = BASELINES_DIR / "vuln_app.json"  # a committed schema 2 baseline


def legacy_dict() -> dict:
    return json.loads(LEGACY.read_text())


# --- legacy files ----------------------------------------------------------------------------


def test_the_legacy_fixture_is_schema_2_without_provenance():
    raw = legacy_dict()
    assert raw["schema_version"] == 2 and "provenance" not in raw
    s = load_summary(LEGACY)
    assert s.provenance is None and all(r.provenance is None for r in s.runs)
    assert load_baseline(LEGACY_BASELINE).provenance is None


def test_every_committed_result_and_baseline_still_loads():
    for path in sorted(BASELINES_DIR.glob("*.json")):
        assert load_baseline(path).provenance is None, path
    for path in sorted(RESULTS_DIR.glob("*.json")):
        raw = json.loads(path.read_text())
        if isinstance(raw, dict) and "aggregate" in raw:
            assert load_summary(path).provenance is None, path


def test_a_legacy_summary_renders_provenance_as_absent():
    rows = render_summary(load_summary(LEGACY)).splitlines()
    assert any(r.split() == ["provenance", "absent", "(legacy", "schema", "2)"] for r in rows)


def test_a_new_summary_renders_commit_and_hashes():
    line = next(
        row for row in render_summary(make_summary()).splitlines() if row.startswith("provenance")
    )
    assert "cccccccc" in line and "aaaaaaaa" in line and "dddddddd" in line


def test_gate_legacy_against_legacy_is_unchanged(capsys):
    assert main(["--summary-file", str(LEGACY), "--baseline", str(LEGACY_BASELINE)]) == EXIT_OK
    out = capsys.readouterr().out
    assert "gate: PASS" in out and "warning" not in out


def test_new_summary_against_a_legacy_baseline_is_comparable_with_a_warning(tmp_path, capsys):
    legacy = load_baseline(LEGACY_BASELINE)
    cur = make_summary(
        [make_run() for _ in range(legacy.n_runs)],
        target=legacy.target,
        n_truth=legacy.n_truth,
        model=legacy.model,
        prompt_version=legacy.prompt_version,
        tools=legacy.tools,
        max_steps=legacy.max_steps,
    )
    c = compare(cur, legacy)
    assert c.not_comparable == []
    assert any("provenance absent in baseline" in w for w in c.warnings)


# --- the schema ------------------------------------------------------------------------------


def test_a_schema_3_round_trip_keeps_provenance(tmp_path):
    s = make_summary()
    path = tmp_path / "s.json"
    save(s, path)
    assert json.loads(path.read_text())["schema_version"] == SCHEMA_VERSION == 3
    assert load_summary(path) == s and load_summary(path).provenance == make_provenance()


def test_schema_3_requires_provenance_on_the_summary_and_every_run():
    with pytest.raises(ValidationError, match="requires provenance"):
        make_summary(provenance=None)
    bare = make_run()
    bare.provenance = None
    with pytest.raises(ValidationError, match="requires provenance"):
        make_summary([bare, make_run()])


def test_schema_2_must_not_carry_provenance_and_unknown_versions_are_refused():
    raw = legacy_dict()
    raw["provenance"] = make_provenance().model_dump(mode="json")
    with pytest.raises(ValidationError, match="predates provenance"):
        Summary.model_validate(raw)
    for version in (1, 4):
        raw = legacy_dict()
        raw["schema_version"] = version
        with pytest.raises(ValidationError, match="unsupported schema_version"):
            Summary.model_validate(raw)


# --- the gate --------------------------------------------------------------------------------


def _pair(**cur_overrides):
    base = make_baseline(make_summary())
    cur = make_summary(provenance=make_provenance(**cur_overrides))
    return compare(cur, base)


@pytest.mark.parametrize(
    "field",
    [
        ("system_prompt_sha256", "1" * 64),
        ("tool_schemas_sha256", "2" * 64),
        ("target_sha256", "3" * 64),
        ("price_table_version", "4" * 64),
        ("effort", "high"),
    ],
    ids=lambda f: f[0],
)
def test_comparability_fields_make_summaries_not_comparable(field):
    name, value = field
    c = _pair(**{name: value})
    assert len(c.not_comparable) == 1 and name in c.not_comparable[0]
    assert c.regressions == []


@pytest.mark.parametrize("name", ["price_table_version", "effort"])
def test_a_comparability_field_is_skipped_when_either_side_lacks_it(name):
    assert _pair(**{name: None}).not_comparable == []
    base = make_baseline(make_summary(provenance=make_provenance(**{name: None})))
    assert compare(make_summary(), base).not_comparable == []


def test_informational_fields_never_make_summaries_not_comparable():
    c = _pair(
        git_commit="9" * 40,
        started_at="2030-01-01T00:00:00Z",
        finished_at="2030-01-01T00:01:00Z",
        price_verified_on="2030-01-01",
        python_version="3.13.0",
        anthropic_sdk_version="9.9.9",
        max_tokens=1,
        caching=False,
        git_dirty=True,
    )
    assert c.not_comparable == [] and c.regressions == []
    text = " ".join(c.warnings)
    for needle in ("python_version", "anthropic_sdk_version", "max_tokens", "caching", "dirty"):
        assert needle in text, needle
    assert "git_commit" not in text and "started_at" not in text


def test_a_patch_level_python_difference_is_not_even_a_warning():
    assert _pair(python_version="3.12.99").warnings == []


def test_exit_code_for_changed_prompt_hash_is_3(tmp_path, capsys):
    base, cur = tmp_path / "b.json", tmp_path / "c.json"
    save(make_baseline(make_summary(target="vuln_app")), base)
    save(
        make_summary(target="vuln_app", provenance=make_provenance(system_prompt_sha256="9" * 64)),
        cur,
    )
    assert main(["--summary-file", str(cur), "--baseline", str(base)]) == EXIT_NOT_COMPARABLE
    assert "system_prompt_sha256" in capsys.readouterr().out


# --- building summaries ----------------------------------------------------------------------


def test_build_summary_merges_run_provenance(tmp_path):
    paths = []
    for i, (dirty, start) in enumerate(
        [(False, "2026-10-25T12:00:00Z"), (True, "2026-10-25T11:00:00Z")]
    ):
        out = tmp_path / f"run{i}" / "findings.json"
        write_run(out, [], provenance=make_provenance(git_dirty=dirty, started_at=start))
        paths.append(out)
    s = build_summary("vuln_app", paths)
    assert s.schema_version == 3
    assert s.provenance.git_dirty is True  # any run
    assert s.provenance.started_at == "2026-10-25T11:00:00Z"  # first start
    assert [r.provenance.started_at for r in s.runs] == [
        "2026-10-25T12:00:00Z",
        "2026-10-25T11:00:00Z",
    ]


@pytest.mark.parametrize("field", ["system_prompt_sha256", "target_sha256", "git_commit"])
def test_build_summary_rejects_runs_that_disagree_on_provenance(tmp_path, field):
    paths = []
    for i, value in enumerate(["1" * 40, "2" * 40]):
        out = tmp_path / f"run{i}" / "findings.json"
        write_run(out, [], provenance=make_provenance(**{field: value}))
        paths.append(out)
    with pytest.raises(ValueError, match=f"disagree on provenance.{field}"):
        build_summary("vuln_app", paths)


def test_a_run_without_provenance_is_rejected_not_silently_legacy(tmp_path):
    out = tmp_path / "findings.json"
    write_run(out, [])
    meta = out.with_name("findings.meta.json")
    raw = json.loads(meta.read_text())
    del raw["provenance"]
    meta.write_text(json.dumps(raw))
    with pytest.raises(ValueError, match="no provenance"):
        build_summary("vuln_app", [out])


def test_summary_sandbox_comes_from_the_recorded_sandbox_mode(tmp_path):
    out = tmp_path / "findings.json"
    write_run(out, [], provenance=make_provenance(sandbox_mode="docker"))
    assert build_summary("vuln_app", [out]).sandbox == "docker"


# --- promoting to a baseline -----------------------------------------------------------------


def test_a_dirty_summary_cannot_become_a_baseline(tmp_path, capsys):
    summary = make_summary(provenance=make_provenance(git_dirty=True))
    with pytest.raises(ValueError, match="dirty working tree"):
        to_baseline(summary)
    src, dest = tmp_path / "s.json", tmp_path / "base" / "b.json"
    save(summary, src)
    assert main(["--summary-file", str(src), "--update-baseline", str(dest)]) == EXIT_ERROR
    assert not dest.exists()
    assert "dirty working tree" in capsys.readouterr().err


def test_unknown_dirty_state_and_clean_summaries_can_be_promoted(tmp_path):
    to_baseline(make_summary(provenance=make_provenance(git_dirty=None)))
    to_baseline(make_summary())
    to_baseline(load_summary(LEGACY))  # a legacy summary has nothing to refuse on
