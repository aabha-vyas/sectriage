import anthropic
import docker
import pytest


@pytest.fixture(autouse=True)
def _no_api_and_no_docker(monkeypatch: pytest.MonkeyPatch) -> None:
    """Swarm tests never call the API and never talk to Docker (in-process; spawned workers get a
    fake factory and no sandbox, so they cannot either)."""

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise RuntimeError("forbidden in swarm tests: the API or Docker")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", _forbidden)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _forbidden)
    monkeypatch.setattr(docker, "from_env", _forbidden)
    monkeypatch.setattr(docker.DockerClient, "__init__", _forbidden)
