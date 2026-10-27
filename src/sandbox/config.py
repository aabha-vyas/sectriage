"""Settings for the Docker sandbox: every limit and isolation flag in one place.

`container_kwargs` and `volume_kwargs` are pure, so tests can check the isolation settings without
a Docker daemon. `docker_sandbox` passes their results straight to the SDK.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Any

from docker.types import Mount

from src.contracts import Task

IMAGE = "sectriage-sandbox:1"
LABEL = "sectriage.task"
WORKDIR = "/work"
SCRATCH = "/scratch"


@dataclass(frozen=True)
class SandboxConfig:
    image: str = IMAGE
    uid: int = 10001
    gid: int = 10001
    cpus: float = 1.0
    memory_mib: int = 512  # swap is set equal to it, so there is none
    pids_limit: int = 128
    # Both are tmpfs and may count against memory_mib; filling them together is untested.
    tmp_mib: int = 64
    scratch_mib: int = 256
    output_cap_bytes: int = 1 << 20  # per stream, per exec
    host_grace_s: float = 5.0  # host deadline = in-container timeout + this
    self_destruct_slack_s: int = 60  # container sleeps task.timeout_s + this, then exits

    @property
    def build_command(self) -> str:
        return f"docker build -t {self.image} src/sandbox"


DEFAULT_CONFIG = SandboxConfig()


def _env() -> dict[str, str]:
    """The whole container environment. Nothing is inherited from the host."""
    return {
        "HOME": SCRATCH,
        "TMPDIR": SCRATCH,
        "LANG": "C.UTF-8",
        "GIT_OPTIONAL_LOCKS": "0",  # /work is read-only: let `git status` skip the index refresh
        "PYTHONDONTWRITEBYTECODE": "1",
    }


def volume_kwargs(task: Task, config: SandboxConfig, name: str) -> dict[str, Any]:
    """Arguments for `volumes.create`: a named volume capped in size by a tmpfs backing."""
    opts = f"size={config.scratch_mib}m,uid={config.uid},gid={config.gid},mode=0700"
    return {
        "name": name,
        "driver": "local",
        "driver_opts": {"type": "tmpfs", "device": "tmpfs", "o": opts},
        "labels": {LABEL: task.task_id},
    }


def container_kwargs(
    task: Task, root: Path, config: SandboxConfig, *, name: str, volume: str
) -> dict[str, Any]:
    """Arguments for `containers.create`. `root` must be the resolved target directory."""
    return {
        "image": config.image,
        "command": ["sleep", str(task.timeout_s + config.self_destruct_slack_s)],
        "name": name,
        "labels": {LABEL: task.task_id},
        "user": f"{config.uid}:{config.gid}",
        "working_dir": WORKDIR,
        "environment": _env(),
        "init": True,
        "auto_remove": True,
        "privileged": False,
        "network_mode": "none",
        "read_only": True,
        "cap_drop": ["ALL"],
        "security_opt": ["no-new-privileges"],
        "nano_cpus": int(config.cpus * 1_000_000_000),
        "mem_limit": f"{config.memory_mib}m",
        "memswap_limit": f"{config.memory_mib}m",
        "pids_limit": config.pids_limit,
        "tmpfs": {"/tmp": f"rw,nosuid,nodev,size={config.tmp_mib}m"},
        # SDK Mount objects, not "-v src:dst" strings: the repo path contains a space.
        "mounts": [
            Mount(target=WORKDIR, source=str(root), type="bind", read_only=True),
            Mount(target=SCRATCH, source=volume, type="volume", read_only=False),
        ],
    }
