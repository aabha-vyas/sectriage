"""Fixtures for the sandbox tests. Docker tests skip when the daemon or the image is missing."""

import time
import uuid
from collections.abc import Callable, Iterator
from pathlib import Path

import docker
import pytest
from docker.errors import DockerException
from requests.exceptions import RequestException

from src.contracts import Task
from src.sandbox.config import DEFAULT_CONFIG, LABEL, SandboxConfig
from src.sandbox.docker_sandbox import DockerSandbox, create_sandbox

REPO = Path(__file__).resolve().parents[2]
TARGET = REPO / "evals" / "targets" / "ledger_api"


def make_task(target: Path = TARGET, *, timeout_s: int = 60, task_id: str | None = None) -> Task:
    return Task(
        task_id=task_id or f"test-{uuid.uuid4().hex[:10]}",
        target_path=str(target),
        goal="sandbox test",
        max_steps=1,
        timeout_s=timeout_s,
        model="test-model",
    )


def leftovers(client: docker.DockerClient, task_id: str, wait_s: float = 5.0) -> list[str]:
    """Containers and volumes still labelled for `task_id`, after giving removal a moment."""
    flt = {"label": f"{LABEL}={task_id}"}
    deadline = time.monotonic() + wait_s
    while True:
        found = [f"container {c.name}" for c in client.containers.list(all=True, filters=flt)]
        found += [f"volume {v.name}" for v in client.volumes.list(filters=flt)]
        if not found or time.monotonic() >= deadline:
            return found
        time.sleep(0.2)


@pytest.fixture(scope="session")
def docker_client() -> Iterator[docker.DockerClient]:
    try:
        client = docker.from_env()
        client.ping()
        client.images.get(DEFAULT_CONFIG.image)
    except DockerException as e:
        pytest.skip(f"docker or the sandbox image is not available: {type(e).__name__}")
    except RequestException:
        pytest.skip("docker daemon is not reachable")
    yield client
    client.close()


SandboxMaker = Callable[..., DockerSandbox]


@pytest.fixture
def make_sandbox(docker_client: docker.DockerClient) -> Iterator[SandboxMaker]:
    """Build real sandboxes; on teardown close them and assert nothing was left behind."""
    made: list[DockerSandbox] = []

    def make(
        target: Path = TARGET,
        *,
        timeout_s: int = 60,
        config: SandboxConfig = DEFAULT_CONFIG,
    ) -> DockerSandbox:
        task = make_task(target, timeout_s=timeout_s)
        sandbox = create_sandbox(task, client=docker_client, config=config)
        made.append(sandbox)
        return sandbox

    yield make
    for sandbox in made:
        sandbox.close()
    for sandbox in made:
        assert leftovers(docker_client, sandbox.task_id) == []
