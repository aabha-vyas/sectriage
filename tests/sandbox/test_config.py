"""The isolation settings, checked without a Docker daemon."""

from pathlib import Path

import pytest

from src.sandbox.config import (
    DEFAULT_CONFIG,
    IMAGE,
    LABEL,
    SCRATCH,
    WORKDIR,
    SandboxConfig,
    container_kwargs,
    volume_kwargs,
)
from tests.sandbox.conftest import make_task


@pytest.fixture
def kwargs(tmp_path: Path) -> dict:
    root = tmp_path / "a target with spaces"
    root.mkdir()
    task = make_task(root, timeout_s=120, task_id="t-1")
    return container_kwargs(task, root, DEFAULT_CONFIG, name="sectriage-x", volume="vol-x")


def test_isolation_flags(kwargs: dict) -> None:
    assert kwargs["network_mode"] == "none"
    assert kwargs["read_only"] is True
    assert kwargs["cap_drop"] == ["ALL"]
    assert kwargs["security_opt"] == ["no-new-privileges"]
    assert kwargs["privileged"] is False
    assert kwargs["user"] == "10001:10001"
    assert kwargs["working_dir"] == WORKDIR
    assert kwargs["auto_remove"] is True
    assert kwargs["init"] is True
    assert kwargs["labels"] == {LABEL: "t-1"}
    assert kwargs["image"] == IMAGE


def test_limits(kwargs: dict) -> None:
    assert kwargs["nano_cpus"] == 1_000_000_000
    assert kwargs["mem_limit"] == "512m"
    assert kwargs["memswap_limit"] == kwargs["mem_limit"]
    assert kwargs["pids_limit"] == 128
    assert kwargs["tmpfs"] == {"/tmp": "rw,nosuid,nodev,size=64m"}


def test_self_destruct_outlives_the_task_by_a_minute(kwargs: dict) -> None:
    assert kwargs["command"] == ["sleep", "180"]


def test_mounts_are_the_target_read_only_and_the_scratch_volume(
    kwargs: dict, tmp_path: Path
) -> None:
    by_target = {m["Target"]: m for m in kwargs["mounts"]}
    assert set(by_target) == {WORKDIR, SCRATCH}
    work, scratch = by_target[WORKDIR], by_target[SCRATCH]
    assert (work["Type"], work["ReadOnly"]) == ("bind", True)
    assert work["Source"] == str(tmp_path / "a target with spaces")
    assert (scratch["Type"], scratch["Source"], scratch["ReadOnly"]) == ("volume", "vol-x", False)
    assert not any("docker.sock" in m["Source"] for m in kwargs["mounts"])


def test_environment_is_explicit_and_carries_no_secrets(kwargs: dict) -> None:
    env = kwargs["environment"]
    assert env["HOME"] == env["TMPDIR"] == SCRATCH
    assert not any("KEY" in k or "TOKEN" in k or "SECRET" in k for k in env)


def test_scratch_volume_is_named_labelled_and_size_capped() -> None:
    task = make_task(Path("."), task_id="t-2")
    v = volume_kwargs(task, DEFAULT_CONFIG, "vol-y")
    assert v["name"] == "vol-y"
    assert v["labels"] == {LABEL: "t-2"}
    assert v["driver_opts"]["type"] == "tmpfs"
    assert "size=256m" in v["driver_opts"]["o"]
    assert "uid=10001" in v["driver_opts"]["o"]


def test_config_values_flow_through(tmp_path: Path) -> None:
    cfg = SandboxConfig(cpus=0.5, memory_mib=256, pids_limit=32)
    k = container_kwargs(make_task(tmp_path), tmp_path, cfg, name="n", volume="v")
    assert (k["nano_cpus"], k["mem_limit"], k["pids_limit"]) == (500_000_000, "256m", 32)


def test_build_command_names_the_image() -> None:
    assert DEFAULT_CONFIG.build_command == "docker build -t sectriage-sandbox:1 src/sandbox"
