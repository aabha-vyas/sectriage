"""live_run with a fake agent: no model call and no spend. One test also runs a real container."""

import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from src.agent.loop import StopReason
from src.agent.sandbox_protocol import ExecResult, SandboxUnavailableError
from src.contracts import Event, Task, ToolName, ToolResult
from src.sandbox import live_run
from src.sandbox.config import DEFAULT_CONFIG
from src.sandbox.docker_sandbox import create_sandbox

NOW = datetime(2026, 10, 26, 12, 0, 0, tzinfo=UTC)


def run_command_event(*, ok: bool = True, error: str | None = None) -> Event:
    result = ToolResult(
        call_id="c1",
        ok=ok,
        error=error,
        exit_code=0 if ok else None,
        duration_ms=1,
    )
    return Event(task_id="t", step=1, ts=NOW, tool=ToolName.RUN_COMMAND, result=result, ok=ok)


def other_event() -> Event:
    result = ToolResult(call_id="c0", ok=True, duration_ms=1)
    return Event(task_id="t", step=0, ts=NOW, tool=ToolName.READ_FILE, result=result)


class FakeAgent:
    """Stands in for `src.agent.run.main`: writes what it would, one list of events per call."""

    def __init__(self, *runs: list[Event], exit_code: int = 0, cost: float = 0.01) -> None:
        self.runs = list(runs)
        self.exit_code = exit_code
        self.cost = cost
        self.argvs: list[list[str]] = []
        self.kwargs: list[dict[str, Any]] = []

    def __call__(self, argv: list[str], **kwargs: Any) -> int:
        self.argvs.append(argv)
        self.kwargs.append(kwargs)
        if self.exit_code != 0:
            return self.exit_code  # dies without leaving a meta
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("[]")
        events = self.runs.pop(0)
        out.with_name("findings.events.jsonl").write_text(
            "".join(e.model_dump_json() + "\n" for e in events)
        )
        meta = {"stop_reason": StopReason.FINISH.value, "cost_usd": self.cost}
        out.with_name("findings.meta.json").write_text(json.dumps(meta))
        return 0


class FakeSandbox:
    def __init__(self, result: ExecResult) -> None:
        self.result = result
        self.closed = False

    def exec(self, argv: Any, *, timeout_s: int) -> ExecResult:
        return self.result

    def close(self) -> None:
        self.closed = True


class FakeFactory:
    def __init__(self, result: ExecResult | None = None, error: Exception | None = None) -> None:
        self.result = result or ExecResult("app.py\n", "", 0)
        self.error = error
        self.made: list[FakeSandbox] = []

    def __call__(self, task: Task) -> FakeSandbox:
        if self.error:
            raise self.error
        self.made.append(FakeSandbox(self.result))
        return self.made[-1]


def run(
    agent: FakeAgent,
    tmp_path: Path,
    *extra: str,
    factory: Any = None,
    preflight: Any = lambda: None,
) -> int:
    return live_run.main(
        list(extra),
        preflight_check=preflight,
        agent_main=agent,
        sandbox_factory=factory or FakeFactory(),
        runs_dir=tmp_path / "evals",
        now=NOW,
    )


def test_a_successful_run_command_ends_it_after_one_run(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = FakeAgent([other_event(), run_command_event()])
    factory = FakeFactory()
    assert run(agent, tmp_path, factory=factory) == live_run.EXIT_OK
    assert len(agent.argvs) == 1
    assert agent.kwargs[0]["sandbox_factory"] is factory
    argv = agent.argvs[0]
    assert argv[argv.index("--max-cost-usd") + 1] == "0.5"
    out = capsys.readouterr().out
    assert "run_command ok/total=1/1" in out
    assert "ledger_api-sandbox-20261026T120000Z" in out


def test_the_check_sandbox_is_closed_and_the_ledger_is_written(tmp_path: Path) -> None:
    factory = FakeFactory()
    run(FakeAgent([run_command_event()]), tmp_path, factory=factory)
    assert [s.closed for s in factory.made] == [
        True
    ]  # the throwaway check; the fake agent made none
    spend = json.loads((tmp_path / "evals" / "spend.json").read_text())
    assert spend["total_usd"] == pytest.approx(0.01)


def test_one_extra_run_is_allowed_when_run_command_was_not_used(tmp_path: Path) -> None:
    agent = FakeAgent([other_event()], [run_command_event()])
    assert run(agent, tmp_path) == live_run.EXIT_OK
    assert len(agent.argvs) == 2


def test_it_stops_after_two_runs_without_a_successful_run_command(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = FakeAgent([other_event()], [run_command_event(ok=False, error="exit code 2")], [])
    assert run(agent, tmp_path) == live_run.EXIT_NO_COMMAND
    assert len(agent.argvs) == 2  # the third list is never used
    assert "no run_command call succeeded in 2 run(s)" in capsys.readouterr().err


def test_attempts_1_means_one_run(tmp_path: Path) -> None:
    agent = FakeAgent([other_event()], [run_command_event()])
    assert run(agent, tmp_path, "--attempts", "1") == live_run.EXIT_NO_COMMAND
    assert len(agent.argvs) == 1


def test_more_than_two_attempts_is_refused(tmp_path: Path) -> None:
    with pytest.raises(SystemExit):
        run(FakeAgent(), tmp_path, "--attempts", "3")


def test_a_refused_exec_is_counted_and_does_not_count_as_success(tmp_path: Path) -> None:
    events = [run_command_event(ok=False, error="sandbox unavailable: no sandbox is available")]
    path = tmp_path / "events.jsonl"
    path.write_text("".join(e.model_dump_json() + "\n" for e in events))
    assert live_run.command_stats(path) == live_run.CommandStats(total=1, ok=0, refused=1)


def test_an_unreadable_event_log_counts_as_no_calls(tmp_path: Path) -> None:
    assert live_run.command_stats(tmp_path / "missing.jsonl") == live_run.CommandStats()
    bad = tmp_path / "bad.jsonl"
    bad.write_text("not json\n")
    assert live_run.command_stats(bad) == live_run.CommandStats()


def test_a_broken_sandbox_stops_it_before_the_agent_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    agent = FakeAgent()
    factory = FakeFactory(error=SandboxUnavailableError(DEFAULT_CONFIG.build_command))
    assert run(agent, tmp_path, factory=factory) == live_run.EXIT_ERROR
    assert agent.argvs == []
    assert DEFAULT_CONFIG.build_command in capsys.readouterr().err


def test_an_unreadable_target_stops_it_before_the_agent_starts(tmp_path: Path) -> None:
    agent = FakeAgent()
    factory = FakeFactory(result=ExecResult("", "ls: cannot open directory", 2))
    assert run(agent, tmp_path, factory=factory) == live_run.EXIT_ERROR
    assert agent.argvs == []
    assert [s.closed for s in factory.made] == [True]


def test_a_failed_preflight_stops_it_before_anything_starts(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    def failing() -> None:
        raise live_run.PreflightError("preflight failed: key missing")

    agent = FakeAgent()
    factory = FakeFactory()
    assert run(agent, tmp_path, factory=factory, preflight=failing) == live_run.EXIT_ERROR
    assert agent.argvs == [] and factory.made == []
    assert "key missing" in capsys.readouterr().err


def test_the_budget_guard_refuses_a_run_that_could_pass_it(tmp_path: Path) -> None:
    earlier = tmp_path / "evals" / "old" / "run1"
    earlier.mkdir(parents=True)
    (earlier / "findings.meta.json").write_text(json.dumps({"cost_usd": 0.9}))
    agent = FakeAgent()
    assert run(agent, tmp_path) == live_run.EXIT_ERROR  # 0.9 spent + 0.5 cap > 1.00 budget
    assert agent.argvs == []


def test_an_agent_that_dies_is_an_error_and_is_not_retried(tmp_path: Path) -> None:
    agent = FakeAgent(exit_code=2)
    assert run(agent, tmp_path) == live_run.EXIT_ERROR
    assert len(agent.argvs) == 1


def test_an_unknown_target_is_an_error(tmp_path: Path) -> None:
    agent = FakeAgent()
    assert run(agent, tmp_path, "--target", "nope") == live_run.EXIT_ERROR
    assert agent.argvs == []


@pytest.mark.docker
def test_the_real_factory_runs_a_command_through_the_runner(
    tmp_path: Path, docker_client: Any
) -> None:
    """No model: a fake agent uses the factory it is handed to run `echo` in a real container."""

    def agent(argv: list[str], *, sandbox_factory: Any, **_: Any) -> int:
        target = Path(argv[argv.index("--target") + 1])
        task = Task(
            task_id="live-run-docker-test",
            target_path=str(target),
            goal="g",
            max_steps=1,
            timeout_s=60,
            model="m",
        )
        sandbox = sandbox_factory(task)
        try:
            result = sandbox.exec(["echo", "hi"], timeout_s=10)
        finally:
            sandbox.close()
        out = Path(argv[argv.index("--out") + 1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text("[]")
        event = run_command_event(ok=result.exit_code == 0 and result.stdout == "hi\n")
        out.with_name("findings.events.jsonl").write_text(event.model_dump_json() + "\n")
        out.with_name("findings.meta.json").write_text(
            json.dumps({"stop_reason": "finish", "cost_usd": 0.0})
        )
        return 0

    code = live_run.main(
        [],
        preflight_check=lambda: None,
        agent_main=agent,
        sandbox_factory=create_sandbox,
        runs_dir=tmp_path / "evals",
        now=NOW,
    )
    assert code == live_run.EXIT_OK
