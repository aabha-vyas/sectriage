import json
import os
import re
from pathlib import Path
from typing import Any

import pytest

from evals import run as eval_run
from src.agent import run as cli
from src.agent.events import read_events
from src.agent.model import ModelError
from tests.agent.fakes import FakeClock, FakeModel, say, tool_call

VULN_APP = cli.TARGETS_DIR / "vuln_app"

SECRET = {
    "file": "app.py",
    "line_start": 11,
    "line_end": 11,
    "vuln_class": "hardcoded_secret",
    "severity": "high",
    "title": "hardcoded secret",
    "explanation": "the key is a string literal",
    "confidence": 0.9,
}
SQLI = {
    **SECRET,
    "line_start": 30,
    "line_end": 31,
    "vuln_class": "sqli",
    "title": "sql built with an f-string",
    "explanation": "user input is formatted into the query",
}


def report(args: dict[str, Any], **kw: Any):
    return tool_call("report_finding", args, **kw)


def finish():
    return tool_call("finish", {})


def main(tmp_path: Path, model: FakeModel, *extra: str, target: Path = VULN_APP) -> int:
    argv = ["--target", str(target), "--out", str(tmp_path / "findings.json"), *extra]
    return cli.main(argv, model=model)


def meta(tmp_path: Path) -> dict[str, Any]:
    return json.loads((tmp_path / "findings.meta.json").read_text())


def findings(tmp_path: Path) -> list[dict[str, Any]]:
    return json.loads((tmp_path / "findings.json").read_text())


def test_scripted_run_writes_findings_that_the_eval_accepts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    model = FakeModel([report(SECRET), report(SQLI), finish()])
    assert main(tmp_path, model) == 0

    written = findings(tmp_path)
    assert {f["file"] for f in written} == {"evals/targets/vuln_app/app.py"}
    capsys.readouterr()

    code = eval_run.main(["--summary", "--findings", str(tmp_path / "findings.json")])
    out = capsys.readouterr().out
    assert code == 0
    assert re.search(r"tp / fp / fn\s+2 / 0 / 3", out)


def test_meta_and_default_events_file(tmp_path: Path):
    assert main(tmp_path, FakeModel([report(SECRET, cost=0.01), finish()])) == 0
    m = meta(tmp_path)
    assert m["stop_reason"] == "finish"
    assert m["steps"] == 2 and m["findings"] == 1
    assert m["model"] == "claude-haiku-5-5"
    assert m["prompt_version"] == "v1"
    assert m["price_verified_on"]
    assert m["max_steps"] == 30 and m["timeout_s"] == 600 and m["max_cost_usd"] == 0.5
    assert m["cost_usd"] == pytest.approx(0.01)
    assert m["error"] is None

    events = read_events(tmp_path / "findings.events.jsonl")
    assert [e.step for e in events] == [1, 2, 3]
    assert {e.task_id for e in events} == {m["task_id"]}


def test_explicit_events_path_and_fresh_log_each_run(tmp_path: Path):
    events_path = tmp_path / "custom.jsonl"
    for _ in range(2):
        assert main(tmp_path, FakeModel([finish()]), "--events", str(events_path)) == 0
    assert len(read_events(events_path)) == 2  # one step + terminal, not appended twice


def test_cli_options_reach_the_task(tmp_path: Path):
    model = FakeModel([finish()])
    argv = ["--target", str(VULN_APP), "--out", str(tmp_path / "findings.json")]
    argv += ["--max-steps", "7", "--timeout-s", "90"]
    assert cli.main(argv, model=model, clock=FakeClock()) == 0
    m = meta(tmp_path)
    assert (m["max_steps"], m["timeout_s"]) == (7, 90)
    assert model.calls[0]["timeout_s"] == 90


# --- refusing bad paths ---------------------------------------------------------------------


@pytest.mark.parametrize(
    "target",
    [
        "outside",
        "targets-root",
        "dotdot",
        "missing",
    ],
)
def test_targets_outside_evals_targets_are_refused(
    tmp_path: Path, target: str, capsys: pytest.CaptureFixture[str]
):
    outside = tmp_path / "outside"
    outside.mkdir()
    path = {
        "outside": outside,
        "targets-root": cli.TARGETS_DIR,
        "dotdot": VULN_APP / ".." / "..",
        "missing": cli.TARGETS_DIR / "does_not_exist",
    }[target]
    model = FakeModel([finish()])
    assert main(tmp_path, model, target=path) == 2
    assert "error:" in capsys.readouterr().err
    assert model.calls == []
    assert not (tmp_path / "findings.json").exists()


def test_a_file_target_is_refused(tmp_path: Path):
    assert main(tmp_path, FakeModel([finish()]), target=VULN_APP / "app.py") == 2


@pytest.mark.parametrize("flag", ["--out", "--events"])
def test_outputs_inside_evals_targets_are_refused(
    tmp_path: Path, flag: str, capsys: pytest.CaptureFixture[str]
):
    bad = VULN_APP / "should_not_exist.json"
    argv = ["--target", str(VULN_APP), "--out", str(tmp_path / "findings.json")]
    if flag == "--out":
        argv[argv.index("--out") + 1] = str(bad)
    else:
        argv += ["--events", str(bad)]
    model = FakeModel([finish()])
    try:
        assert cli.main(argv, model=model) == 2
        assert "must not be inside" in capsys.readouterr().err
        assert model.calls == []
        assert not bad.exists()
        assert not list(VULN_APP.glob("*.meta.json"))
        assert not list(tmp_path.glob("*"))
    finally:
        bad.unlink(missing_ok=True)


def test_an_output_that_reaches_targets_through_dotdot_is_refused(tmp_path: Path):
    inside = VULN_APP / "sub" / ".." / "findings.json"
    argv = ["--target", str(VULN_APP), "--out", str(inside)]
    assert cli.main(argv, model=FakeModel([finish()])) == 2
    assert not (VULN_APP / "findings.json").exists()


@pytest.mark.parametrize(
    "bad", [["--max-steps", "0"], ["--timeout-s", "0"], ["--max-cost-usd", "0"]]
)
def test_non_positive_limits_are_refused(tmp_path: Path, bad: list[str]):
    model = FakeModel([finish()])
    assert main(tmp_path, model, *bad) == 2
    assert model.calls == []


def test_unknown_model_is_refused_before_any_call(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    argv = ["--target", str(VULN_APP), "--out", str(tmp_path / "f.json"), "--model", "nope"]
    assert cli.main(argv) == 2
    assert "no price for model" in capsys.readouterr().err


# --- abnormal stops still write everything --------------------------------------------------


def test_abnormal_stop_still_writes_findings_and_returns_1(tmp_path: Path):
    model = FakeModel([report(SECRET), ModelError("APIStatusError (status 529)")])
    assert main(tmp_path, model) == 1
    assert len(findings(tmp_path)) == 1
    m = meta(tmp_path)
    assert m["stop_reason"] == "model_error" and m["error"] == "APIStatusError (status 529)"
    assert read_events(tmp_path / "findings.events.jsonl")[-1].ok is False


def test_nudge_then_stop_is_reported_as_model_stopped(tmp_path: Path):
    assert main(tmp_path, FakeModel([report(SECRET), say(), say()])) == 1
    assert meta(tmp_path)["stop_reason"] == "model_stopped"
    assert len(findings(tmp_path)) == 1


def test_budget_stop_is_recorded_in_meta(tmp_path: Path):
    model = FakeModel(
        [report(SECRET, cost=0.3), tool_call("search_code", {"pattern": "x"}, cost=0.3)]
    )
    assert main(tmp_path, model, "--max-cost-usd", "0.5") == 1
    m = meta(tmp_path)
    assert m["stop_reason"] == "budget"
    assert m["max_cost_usd"] == 0.5
    assert m["cost_usd"] == pytest.approx(0.6)
    assert len(findings(tmp_path)) == 1


def test_max_steps_is_a_clean_stop(tmp_path: Path):
    model = FakeModel(tool_call("search_code", {"pattern": f"q{i}"}) for i in range(5))
    assert main(tmp_path, model, "--max-steps", "2") == 0
    assert meta(tmp_path)["stop_reason"] == "max_steps"


def test_keyboard_interrupt_writes_everything_and_exits_130(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
):
    model = FakeModel([report(SECRET), KeyboardInterrupt()])
    assert main(tmp_path, model) == 130
    assert len(findings(tmp_path)) == 1
    assert meta(tmp_path)["stop_reason"] == "interrupted"
    events = read_events(tmp_path / "findings.events.jsonl")
    assert events[-1].error == "interrupted"
    assert "stop=interrupted" in capsys.readouterr().out


# --- atomic writes --------------------------------------------------------------------------


def test_no_temp_files_are_left_behind(tmp_path: Path):
    assert main(tmp_path, FakeModel([report(SECRET), finish()])) == 0
    assert sorted(p.name for p in tmp_path.iterdir()) == [
        "findings.events.jsonl",
        "findings.json",
        "findings.meta.json",
    ]


def test_a_failed_write_keeps_the_old_file_and_cleans_up(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "findings.json"
    path.write_text("old\n")

    def boom(src: str, dst: str) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(os, "replace", boom)
    with pytest.raises(OSError, match="disk full"):
        cli._atomic_write(path, "new\n")
    assert path.read_text() == "old\n"
    assert [p.name for p in tmp_path.iterdir()] == ["findings.json"]
