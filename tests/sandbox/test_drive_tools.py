"""The $0 driver: run_command through the real ToolRegistry and DockerSandbox."""

from typing import Any

import pytest

from src.agent.tools.registry import ToolRegistry
from src.sandbox import drive_tools
from src.sandbox.config import LABEL
from tests.sandbox.conftest import TARGET, SandboxMaker


def execute(registry: ToolRegistry, argv: list[str]) -> Any:
    return registry.execute("t1", "run_command", {"argv": argv, "timeout_s": 20}, timeout_s=120)


@pytest.mark.docker
def test_the_driver_commands_run_for_real(make_sandbox: SandboxMaker) -> None:
    sandbox = make_sandbox()
    calls = drive_tools.drive(ToolRegistry(TARGET.resolve(), sandbox))
    grep, script = calls
    assert grep.result.ok and grep.result.exit_code == 0
    assert "services/shell.py" in grep.result.output and "shell=True" in grep.result.output
    assert script.result.ok and script.result.exit_code == 0
    assert "services/shell.py 2" in script.result.output.replace("\\", "/")  # real AST count


@pytest.mark.docker
def test_main_prints_argv_exit_code_and_output_and_cleans_up(
    capsys: pytest.CaptureFixture[str], docker_client: Any
) -> None:
    assert drive_tools.main() == 0
    out = capsys.readouterr().out
    assert "$ grep -rn shell=True ." in out
    assert "$ python3 -c <script>" in out
    assert out.count("ok=True exit_code=0") == 2
    assert "services/shell.py" in out
    flt = {"label": LABEL}
    labels = [c.labels for c in docker_client.containers.list(all=True, filters=flt)]
    labels += [v.attrs.get("Labels") or {} for v in docker_client.volumes.list(filters=flt)]
    assert [x for x in labels if x.get(LABEL, "").startswith("drive-tools-")] == []


@pytest.mark.docker
def test_a_write_to_the_target_through_run_command_fails(make_sandbox: SandboxMaker) -> None:
    registry = ToolRegistry(TARGET.resolve(), make_sandbox())
    result = execute(registry, ["touch", "/work/new_file"])
    assert not result.ok and result.exit_code not in (None, 0)
    assert not (TARGET / "new_file").exists()
