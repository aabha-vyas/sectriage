"""DockerSandbox: unit tests on a fake client, then `@pytest.mark.docker` tests on a real one."""

import shutil
import subprocess
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import docker
import pytest
from docker.errors import APIError, ImageNotFound, NotFound
from requests.exceptions import ConnectionError as RequestsConnectionError

import src.sandbox.docker_sandbox as ds
from src.agent.sandbox_protocol import SandboxUnavailableError
from src.sandbox.config import DEFAULT_CONFIG, SandboxConfig
from src.sandbox.docker_sandbox import DockerSandbox, create_sandbox
from tests.sandbox.conftest import REPO, TARGET, SandboxMaker, leftovers, make_task

# --- fake client -------------------------------------------------------------------------------


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeApi:
    def __init__(self, clock: FakeClock) -> None:
        self.clock = clock
        self.calls: list[tuple[list[str], dict[str, Any]]] = []
        self.chunks: list[tuple[bytes | None, bytes | None]] = [(b"", None)]
        self.exit_code: int | None = 0
        self.advance = 0.0  # fake seconds an exec "takes"
        self.block: threading.Event | None = None
        self.exec_create_error: Exception | None = None
        self.remove_container_errors: list[Exception] = []
        self.removed_containers: list[str] = []
        self.removed_volumes: list[str] = []

    def exec_create(self, container: str, cmd: list[str], **kw: Any) -> dict[str, str]:
        if self.exec_create_error:
            raise self.exec_create_error
        self.calls.append((cmd, kw))
        return {"Id": "e1"}

    def exec_start(self, exec_id: str, *, stream: bool, demux: bool) -> Any:
        def chunks() -> Any:
            if self.block:
                self.block.wait(10)
            self.clock.now += self.advance
            yield from self.chunks

        return chunks()

    def exec_inspect(self, exec_id: str) -> dict[str, Any]:
        return {"ExitCode": self.exit_code}

    def remove_container(self, cid: str, force: bool = False) -> None:
        if self.remove_container_errors:
            raise self.remove_container_errors.pop(0)
        self.removed_containers.append(cid)

    def remove_volume(self, name: str, force: bool = False) -> None:
        self.removed_volumes.append(name)


class FakeContainer:
    id = "c1"

    def __init__(self, start_error: Exception | None = None) -> None:
        self.start_error = start_error

    def start(self) -> None:
        if self.start_error:
            raise self.start_error


class FakeClient:
    def __init__(self, clock: FakeClock) -> None:
        self.api = FakeApi(clock)
        self.created_containers: list[dict[str, Any]] = []
        self.created_volumes: list[dict[str, Any]] = []
        self.image_error: Exception | None = None
        self.ping_error: Exception | None = None
        self.create_error: Exception | None = None
        self.start_error: Exception | None = None
        self.closed = False
        self.containers = SimpleNamespace(create=self._create_container)
        self.volumes = SimpleNamespace(create=lambda **kw: self.created_volumes.append(kw))
        self.images = SimpleNamespace(get=self._get_image)

    def ping(self) -> bool:
        if self.ping_error:
            raise self.ping_error
        return True

    def close(self) -> None:
        self.closed = True

    def _get_image(self, name: str) -> object:
        if self.image_error:
            raise self.image_error
        return object()

    def _create_container(self, **kw: Any) -> FakeContainer:
        if self.create_error:
            raise self.create_error
        self.created_containers.append(kw)
        return FakeContainer(self.start_error)


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def client(clock: FakeClock) -> FakeClient:
    return FakeClient(clock)


@pytest.fixture
def sandbox(client: FakeClient, clock: FakeClock, tmp_path: Path) -> DockerSandbox:
    return create_sandbox(make_task(tmp_path, timeout_s=100), client=client, clock=clock)


def test_exec_runs_argv_under_timeout_as_the_sandbox_user(
    sandbox: DockerSandbox, client: FakeClient, clock: FakeClock
) -> None:
    client.api.chunks = [(b"hi\n", None), (None, b"warn\n")]
    client.api.advance = 0.25
    result = sandbox.exec(["ls", "-la"], timeout_s=5)
    cmd, kw = client.api.calls[0]
    assert cmd == ["timeout", "-s", "KILL", "5", "ls", "-la"]
    assert (kw["user"], kw["workdir"]) == ("10001:10001", "/work")
    assert (result.stdout, result.stderr, result.exit_code) == ("hi\n", "warn\n", 0)
    assert (result.timed_out, result.duration_ms) == (False, 250)


def test_exec_limit_is_the_smaller_of_call_timeout_and_remaining_task_time(
    sandbox: DockerSandbox, client: FakeClient, clock: FakeClock
) -> None:
    sandbox.exec(["a"], timeout_s=30)  # 100s left: the call's own timeout wins
    clock.now += 95
    sandbox.exec(["b"], timeout_s=30)  # 5s left: the task wins
    clock.now += 50
    sandbox.exec(["c"], timeout_s=30)  # overdue: still at least 1s
    limits = [cmd[3] for cmd, _ in client.api.calls]
    assert limits == ["30", "5", "1"]


def test_exit_137_after_the_limit_is_a_timeout(sandbox: DockerSandbox, client: FakeClient) -> None:
    client.api.exit_code = 137
    client.api.advance = 5.0
    result = sandbox.exec(["sleep", "300"], timeout_s=5)
    assert (result.timed_out, result.exit_code) == (True, None)
    client.api.advance = 0.0  # the sandbox stays usable
    assert sandbox.exec(["true"], timeout_s=5).exit_code == 137


def test_exit_137_before_the_limit_is_an_ordinary_exit(
    sandbox: DockerSandbox, client: FakeClient
) -> None:
    client.api.exit_code = 137
    client.api.advance = 0.5
    result = sandbox.exec(["python3", "hog.py"], timeout_s=5)
    assert (result.timed_out, result.exit_code) == (False, 137)


def test_output_is_capped_per_stream_and_still_drained(
    client: FakeClient, clock: FakeClock, tmp_path: Path
) -> None:
    cfg = SandboxConfig(output_cap_bytes=10)
    sb = create_sandbox(make_task(tmp_path), client=client, config=cfg, clock=clock)
    client.api.chunks = [(b"x" * 8, b"e" * 3), (b"y" * 8, None), (b"z" * 100, None)]
    result = sb.exec(["noisy"], timeout_s=5)
    assert result.stdout == "xxxxxxxxyy\n[output truncated at 10 bytes]"
    assert result.stderr == "eee"


def test_host_deadline_backstop_breaks_the_sandbox(
    client: FakeClient, clock: FakeClock, tmp_path: Path
) -> None:
    cfg = SandboxConfig(host_grace_s=0.1)
    sb = create_sandbox(make_task(tmp_path), client=client, config=cfg, clock=clock)
    client.api.block = threading.Event()
    client.api.chunks = [(b"partial", None)]
    try:
        start = time.monotonic()
        result = sb.exec(["sleep", "300"], timeout_s=1)
        assert time.monotonic() - start < 5
    finally:
        client.api.block.set()
    assert (result.timed_out, result.exit_code) == (True, None)
    assert client.api.removed_containers == ["c1"]
    assert client.api.removed_volumes == [client.created_volumes[0]["name"]]
    with pytest.raises(SandboxUnavailableError, match="broken"):
        sb.exec(["true"], timeout_s=1)
    sb.close()
    assert client.api.removed_containers == ["c1"]  # nothing removed twice


def test_docker_error_during_exec_breaks_and_cleans_up(
    sandbox: DockerSandbox, client: FakeClient
) -> None:
    client.api.exec_create_error = APIError("boom")
    with pytest.raises(SandboxUnavailableError, match="APIError"):
        sandbox.exec(["true"], timeout_s=5)
    assert client.api.removed_containers == ["c1"]
    assert len(client.api.removed_volumes) == 1
    with pytest.raises(SandboxUnavailableError, match="broken"):
        sandbox.exec(["true"], timeout_s=5)
    sandbox.close()


def test_close_is_idempotent_and_ends_exec(sandbox: DockerSandbox, client: FakeClient) -> None:
    sandbox.close()
    sandbox.close()
    assert client.api.removed_containers == ["c1"]
    assert len(client.api.removed_volumes) == 1
    with pytest.raises(SandboxUnavailableError, match="closed"):
        sandbox.exec(["true"], timeout_s=5)
    assert client.closed is False  # a client the caller passed in is the caller's to close


def test_close_tolerates_already_gone_and_waits_out_a_removal_in_progress(
    sandbox: DockerSandbox, client: FakeClient, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(ds, "REMOVE_PAUSE_S", 0)
    client.api.remove_container_errors = [APIError("removal in progress"), NotFound("gone")]
    sandbox.close()
    assert len(client.api.removed_volumes) == 1


def test_factory_mounts_the_resolved_target_read_only(
    client: FakeClient, clock: FakeClock, tmp_path: Path
) -> None:
    target = tmp_path / "dir with space"
    target.mkdir()
    create_sandbox(make_task(target), client=client, clock=clock)
    (kwargs,) = client.created_containers
    work = next(m for m in kwargs["mounts"] if m["Target"] == "/work")
    assert (work["Source"], work["ReadOnly"]) == (str(target.resolve()), True)
    assert kwargs["network_mode"] == "none"
    assert client.created_volumes[0]["name"] == kwargs["mounts"][1]["Source"]


def test_factory_refuses_a_target_that_is_not_a_directory(
    client: FakeClient, tmp_path: Path
) -> None:
    (tmp_path / "f.txt").write_text("x")
    for bad in (tmp_path / "missing", tmp_path / "f.txt"):
        with pytest.raises(SandboxUnavailableError, match="existing directory"):
            create_sandbox(make_task(bad), client=client)
    assert client.created_containers == client.created_volumes == []


def test_factory_missing_image_says_how_to_build_it(client: FakeClient, tmp_path: Path) -> None:
    client.image_error = ImageNotFound("no such image")
    with pytest.raises(SandboxUnavailableError) as e:
        create_sandbox(make_task(tmp_path), client=client)
    assert DEFAULT_CONFIG.build_command in str(e.value)
    assert client.created_volumes == []


def test_factory_unreachable_daemon(client: FakeClient, tmp_path: Path) -> None:
    client.ping_error = RequestsConnectionError("no socket")
    with pytest.raises(SandboxUnavailableError, match="not reachable"):
        create_sandbox(make_task(tmp_path), client=client)


@pytest.mark.parametrize("fail", ["create", "start"])
def test_failed_factory_removes_what_it_made(fail: str, client: FakeClient, tmp_path: Path) -> None:
    if fail == "create":
        client.create_error = APIError("no space")
    else:
        client.start_error = APIError("cannot start")
    with pytest.raises(SandboxUnavailableError, match="could not start"):
        create_sandbox(make_task(tmp_path), client=client)
    assert client.api.removed_volumes == [client.created_volumes[0]["name"]]
    assert client.api.removed_containers == (["c1"] if fail == "start" else [])


# --- real Docker -------------------------------------------------------------------------------

LIST_SLEEPS = (
    "import os\n"
    "n = 0\n"
    "for p in os.listdir('/proc'):\n"
    "    try:\n"
    "        argv = open(f'/proc/{p}/cmdline', 'rb').read().split(b'\\0')[:2]\n"
    "    except OSError:\n"
    "        continue\n"
    "    n += argv in ([b'sleep', b'300'], [b'sleep', b'301'])\n"
    "print(n)\n"
)
MEMORY_HOG = "b = bytearray()\nwhile True:\n    b.extend(bytes(1 << 20))\n"


def sh(sb: DockerSandbox, script: str, *, timeout_s: int = 20) -> Any:
    return sb.exec(["sh", "-c", script], timeout_s=timeout_s)


def dotenv_values() -> dict[str, str]:
    env_file = REPO / ".env"
    if not env_file.is_file():
        return {}
    values: dict[str, str] = {}
    for line in env_file.read_text().splitlines():
        key, eq, value = line.strip().partition("=")
        if eq and not key.startswith("#") and len(value.strip("'\"")) >= 4:
            values[key] = value.strip("'\"")
    return values


@pytest.mark.docker
class TestDocker:
    def test_network_is_unreachable(self, make_sandbox: SandboxMaker) -> None:
        sb = make_sandbox()
        dial = "import socket; socket.create_connection(('1.1.1.1', 53), timeout=3)"
        assert sb.exec(["python3", "-c", dial], timeout_s=20).exit_code != 0
        devices = sb.exec(["cat", "/proc/net/dev"], timeout_s=5).stdout
        assert "eth" not in devices  # no interface to the outside, only loopback and kernel stubs
        assert sb.exec(["cat", "/proc/net/route"], timeout_s=5).stdout.count("\n") == 1  # no routes

    def test_work_and_root_are_read_only_scratch_is_writable(
        self, make_sandbox: SandboxMaker
    ) -> None:
        sb = make_sandbox()
        assert sb.exec(["touch", "/work/new"], timeout_s=5).exit_code != 0
        assert sb.exec(["touch", "/etc/new"], timeout_s=5).exit_code != 0
        assert sh(sb, "echo hi > /scratch/f && cat /scratch/f").stdout == "hi\n"
        assert sb.exec(["cat", "/scratch/f"], timeout_s=5).stdout == "hi\n"  # persists across execs

    def test_runs_as_a_non_root_user(self, make_sandbox: SandboxMaker) -> None:
        sb = make_sandbox()
        assert sb.exec(["id", "-u"], timeout_s=5).stdout.strip() == "10001"
        assert sb.exec(["id", "-g"], timeout_s=5).stdout.strip() == "10001"

    def test_target_files_are_readable_by_the_sandbox_user(
        self, make_sandbox: SandboxMaker
    ) -> None:
        sb = make_sandbox()
        result = sb.exec(["cat", "app.py"], timeout_s=5)
        assert result.exit_code == 0
        assert result.stdout == (TARGET / "app.py").read_text()
        assert sb.exec(["pwd"], timeout_s=5).stdout.strip() == "/work"

    def test_target_path_with_a_space(self, make_sandbox: SandboxMaker, tmp_path: Path) -> None:
        target = tmp_path / "dir with a space"
        target.mkdir()
        (target / "hello.txt").write_text("hello\n")
        target.chmod(0o755)
        (target / "hello.txt").chmod(0o644)
        sb = make_sandbox(target)
        assert sb.exec(["cat", "hello.txt"], timeout_s=5).stdout == "hello\n"
        assert sb.exec(["touch", "x"], timeout_s=5).exit_code != 0

    def test_argv_is_not_run_through_a_shell(self, make_sandbox: SandboxMaker) -> None:
        sb = make_sandbox()
        assert sb.exec(["echo", "$HOME;id"], timeout_s=5).stdout == "$HOME;id\n"

    def test_host_secrets_are_not_in_the_container(
        self, make_sandbox: SandboxMaker, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        canary = "sk-ant-canary-0123456789abcdef"
        monkeypatch.setenv("ANTHROPIC_API_KEY", canary)
        secrets = {"ANTHROPIC_API_KEY": canary, **dotenv_values()}
        sb = make_sandbox()
        seen = sh(sb, "env; tr '\\0' '\\n' < /proc/1/environ").stdout
        assert seen  # the command ran
        assert "ANTHROPIC_API_KEY" not in seen
        leaked = [name for name, value in secrets.items() if value in seen]  # names only
        assert leaked == []

    def test_in_container_timeout_kills_the_tree_and_the_sandbox_stays_usable(
        self, make_sandbox: SandboxMaker
    ) -> None:
        sb = make_sandbox()
        start = time.monotonic()
        result = sh(sb, "sleep 300 & sleep 301", timeout_s=2)
        assert time.monotonic() - start < 10
        assert (result.timed_out, result.exit_code) == (True, None)
        assert sb.exec(["python3", "-c", LIST_SLEEPS], timeout_s=5).stdout.strip() == "0"
        assert sb.exec(["echo", "still here"], timeout_s=5).stdout == "still here\n"

    def test_exec_is_limited_by_the_remaining_task_time(self, make_sandbox: SandboxMaker) -> None:
        sb = make_sandbox(timeout_s=3)
        start = time.monotonic()
        result = sb.exec(["sleep", "300"], timeout_s=300)
        assert result.timed_out
        assert time.monotonic() - start < 10

    def test_memory_hog_is_killed_and_the_sandbox_stays_usable(
        self, make_sandbox: SandboxMaker
    ) -> None:
        sb = make_sandbox()
        result = sb.exec(["python3", "-c", MEMORY_HOG], timeout_s=30)
        assert (result.timed_out, result.exit_code) == (False, 137)
        assert result.duration_ms < 30_000
        assert sb.exec(["echo", "ok"], timeout_s=5).stdout == "ok\n"

    def test_runaway_output_is_capped(self, make_sandbox: SandboxMaker) -> None:
        sb = make_sandbox()
        spew = "import sys; sys.stdout.write('x' * (3 << 20))"
        result = sb.exec(["python3", "-c", spew], timeout_s=30)
        assert result.exit_code == 0
        assert len(result.stdout) < DEFAULT_CONFIG.output_cap_bytes + 100
        assert "[output truncated" in result.stdout
        assert sb.exec(["echo", "ok"], timeout_s=5).stdout == "ok\n"

    @pytest.mark.parametrize("path", ["/scratch/big", "/tmp/big"])
    def test_writable_areas_have_a_size_cap(self, make_sandbox: SandboxMaker, path: str) -> None:
        sb = make_sandbox()
        result = sh(sb, f"head -c 300M /dev/zero > {path}")
        assert result.exit_code != 0
        assert "No space left" in result.stderr

    def test_git_runs_and_can_init_in_scratch(self, make_sandbox: SandboxMaker) -> None:
        sb = make_sandbox()
        version = sb.exec(["git", "--version"], timeout_s=5)
        assert (version.exit_code, version.stdout.startswith("git version")) == (0, True)
        assert sb.exec(["git", "-C", "/scratch", "init"], timeout_s=10).exit_code == 0

    @pytest.mark.skipif(shutil.which("git") is None, reason="needs git on the host")
    def test_git_reads_a_read_only_repo_owned_by_someone_else(
        self, make_sandbox: SandboxMaker, tmp_path: Path
    ) -> None:
        repo = tmp_path / "repo"
        repo.mkdir()
        (repo / "a.txt").write_text("a\n")
        who = ["-c", "user.name=t", "-c", "user.email=t@example.com"]
        for cmd in (["init", "-q"], ["add", "."], [*who, "commit", "-q", "-m", "first"]):
            subprocess.run(["git", "-C", str(repo), *cmd], check=True, capture_output=True)
        repo.chmod(0o755)
        sb = make_sandbox(repo)
        log = sb.exec(["git", "--no-pager", "log", "--oneline"], timeout_s=10)
        assert (log.exit_code, "first" in log.stdout) == (0, True)
        assert sb.exec(["git", "--no-pager", "status", "--short"], timeout_s=10).exit_code == 0

    def test_everything_is_gone_after_close_and_close_twice_is_fine(
        self, make_sandbox: SandboxMaker, docker_client: docker.DockerClient
    ) -> None:
        sb = make_sandbox()
        assert leftovers(docker_client, sb.task_id, wait_s=0) != []  # it exists while open
        sb.close()
        assert leftovers(docker_client, sb.task_id) == []
        sb.close()
        with pytest.raises(SandboxUnavailableError):
            sb.exec(["true"], timeout_s=5)

    def test_everything_is_gone_after_an_exec_error(
        self, make_sandbox: SandboxMaker, docker_client: docker.DockerClient
    ) -> None:
        sb = make_sandbox()
        docker_client.api.remove_container(sb._container_id, force=True)  # pull it out from under
        with pytest.raises(SandboxUnavailableError):
            sb.exec(["true"], timeout_s=5)
        assert leftovers(docker_client, sb.task_id) == []
        with pytest.raises(SandboxUnavailableError, match="broken"):
            sb.exec(["true"], timeout_s=5)

    def test_everything_is_gone_after_a_host_deadline_timeout(
        self, make_sandbox: SandboxMaker, docker_client: docker.DockerClient
    ) -> None:
        sb = make_sandbox(config=SandboxConfig(host_grace_s=1))
        # `setsid` moves a sleeper out of timeout's process group; it keeps the output pipe open.
        start = time.monotonic()
        result = sh(sb, "setsid sleep 300 & sleep 300", timeout_s=2)
        assert time.monotonic() - start < 15
        assert result.timed_out
        assert leftovers(docker_client, sb.task_id) == []
        with pytest.raises(SandboxUnavailableError, match="broken"):
            sb.exec(["true"], timeout_s=5)

    def test_create_sandbox_refuses_when_the_image_is_missing(
        self, docker_client: docker.DockerClient, tmp_path: Path
    ) -> None:
        cfg = SandboxConfig(image="sectriage-sandbox:does-not-exist")
        task = make_task(tmp_path)
        with pytest.raises(SandboxUnavailableError, match="docker build"):
            create_sandbox(task, client=docker_client, config=cfg)
        assert leftovers(docker_client, task.task_id, wait_s=0) == []
