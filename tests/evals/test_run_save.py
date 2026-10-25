import json

import pytest

from evals.results import load_baseline, load_summary, save
from evals.run import main
from evals.scorer import load_ground_truth
from tests.evals.builders import make_baseline, make_summary, write_run
from tests.fixtures import oracle_findings

TRUTH = load_ground_truth("vuln_app")


def _dirs(tmp_path):
    return {
        "results_dir": tmp_path / "results",
        "baselines_dir": tmp_path / "baselines",
        "runs_dir": tmp_path / "runs",
    }


def _fake_runner(findings, **kw):
    calls = []

    def runner(target, out):
        calls.append((target, out))
        write_run(out, findings(len(calls)), **kw)

    runner.calls = calls
    return runner


# --- --save ----------------------------------------------------------------------------------


def test_save_findings_file_writes_summary(tmp_path, capsys):
    out = tmp_path / "f.json"
    write_run(out, oracle_findings(TRUTH), cost=0.02)
    dest = tmp_path / "s.json"
    assert main(["--findings", str(out), "--save", str(dest)]) == 0
    summary = load_summary(dest)
    assert summary.target == "vuln_app" and summary.tuned is True
    assert summary.model == "m" and summary.prompt_version == "p1"
    assert summary.n_runs == 1 and summary.aggregate.clean_runs == 1
    run = summary.runs[0]
    assert (run.tp, run.fp, run.fn) == (5, 0, 0)
    assert run.cost_usd == 0.02 and run.cost_per_finding_usd == pytest.approx(0.004)
    assert run.latency_s.wall == 10.0
    assert f"saved: {dest}" in capsys.readouterr().out


def test_save_without_path_uses_results_dir_and_untuned_target_flag(tmp_path):
    dirs = _dirs(tmp_path)
    out = tmp_path / "f.json"
    write_run(out, [])
    assert main(["--findings", str(out), "--save"], **dirs) == 0
    [saved] = dirs["results_dir"].glob("vuln_app-*.json")
    assert load_summary(saved).aggregate.recall.mean == 0.0


def test_save_without_a_run_is_an_error(capsys):
    assert main(["--save"]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_findings_without_sidecars_is_one_line_error(tmp_path, capsys):
    out = tmp_path / "f.json"
    out.write_text("[]")
    assert main(["--findings", str(out), "--save", str(tmp_path / "s.json")]) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("error:")
    assert not (tmp_path / "s.json").exists()


def test_findings_with_baseline_gates_directly(tmp_path, capsys):
    out = tmp_path / "f.json"
    write_run(out, [])  # recall 0
    base = tmp_path / "b.json"
    save(
        make_baseline(
            make_summary(target="vuln_app", model="m", prompt_version="p1", tuned=True, n_truth=5)
        ),
        base,
    )
    assert main(["--findings", str(out), "--baseline", str(base)]) == 1
    assert "regression: recall" in capsys.readouterr().out


# --- --repeat --------------------------------------------------------------------------------


def test_repeat_runs_n_times_and_aggregates(tmp_path):
    dirs = _dirs(tmp_path)
    full, empty = oracle_findings(TRUTH), []
    runner = _fake_runner(lambda k: full if k % 2 else empty)
    dest = tmp_path / "s.json"
    assert main(["--repeat", "4", "--save", str(dest)], runner=runner, **dirs) == 0
    assert [t for t, _ in runner.calls] == ["vuln_app"] * 4
    assert len({str(o) for _, o in runner.calls}) == 4
    summary = load_summary(dest)
    assert summary.n_runs == 4
    recall = summary.aggregate.recall
    assert (recall.mean, recall.min, recall.max) == (0.5, 0.0, 1.0)
    assert recall.stdev > 0


def test_repeat_with_target_passes_target_to_runner(tmp_path):
    runner = _fake_runner(lambda k: [])
    assert main(["--repeat", "1", "--target", "vuln_app"], runner=runner, **_dirs(tmp_path)) == 0
    assert runner.calls[0][0] == "vuln_app"


@pytest.mark.parametrize("n", ["0", "-1", "21"])
def test_repeat_out_of_range_exits_2(tmp_path, capsys, n):
    runner = _fake_runner(lambda k: [])
    assert main(["--repeat", n], runner=runner, **_dirs(tmp_path)) == 2
    assert runner.calls == [] and capsys.readouterr().err.startswith("error:")


def test_repeat_without_a_runner_goes_through_preflight_and_spends_nothing(
    tmp_path, capsys, monkeypatch
):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)  # preflight must fail on the key
    assert main(["--repeat", "2"], **_dirs(tmp_path)) == 2
    assert "ANTHROPIC_API_KEY" in capsys.readouterr().err
    assert not (tmp_path / "runs").exists()  # no run dir, no ledger


def test_repeat_and_findings_are_exclusive(tmp_path, capsys):
    runner = _fake_runner(lambda k: [])
    assert main(["--repeat", "1", "--findings", str(tmp_path / "f.json")], runner=runner) == 2
    assert runner.calls == []


def test_repeat_runner_failure_stops_and_reports_kept_runs(tmp_path, capsys):
    dirs = _dirs(tmp_path)

    def runner(target, out):
        if "run2" in str(out):
            raise RuntimeError("budget exhausted")
        write_run(out, [])

    assert main(["--repeat", "3", "--save", str(tmp_path / "s.json")], runner=runner, **dirs) == 2
    err = capsys.readouterr().err
    assert "run 2/3 failed: RuntimeError: budget exhausted" in err and "1 finished run" in err
    assert not (tmp_path / "s.json").exists()
    assert list(dirs["runs_dir"].glob("*/run1/findings.json"))


def test_repeat_rejects_runs_that_disagree_on_prompt_version(tmp_path, capsys):
    count = {"n": 0}

    def runner(target, out):
        count["n"] += 1
        write_run(out, [], prompt_version=f"p{count['n']}")

    assert main(["--repeat", "2"], runner=runner, **_dirs(tmp_path)) == 2
    assert "disagree on prompt_version" in capsys.readouterr().err


def test_repeat_gate_regression_exits_1(tmp_path, capsys):
    base = tmp_path / "b.json"
    save(
        make_baseline(make_summary(target="vuln_app", prompt_version="p1", tuned=True, n_truth=5)),
        base,
    )
    runner = _fake_runner(lambda k: [])
    assert main(["--repeat", "2", "--baseline", str(base)], runner=runner, **_dirs(tmp_path)) == 1
    assert "gate: FAIL" in capsys.readouterr().out


# --- --update-baseline -----------------------------------------------------------------------


def test_update_baseline_promotes_summary_to_default_path(tmp_path, capsys):
    dirs = _dirs(tmp_path)
    s = tmp_path / "s.json"
    save(make_summary(target="vuln_app"), s)
    assert main(["--summary-file", str(s), "--update-baseline"], **dirs) == 0
    base = load_baseline(dirs["baselines_dir"] / "vuln_app.json")
    assert (
        base.target == "vuln_app"
        and base.tolerances.recall == 0.15
        and base.tolerances.precision == 0.10
    )
    assert "baseline written" in capsys.readouterr().out


def test_update_baseline_explicit_path_then_gate_passes(tmp_path):
    s, b = tmp_path / "s.json", tmp_path / "b.json"
    save(make_summary(), s)
    assert main(["--summary-file", str(s), "--update-baseline", str(b)]) == 0
    assert main(["--summary-file", str(s), "--baseline", str(b)]) == 0


def test_update_baseline_refuses_unclean_runs_and_keeps_old_file(tmp_path, capsys):
    from tests.evals.builders import make_run

    s, b = tmp_path / "s.json", tmp_path / "b.json"
    b.write_text("old")
    save(make_summary([make_run(stop="timeout"), make_run()]), s)
    assert main(["--summary-file", str(s), "--update-baseline", str(b)]) == 2
    assert "1/2 runs stopped cleanly" in capsys.readouterr().err
    assert b.read_text() == "old"


def test_update_baseline_needs_summary_file(capsys):
    assert main(["--update-baseline"]) == 2
    assert "--summary-file" in capsys.readouterr().err


def test_update_baseline_cannot_combine_with_baseline(tmp_path, capsys):
    s = tmp_path / "s.json"
    save(make_summary(), s)
    assert main(["--summary-file", str(s), "--update-baseline", "--baseline", str(s)]) == 2


def test_deliberate_regression_after_update_baseline_exits_nonzero(tmp_path):
    """The lane's done-criterion, end to end through saved files."""
    good, bad, b = tmp_path / "good.json", tmp_path / "bad.json", tmp_path / "b.json"
    write_run(good, oracle_findings(TRUTH))
    write_run(bad, oracle_findings(TRUTH)[:2])
    assert main(["--findings", str(good), "--save", str(tmp_path / "gs.json")]) == 0
    assert main(["--summary-file", str(tmp_path / "gs.json"), "--update-baseline", str(b)]) == 0
    assert main(["--findings", str(good), "--baseline", str(b)]) == 0
    assert main(["--findings", str(bad), "--baseline", str(b)]) == 1
    json.loads(b.read_text())
