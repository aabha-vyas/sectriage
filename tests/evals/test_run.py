import json

import pytest

from evals.results import save
from evals.run import main, render
from evals.scorer import Score, load_ground_truth
from tests.evals.builders import make_baseline, make_run, make_summary
from tests.fixtures import oracle_findings


def test_render_formats_na_and_values():
    out = render(
        "t",
        Score(tp=1, fp=0, fn=1, precision=None, recall=0.5, f1=None, recall_by_class={"xss": 0.5}),
    )
    lines = out.splitlines()
    assert lines[0].split() == ["target", "t"]
    assert any(line.split() == ["precision", "n/a"] for line in lines)
    assert any(line.split() == ["recall", "0.50"] for line in lines)
    assert any(line.split() == ["recall[xss]", "0.50"] for line in lines)


def test_main_without_findings_prints_na_table(capsys):
    assert main(["--summary"]) == 0
    out = capsys.readouterr().out
    assert "0 / 0 / 5" in out and "n/a" in out


def test_main_summary_flag_is_optional(capsys):
    assert main([]) == 0
    assert "vuln_app" in capsys.readouterr().out


def test_main_scores_findings_file(tmp_path, capsys):
    findings = [f.model_dump(mode="json") for f in oracle_findings(load_ground_truth("vuln_app"))]
    path = tmp_path / "f.json"
    path.write_text(json.dumps(findings))
    assert main(["--summary", "--findings", str(path)]) == 0
    out = capsys.readouterr().out
    assert "5 / 0 / 0" in out
    assert "n/a" not in out


@pytest.mark.parametrize(
    "content",
    ["{nope", '{"not": "a list"}', '[{"finding_id": "x"}]', None],
    ids=["bad-json", "not-a-list", "invalid-finding", "missing-file"],
)
def test_bad_findings_input_is_one_line_error_exit_2(tmp_path, capsys, content):
    path = tmp_path / "f.json"
    if content is not None:
        path.write_text(content)
    assert main(["--summary", "--findings", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    err_lines = captured.err.strip().splitlines()
    assert len(err_lines) == 1 and err_lines[0].startswith("error:")


@pytest.mark.parametrize("target", ["../x", "Vuln", "a b", "vuln-app", "", "a\n"])
def test_invalid_target_rejected(capsys, target):
    assert main(["--summary", "--target", target]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_unknown_but_valid_target_is_clean_error(capsys):
    assert main(["--summary", "--target", "no_such_target"]) == 2
    assert capsys.readouterr().err.startswith("error:")


# --- gate: --summary-file / --baseline -------------------------------------------------------


def _gate_files(tmp_path, summary, baseline=None):
    s_path, b_path = tmp_path / "summary.json", tmp_path / "baseline.json"
    save(summary, s_path)
    save(baseline or make_baseline(make_summary()), b_path)
    return ["--summary-file", str(s_path), "--baseline", str(b_path)]


def test_gate_pass_exits_0(tmp_path, capsys):
    assert main(_gate_files(tmp_path, make_summary())) == 0
    out = capsys.readouterr().out
    assert "gate: PASS" in out
    assert any(line.split() == ["patch", "pass", "rate", "n/a"] for line in out.splitlines())


def test_gate_regression_exits_1(tmp_path, capsys):
    worse = make_summary([make_run(recall=0.5), make_run(recall=0.5)])
    assert main(_gate_files(tmp_path, worse)) == 1
    out = capsys.readouterr().out
    assert "regression: recall" in out and "gate: FAIL" in out


def test_gate_not_comparable_exits_3(tmp_path, capsys):
    other = make_summary(sandbox="docker")
    assert main(_gate_files(tmp_path, other)) == 3
    out = capsys.readouterr().out
    assert "not comparable: sandbox" in out and "gate: NOT COMPARABLE" in out


def test_gate_per_class_warning_prints_but_exits_0(tmp_path, capsys):
    warned = make_summary([make_run(by_class={"sqli": 0.0, "xss": 1.0}) for _ in range(2)])
    assert main(_gate_files(tmp_path, warned)) == 0
    out = capsys.readouterr().out
    assert "warning: recall[sqli]" in out
    assert "regression:" not in out and "gate: PASS" in out


def test_summary_file_without_baseline_prints_table_exits_0(tmp_path, capsys):
    path = tmp_path / "s.json"
    save(make_summary(tuned=True), path)
    assert main(["--summary-file", str(path)]) == 0
    out = capsys.readouterr().out
    assert "t (tuned)" in out and "gate:" not in out


@pytest.mark.parametrize("which", ["summary", "baseline"])
def test_gate_unreadable_input_exits_2(tmp_path, capsys, which):
    args = _gate_files(tmp_path, make_summary())
    (tmp_path / f"{which}.json").write_text("{nope")
    assert main(args) == 2
    err = capsys.readouterr().err.strip().splitlines()
    assert len(err) == 1 and err[0].startswith("error:")


def test_gate_missing_summary_file_exits_2(tmp_path, capsys):
    assert main(["--summary-file", str(tmp_path / "missing.json")]) == 2
    assert capsys.readouterr().err.startswith("error:")


def test_baseline_without_summary_file_exits_2(tmp_path, capsys):
    assert main(["--baseline", str(tmp_path / "b.json")]) == 2
    assert "--summary-file" in capsys.readouterr().err


def test_summary_file_with_findings_exits_2(tmp_path, capsys):
    path = tmp_path / "s.json"
    save(make_summary(), path)
    assert main(["--summary-file", str(path), "--findings", str(path)]) == 2
    assert capsys.readouterr().err.startswith("error:")
