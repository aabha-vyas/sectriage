"""A `Sandbox` backed by one Docker container per task.

The factory (`create_sandbox`) starts the container. Every `exec` runs argv directly, no shell,
under in-container `timeout -s KILL`; a host deadline just after it is the backstop, because a
`docker exec` timeout does not kill the process it started. See docs/sandbox-lane-plan.md.
"""

import logging
import threading
import time
import uuid
from collections.abc import Callable, Sequence
from typing import Any

import docker
from docker.errors import APIError, DockerException, ImageNotFound, NotFound
from requests.exceptions import RequestException

from src.agent.paths import PathError, resolve_root
from src.agent.sandbox_protocol import ExecResult, SandboxUnavailableError
from src.contracts import Task
from src.sandbox.config import (
    DEFAULT_CONFIG,
    WORKDIR,
    SandboxConfig,
    container_kwargs,
    volume_kwargs,
)

log = logging.getLogger(__name__)

SIGKILL_EXIT = 137  # what `timeout -s KILL` exits with, same as any other SIGKILL (OOM included)
REMOVE_TRIES = 15
REMOVE_PAUSE_S = 0.2


def _brief(e: BaseException) -> str:
    first = (str(e).splitlines() or [""])[0][:200]
    return f"{type(e).__name__}: {first}" if first else type(e).__name__


def _retry_remove(remove: Callable[[], Any], what: str) -> None:
    """Run `remove`, treating "already gone" as success and waiting out "removal in progress"."""
    for attempt in range(REMOVE_TRIES):
        try:
            remove()
            return
        except NotFound:
            return
        except APIError:  # 409: removal already in progress, or the volume is still in use
            if attempt == REMOVE_TRIES - 1:
                log.warning("could not remove %s", what)
                return
            time.sleep(REMOVE_PAUSE_S)
        except (DockerException, RequestException, OSError) as e:
            log.warning("could not remove %s: %s", what, _brief(e))
            return


def _remove_resources(client: Any, container_id: str | None, volume: str) -> None:
    if container_id:
        _retry_remove(
            lambda: client.api.remove_container(container_id, force=True),
            f"container {container_id[:12]}",
        )
    _retry_remove(lambda: client.api.remove_volume(volume, force=True), f"volume {volume}")


class _Capture:
    """Per-stream byte buffers that stop growing at the cap but keep accepting (draining) chunks."""

    def __init__(self, cap: int) -> None:
        self.cap = cap
        self.buffers = {"out": bytearray(), "err": bytearray()}
        self.cut: set[str] = set()

    def add(self, out: bytes | None, err: bytes | None) -> None:
        for name, chunk in (("out", out), ("err", err)):
            if not chunk:
                continue
            buf = self.buffers[name]
            room = self.cap - len(buf)
            if len(chunk) > room:
                self.cut.add(name)
            if room > 0:
                buf += chunk[:room]

    def text(self, name: str) -> str:
        s = bytes(self.buffers[name]).decode("utf-8", errors="replace")
        if name in self.cut:
            s += f"\n[output truncated at {self.cap} bytes]"
        return s


class DockerSandbox:
    def __init__(
        self,
        client: Any,
        container_id: str,
        volume: str,
        task: Task,
        config: SandboxConfig,
        *,
        clock: Callable[[], float] = time.monotonic,
        owns_client: bool = False,
    ) -> None:
        self.task_id = task.task_id
        self._client = client
        self._container_id = container_id
        self._volume = volume
        self._config = config
        self._clock = clock
        self._owns_client = owns_client
        self._deadline = clock() + task.timeout_s  # the task's own wall clock
        self._state = "ready"  # ready | broken | closed
        self._reason = ""
        self._removed = False
        self._lock = threading.Lock()

    def exec(self, argv: Sequence[str], *, timeout_s: int) -> ExecResult:
        if self._state != "ready":
            detail = f": {self._reason}" if self._reason else ""
            raise SandboxUnavailableError(f"sandbox is {self._state}{detail}")
        # min(per-call timeout, remaining task time); the tool layer clamps too, this is the guard.
        limit = max(1, min(timeout_s, int(self._deadline - self._clock())))
        cmd = ["timeout", "-s", "KILL", str(limit), *argv]
        capture = _Capture(self._config.output_cap_bytes)
        outcome: dict[str, Any] = {}
        worker = threading.Thread(
            target=self._read_exec, args=(cmd, capture, outcome), daemon=True, name="sandbox-exec"
        )
        start = self._clock()
        worker.start()
        worker.join(limit + self._config.host_grace_s)
        elapsed = self._clock() - start
        duration_ms = int(elapsed * 1000)
        stdout, stderr = capture.text("out"), capture.text("err")
        if worker.is_alive():  # in-container timeout did not end it: the container is suspect
            self._break(f"exec outlived its {limit}s limit")
            return ExecResult(stdout, stderr, None, timed_out=True, duration_ms=duration_ms)
        if "error" in outcome:
            self._break(f"docker error during exec ({outcome['error']})")
            raise SandboxUnavailableError(f"sandbox failed: {outcome['error']}") from None
        code = outcome["exit_code"]
        # `timeout -s KILL` exits 137, the same as an OOM kill; the elapsed time tells them apart.
        # The host clock started before the in-container one, so a real timeout never reads short.
        if code == SIGKILL_EXIT and elapsed >= limit:
            return ExecResult(stdout, stderr, None, timed_out=True, duration_ms=duration_ms)
        return ExecResult(stdout, stderr, code, duration_ms=duration_ms)

    def close(self) -> None:
        with self._lock:
            if self._state == "closed":
                return
            self._state = "closed"
            self._remove()
            if self._owns_client:
                try:
                    self._client.close()
                except Exception:  # best effort at teardown
                    log.debug("docker client close failed", exc_info=True)

    def _read_exec(self, cmd: list[str], capture: _Capture, outcome: dict[str, Any]) -> None:
        cfg = self._config
        try:
            api = self._client.api
            exec_id = api.exec_create(
                self._container_id,
                cmd,
                user=f"{cfg.uid}:{cfg.gid}",
                workdir=WORKDIR,
                stdout=True,
                stderr=True,
            )["Id"]
            for out, err in api.exec_start(exec_id, stream=True, demux=True):
                capture.add(out, err)
            outcome["exit_code"] = api.exec_inspect(exec_id)["ExitCode"]
        except Exception as e:  # thread boundary: hand any failure to the caller
            outcome["error"] = _brief(e)

    def _break(self, reason: str) -> None:
        """Kill and remove the container; later execs raise. `close` stays safe."""
        with self._lock:
            if self._state == "ready":
                self._state = "broken"
                self._reason = reason
            self._remove()

    def _remove(self) -> None:
        if not self._removed:
            self._removed = True
            _remove_resources(self._client, self._container_id, self._volume)


def _connect(client: Any) -> tuple[Any, bool]:
    owns = client is None
    try:
        client = docker.from_env() if owns else client
        client.ping()
    except (DockerException, RequestException, OSError) as e:
        raise SandboxUnavailableError(f"docker daemon is not reachable: {_brief(e)}") from None
    return client, owns


def create_sandbox(
    task: Task,
    *,
    client: Any = None,
    config: SandboxConfig = DEFAULT_CONFIG,
    clock: Callable[[], float] = time.monotonic,
) -> DockerSandbox:
    """The `SandboxFactory`. Raises only `SandboxUnavailableError`, and leaves nothing behind."""
    try:
        root = resolve_root(task.target_path)
    except (OSError, PathError) as e:
        raise SandboxUnavailableError(f"target is not an existing directory: {_brief(e)}") from None
    client, owns = _connect(client)
    token = uuid.uuid4().hex[:12]
    volume = f"sectriage-scratch-{token}"
    container_id: str | None = None
    try:
        try:
            client.images.get(config.image)
        except ImageNotFound:
            raise SandboxUnavailableError(
                f"sandbox image {config.image} is missing; build it with: {config.build_command}"
            ) from None
        client.volumes.create(**volume_kwargs(task, config, volume))
        container = client.containers.create(
            **container_kwargs(task, root, config, name=f"sectriage-{token}", volume=volume)
        )
        container_id = container.id
        container.start()
    except (DockerException, RequestException, OSError) as e:
        _remove_resources(client, container_id, volume)
        if owns:
            client.close()
        raise SandboxUnavailableError(f"could not start the sandbox: {_brief(e)}") from None
    except BaseException:
        _remove_resources(client, container_id, volume)
        if owns:
            client.close()
        raise
    return DockerSandbox(client, container_id, volume, task, config, clock=clock, owns_client=owns)
