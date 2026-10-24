from pathlib import Path

import pytest

from src.agent.paths import MAX_OUTPUT_BYTES, TRUNCATION_MARKER
from src.agent.sandbox_protocol import ExecResult, UnavailableSandbox
from src.agent.tools import host
from src.agent.tools.registry import ToolRegistry
from src.contracts import TOOL_ARGS, ToolName
from tests.agent.fakes import FakeSandbox


@pytest.fixture
def registry(target: Path) -> ToolRegistry:
    return ToolRegistry(target, FakeSandbox())


def test_schemas_cover_every_tool_in_a_fixed_order(registry: ToolRegistry):
    schemas = registry.schemas()
    assert [s["name"] for s in schemas] == [t.value for t in ToolName]
    for s in schemas:
        assert s["description"]
        assert s["input_schema"] == TOOL_ARGS[ToolName(s["name"])].model_json_schema()
    assert schemas == registry.schemas()


def test_execute_read_file(registry: ToolRegistry):
    r = registry.execute("c1", "read_file", {"path": "app.py", "end_line": 1}, timeout_s=60)
    assert r.ok
    assert r.call_id == "c1"
    assert r.output == "1: import os"
    assert r.error is None
    assert not r.truncated
    assert r.duration_ms >= 0


def test_execute_unknown_tool(registry: ToolRegistry):
    r = registry.execute("c1", "rm_rf", {}, timeout_s=60)
    assert not r.ok
    assert r.error is not None
    assert "unknown tool" in r.error
    assert "read_file" in r.error


@pytest.mark.parametrize("raw", [None, "app.py", ["app.py"], 3])
def test_execute_non_object_args(registry: ToolRegistry, raw: object):
    r = registry.execute("c1", "read_file", raw, timeout_s=60)
    assert not r.ok
    assert r.error == "invalid arguments: expected a JSON object"


@pytest.mark.parametrize(
    ("name", "raw"),
    [
        ("read_file", {}),
        ("read_file", {"path": 5}),
        ("read_file", {"path": "a", "start_line": 0}),
        ("read_file", {"path": "a", "start_line": 5, "end_line": 2}),
        ("read_file", {"path": "a", "extra": 1}),
        ("search_code", {"pattern": ["x"]}),
        ("run_command", {"argv": [], "timeout_s": 5}),
        ("run_command", {"argv": ["ls"], "timeout_s": 0}),
        ("semgrep", {"config": "x"}),
        ("git", {"subcommand": "log", "args": "--oneline"}),
    ],
)
def test_execute_invalid_args_never_raise(registry: ToolRegistry, name: str, raw: dict):
    r = registry.execute("c1", name, raw, timeout_s=60)
    assert not r.ok
    assert r.error is not None
    assert r.error.startswith("invalid arguments:")
    assert len(r.error) <= 500


def test_validation_errors_do_not_echo_input_values(registry: ToolRegistry):
    r = registry.execute(
        "c1", "read_file", {"path": "a", "start_line": "sk-secret-value"}, timeout_s=60
    )
    assert not r.ok
    assert r.error is not None
    assert "sk-secret-value" not in r.error
    assert "start_line" in r.error


def test_execute_traversal_is_rejected(registry: ToolRegistry):
    for call in [
        ("read_file", {"path": "../outside/secret.txt"}),
        ("read_file", {"path": "link_file"}),
        ("read_file", {"path": "link_dir/secret.txt"}),
        ("read_file", {"path": "/etc/passwd"}),
        ("search_code", {"pattern": "SECRET", "path": "link_dir"}),
        ("search_code", {"pattern": "SECRET", "path": "../outside"}),
    ]:
        r = registry.execute("c1", *call, timeout_s=60)
        assert not r.ok, call
        assert "TOP SECRET" not in r.output


def test_execute_truncates_large_output(registry: ToolRegistry):
    r = registry.execute("c1", "read_file", {"path": "big.txt"}, timeout_s=60)
    assert r.ok
    assert r.truncated
    assert len(r.output.encode()) <= MAX_OUTPUT_BYTES
    assert r.output.endswith(TRUNCATION_MARKER)


def test_execute_small_output_is_not_flagged_truncated(registry: ToolRegistry):
    assert not registry.execute("c1", "read_file", {"path": "app.py"}, timeout_s=60).truncated


def test_execute_search_truncation_flag_from_handler(target: Path, registry: ToolRegistry):
    (target / "many.txt").write_text("hit\n" * (host.MAX_MATCHES + 5))
    r = registry.execute("c1", "search_code", {"pattern": "hit", "path": "many.txt"}, timeout_s=60)
    assert r.ok
    assert r.truncated


def test_execute_sandboxed_tools_go_through_the_sandbox(target: Path):
    sb = FakeSandbox(ExecResult(stdout="out", stderr="", exit_code=0))
    reg = ToolRegistry(target, sb)
    r = reg.execute("c1", "run_command", {"argv": ["id"], "timeout_s": 9}, timeout_s=4)
    assert r.ok
    assert r.output == "out"
    assert sb.calls == [(["id"], 4)]


def test_execute_failure_result_keeps_output_and_exit_code(target: Path):
    sb = FakeSandbox(ExecResult(stdout="o", stderr="", exit_code=3))
    r = ToolRegistry(target, sb).execute(
        "c1", "run_command", {"argv": ["x"], "timeout_s": 9}, timeout_s=60
    )
    assert not r.ok
    assert r.error == "exit code 3"
    assert r.exit_code == 3
    assert r.output == "o"


def test_execute_without_a_sandbox_fails_closed(target: Path):
    reg = ToolRegistry(target, UnavailableSandbox())
    for name, raw in [
        ("run_command", {"argv": ["id"], "timeout_s": 5}),
        ("semgrep", {"config": "rules.yml", "path": "app.py"}),
        ("git", {"subcommand": "status"}),
    ]:
        r = reg.execute("c1", name, raw, timeout_s=60)
        assert not r.ok
        assert r.error is not None
        assert "sandbox unavailable" in r.error


def test_execute_git_rejection_is_a_failed_result_not_an_exception(target: Path):
    sb = FakeSandbox()
    r = ToolRegistry(target, sb).execute(
        "c1", "git", {"subcommand": "log", "args": ["--out=x"]}, timeout_s=60
    )
    assert not r.ok
    assert sb.calls == []


def test_unexpected_handler_exception_becomes_a_result(
    target: Path, monkeypatch: pytest.MonkeyPatch
):
    def boom(*a: object, **k: object) -> None:
        raise RuntimeError(f"disk exploded at {target}")

    monkeypatch.setattr(host, "read_file", boom)
    r = ToolRegistry(target, FakeSandbox()).execute(
        "c1", "read_file", {"path": "app.py"}, timeout_s=60
    )
    assert not r.ok
    assert r.error == "internal error: RuntimeError"
    assert str(target) not in (r.error or "")
