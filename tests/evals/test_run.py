import json

import pytest

from evals.run import main, render
from evals.scorer import Score, load_ground_truth
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
