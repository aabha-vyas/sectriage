"""The live runner with a fake model and fake sandbox: no network, no Docker, no real spend."""

import json
import subprocess

import pytest

from evals import frozen, live
from evals.results import load_summary
from evals.run import main
from src.agent.model import ModelError
from tests.agent.fakes import FakeModel, FakeSandbox, tool_call

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


def ok_run(cost=0.01):
    return FakeModel([tool_call("report_finding", SECRET, cost=cost), tool_call("finish", {})])


def failing_run():
    return FakeModel([ModelError("boom")])


def scripted(*runs):
    """A model_factory handing out one fake model per run, in order."""
    queue = list(runs)
    return lambda: queue.pop(0)


def runner_for(tmp_path, *runs, **kw):
    return live.make_runner(
        root=tmp_path / "runs",
        preflight_check=lambda: None,
        model_factory=scripted(*runs),
        sandbox_factory=lambda task: FakeSandbox(),
        **kw,
    )


def write_meta(root, name, cost, stop="finish"):
    run = root / name
    run.mkdir(parents=True)
    (run / "findings.meta.json").write_text(json.dumps({"cost_usd": cost, "stop_reason": stop}))


def dirs(tmp_path):
    return {
        "results_dir": tmp_path / "results",
        "baselines_dir": tmp_path / "baselines",
        "runs_dir": tmp_path / "runs",
    }


# --- ledger ----------------------------------------------------------------------------------


def test_ledger_sums_the_cost_of_every_run_meta(tmp_path):
    root = tmp_path / "runs"
    write_meta(root, "a/run1", 0.25)
    write_meta(root, "b/run1", 0.5)
    spend = live.update_ledger(root, 0.5)
    assert spend.total_usd == 0.75
    ledger = json.loads((root / "spend.json").read_text())
    assert ledger["total_usd"] == 0.75 and len(ledger["runs"]) == 2


def test_a_run_with_no_meta_counts_at_the_per_run_cap(tmp_path):
    root = tmp_path / "runs"
    (root / "dead").mkdir(parents=True)
    (root / "dead" / "findings.events.jsonl").write_text("{}\n")  # killed before meta
    (root / "bad").mkdir()
    (root / "bad" / "findings.meta.json").write_text("not json")
    spend = live.sum_spend(root, 0.5)
    assert spend.total_usd == 1.0 and all(r["estimated"] for r in spend.runs)


def test_ledger_refuses_a_run_that_could_pass_the_budget(tmp_path):
    write_meta(tmp_path / "runs", "old/run1", 1.60)
    calls = []
    runner = live.make_runner(
        root=tmp_path / "runs",
        preflight_check=lambda: None,
        agent_main=lambda argv, **kw: calls.append(argv) or 0,
    )
    with pytest.raises(live.BudgetExceeded, match=r"\$1\.6000 \+ per-run cap \$0\.50"):
        runner("vuln_app", tmp_path / "runs" / "new" / "findings.json")
    assert calls == []  # the agent was never started


def test_ledger_allows_a_run_that_lands_exactly_on_the_budget(tmp_path):
    write_meta(tmp_path / "runs", "old/run1", 1.50)
    calls = []

    def agent_main(argv, **kw):
        calls.append(argv)
        return 0

    runner = live.make_runner(
        root=tmp_path / "runs", preflight_check=lambda: None, agent_main=agent_main
    )
    runner("vuln_app", tmp_path / "runs" / "new" / "findings.json")
    assert len(calls) == 1


def test_runs_are_refused_once_the_budget_is_spent_and_each_run_prints_the_total(tmp_path, capsys):
    runs = [ok_run(0.30) for _ in range(4)]
    runner = runner_for(tmp_path, *runs, budget_usd=1.2)
    root = tmp_path / "runs"
    for k, total in enumerate(["0.3000", "0.6000", "0.9000"], start=1):
        runner("vuln_app", root / "b" / f"run{k}" / "findings.json")
        assert f"spend: ${total} of $1.20" in capsys.readouterr().out
    with pytest.raises(live.BudgetExceeded):  # 0.90 + 0.50 > 1.20
        runner("vuln_app", root / "b" / "run4" / "findings.json")
    assert not (root / "b" / "run4").exists()


def test_agent_gets_the_pinned_model_and_the_per_run_cap(tmp_path):
    seen = []
    runner = live.make_runner(
        root=tmp_path / "runs",
        max_cost_usd=0.25,
        preflight_check=lambda: None,
        agent_main=lambda argv, **kw: seen.append(argv) or 0,
    )
    runner("vuln_app", tmp_path / "runs" / "r" / "findings.json")
    argv = seen[0]
    assert argv[argv.index("--model") + 1] == "claude-haiku-5-5"
    assert argv[argv.index("--max-cost-usd") + 1] == "0.25"
    assert argv[argv.index("--target") + 1].endswith("evals/targets/vuln_app")


def test_a_run_outside_the_ledger_root_is_refused(tmp_path):
    runner = runner_for(tmp_path, ok_run())
    with pytest.raises(live.RunFailed, match="outside"):
        runner("vuln_app", tmp_path / "elsewhere" / "findings.json")


# --- failing runs ----------------------------------------------------------------------------


def test_a_failing_run_keeps_earlier_artifacts_and_its_own_cost(tmp_path, capsys):
    runner = runner_for(tmp_path, ok_run(), failing_run(), ok_run())
    code = main(
        ["--repeat", "3", "--save", str(tmp_path / "s.json")], runner=runner, **dirs(tmp_path)
    )
    assert code == 2
    err = capsys.readouterr().err
    assert "run 2/3 failed" in err and "RunFailed" in err and "1 finished run(s) kept" in err
    [batch] = (tmp_path / "runs").glob("vuln_app-*")
    assert json.loads((batch / "run1" / "findings.json").read_text())[0]["vuln_class"] == (
        "hardcoded_secret"
    )
    assert (batch / "run1" / "findings.meta.json").exists()
    assert (batch / "run2" / "findings.meta.json").exists()  # the failed run left its meta
    assert not (batch / "run3").exists()  # and nothing after it started
    assert json.loads((tmp_path / "runs" / "spend.json").read_text())["total_usd"] == 0.01
    assert not (tmp_path / "s.json").exists()  # no summary from a broken batch


def test_repeat_with_the_live_runner_builds_a_summary(tmp_path, capsys):
    runner = runner_for(tmp_path, ok_run(0.02), ok_run(0.04))
    dest = tmp_path / "s.json"
    assert main(["--repeat", "2", "--save", str(dest)], runner=runner, **dirs(tmp_path)) == 0
    summary = load_summary(dest)
    assert summary.n_runs == 2 and summary.model == "claude-haiku-5-5"
    assert summary.aggregate.cost_usd.mean == pytest.approx(0.03)
    assert "spend: $0.0600 of $2.00" in capsys.readouterr().out


@pytest.mark.parametrize(
    ("code", "stop", "fails"),
    [
        (0, "finish", False),
        (1, "timeout", False),
        (1, "budget", False),
        (1, "refusal", False),
        (1, "model_error", True),
        (1, "internal_error", True),
        (130, "interrupted", True),
        (2, None, True),
    ],
)
def test_exit_codes_and_stop_reasons(tmp_path, code, stop, fails):
    out = tmp_path / "runs" / "r" / "findings.json"

    def agent_main(argv, **kw):
        out.parent.mkdir(parents=True, exist_ok=True)
        if stop:
            out.with_name("findings.meta.json").write_text(
                json.dumps({"cost_usd": 0.0, "stop_reason": stop})
            )
        return code

    runner = live.make_runner(
        root=tmp_path / "runs", preflight_check=lambda: None, agent_main=agent_main
    )
    if fails:
        with pytest.raises(live.RunFailed):
            runner("vuln_app", out)
    else:
        runner("vuln_app", out)


def test_a_run_over_its_cap_is_a_result_not_an_error(tmp_path):
    runner = runner_for(tmp_path, FakeModel([tool_call("report_finding", SECRET, cost=0.9)]))
    out = tmp_path / "runs" / "r" / "findings.json"
    runner("vuln_app", out)  # does not raise
    meta = json.loads(out.with_name("findings.meta.json").read_text())
    assert meta["stop_reason"] == "budget" and meta["cost_usd"] == 0.9


# --- preflight -------------------------------------------------------------------------------


def good_env(tmp_path):
    env_file = tmp_path / ".env"
    env_file.write_text("ANTHROPIC_API_KEY=placeholder-not-a-key\n")
    return {"ANTHROPIC_API_KEY": "placeholder-not-a-key"}, env_file


def git(repo, *args):
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@t", *args], cwd=repo, check=True
    )


def clean_repo(tmp_path):
    repo = tmp_path / "repo"
    (repo / "evals" / "targets" / "x").mkdir(parents=True)
    (repo / "evals" / "targets" / "x" / "a.py").write_text("A = 1\n")
    git(repo, "init", "-q")
    git(repo, "add", ".")
    git(repo, "commit", "-q", "-m", "init")
    return repo


def test_frozen_and_key_checks_pass_on_the_committed_state(tmp_path):
    env, env_file = good_env(tmp_path)
    # the real repo's git state is the developer's business; check the other two parts here
    assert live.frozen_problems() == []
    assert live.key_problems(env, env_file) == []


def test_preflight_fails_when_a_frozen_target_was_modified(tmp_path, monkeypatch):
    env, env_file = good_env(tmp_path)
    repo = clean_repo(tmp_path)
    changed = {**frozen.current_hashes()}
    victim = next(iter(changed))
    changed[victim] = "0" * 64
    monkeypatch.setattr(frozen, "current_hashes", lambda: changed)
    with pytest.raises(live.PreflightError, match=f"changed: {victim}"):
        live.preflight(repo=repo, env=env, env_file=env_file)


def test_make_runner_does_not_start_when_preflight_fails(tmp_path, monkeypatch):
    env, env_file = good_env(tmp_path)
    monkeypatch.setattr(frozen, "current_hashes", lambda: {"evals/targets/ledger_api/x.py": "1"})
    started = []
    with pytest.raises(live.PreflightError, match="frozen targets differ"):
        live.make_runner(
            root=tmp_path / "runs",
            preflight_check=lambda: live.preflight(
                repo=clean_repo(tmp_path), env=env, env_file=env_file
            ),
            agent_main=lambda argv, **kw: started.append(argv) or 0,
        )
    assert started == [] and not (tmp_path / "runs").exists()


def test_git_problems_lists_modified_and_untracked_files(tmp_path):
    repo = clean_repo(tmp_path)
    assert live.git_problems(repo) == []
    (repo / "evals" / "targets" / "x" / "a.py").write_text("A = 2\n")
    (repo / "evals" / "targets" / "x" / "new.py").write_text("B = 1\n")
    problems = live.git_problems(repo)
    assert any("a.py" in p for p in problems) and any("new.py" in p for p in problems)


def test_git_problems_outside_a_repo_is_a_problem(tmp_path):
    assert live.git_problems(tmp_path)


def test_key_problems_names_what_is_missing_without_printing_a_value(tmp_path):
    env, env_file = good_env(tmp_path)
    assert live.key_problems({}, env_file) == [
        "ANTHROPIC_API_KEY is not in the environment; run with `uv run --env-file .env`"
    ]
    missing = live.key_problems(env, tmp_path / "nope.env")
    assert len(missing) == 1 and "not defined" in missing[0]
    blank = tmp_path / "blank.env"
    blank.write_text("ANTHROPIC_API_KEY=\n")
    assert "not defined" in live.key_problems(env, blank)[0]
    assert all("placeholder" not in p for p in live.key_problems({}, blank))


# --- evals.run flags -------------------------------------------------------------------------


@pytest.mark.parametrize("flag", ["--budget-usd", "--max-cost-usd"])
def test_non_positive_caps_exit_2(tmp_path, capsys, flag):
    assert main(["--repeat", "1", flag, "0"], **dirs(tmp_path)) == 2
    assert "positive" in capsys.readouterr().err


def test_agent_gets_the_step_budget(tmp_path):
    seen = []
    runner = live.make_runner(
        root=tmp_path / "runs",
        max_steps=12,
        preflight_check=lambda: None,
        agent_main=lambda argv, **kw: seen.append(argv) or 0,
    )
    runner("vuln_app", tmp_path / "runs" / "r" / "findings.json")
    assert seen[0][seen[0].index("--max-steps") + 1] == "12"


def test_the_step_budget_defaults_to_the_agent_default(tmp_path):
    seen = []
    runner = live.make_runner(
        root=tmp_path / "runs",
        preflight_check=lambda: None,
        agent_main=lambda argv, **kw: seen.append(argv) or 0,
    )
    runner("vuln_app", tmp_path / "runs" / "r" / "findings.json")
    assert seen[0][seen[0].index("--max-steps") + 1] == "30"


def test_evals_run_flags_reach_the_live_runner_and_the_summary_records_the_steps(
    tmp_path, monkeypatch
):
    made = {}
    real_make_runner = live.make_runner

    def fake_make_runner(**kw):
        made.update(kw)
        return real_make_runner(
            root=kw["root"],
            max_steps=kw["max_steps"],
            preflight_check=lambda: None,
            model_factory=scripted(ok_run()),
            sandbox_factory=lambda task: FakeSandbox(),
        )

    monkeypatch.setattr(live, "make_runner", fake_make_runner)
    dest = tmp_path / "s.json"
    argv = ["--repeat", "1", "--max-steps", "7", "--budget-usd", "1.5", "--max-cost-usd", "0.25"]
    assert main([*argv, "--save", str(dest)], **dirs(tmp_path)) == 0
    assert (made["max_steps"], made["budget_usd"], made["max_cost_usd"]) == (7, 1.5, 0.25)
    assert load_summary(dest).max_steps == 7


def test_non_positive_max_steps_exits_2(tmp_path, capsys):
    assert main(["--repeat", "1", "--max-steps", "0"], **dirs(tmp_path)) == 2
    assert "positive" in capsys.readouterr().err
