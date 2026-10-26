# Sandbox lane plan (lane/sandbox)

Status: agent lane merged. `run_task` accepts a `SandboxFactory`, but no CLI passes one, so `run_command` and `git` refuse every exec (`UnavailableSandbox`). This lane makes them run for real. The `semgrep` tool stays unsupported here (see Follow-ups).

## Scope (src/sandbox/ and tests/sandbox/ only; do not change src/contracts.py, src/agent/, or evals/)
1. `DockerSandbox` implements the `Sandbox` Protocol in `src/agent/sandbox_protocol.py` with the `docker` SDK. `create_sandbox(task)` is the `SandboxFactory`. It raises `SandboxUnavailableError` when the daemon or image is missing; for a missing image the message is the build command.
2. One container per task, started in the factory. Each `exec` runs `argv` as a list (no shell), cwd `/work`, and returns `ExecResult`. `close` is idempotent.
3. Isolation: `network_mode=none`, read-only root fs, `cap_drop=ALL`, `no-new-privileges`, non-root uid/gid, tmpfs `/tmp`, no docker socket, not privileged.
4. Limits (defaults in `config.py`): 1 CPU, 512 MiB with swap equal to memory, 128 pids. Each exec runs under in-container `timeout -s KILL <timeout_s>`; expiry returns `timed_out=True` and the sandbox stays usable. A host deadline just after it is the backstop: on expiry kill and remove the container, mark the sandbox broken, return `timed_out=True`, and make later execs raise `SandboxUnavailableError`. No recreate. Captured output is capped per stream (1 MiB).
5. Mounts: only the target, read-only at `/work`, plus a per-task named volume at `/scratch` (HOME, TMPDIR). Use SDK `Mount` objects, not `-v` strings (the repo path contains a space). The factory refuses a target that is not an existing directory.
6. Cleanup: `close` force-removes the container and the scratch volume and tolerates "already gone"; a failed `create_sandbox` removes what it made. Containers carry label `sectriage.task=<id>` and a self-destruct `sleep` of `timeout_s` + 60 s, so a killed worker leaves nothing long-lived. No reaper.
7. Image: `src/sandbox/Dockerfile`, base pinned by digest, non-root user, build command in the file header. It installs git and has coreutils, grep, find, sed and python3 so `run_command` is useful. No semgrep: the tool is unsupported here (Follow-ups). Env: git `safe.directory=/work`.
8. Tests (`@pytest.mark.docker`, skipped when the daemon or image is missing): network unreachable; write to `/work` fails, write to `/scratch` works; `sleep 300` with `timeout_s=2` returns `timed_out=True` within seconds and leaves no process; memory hog is killed and the sandbox stays usable; `id -u` is not 0; `git --version` runs and `git init` works in `/scratch`; container and volume are gone after `close`, after an exec error and after a timeout; `close` twice is fine. Non-Docker unit tests use a fake client for the container config and the broken-sandbox path.

## Rules
- Never run model-supplied commands on the host. No egress allowlist without explicit approval.
- Only intentionally vulnerable local targets under `evals/targets/` are mounted.
- The live run spends money: only after approval, `uv run --env-file .env`, default cap $0.50. Report the delta only; give paths, not logs.
- Contract changes are separate small commits on main.

## Done when
- pytest, ruff check, ruff format --check and scripts/verify.sh are clean, and the docker tests pass locally.
- Verified so far: the docker tests; a free real-env preflight and sandbox check on ledger_api; `src/sandbox/drive_tools.py`, which runs `run_command` (a grep and a `python3 -c`) through the real `ToolRegistry` and `DockerSandbox` on ledger_api for $0; and two live agent runs (`stop=finish`, $0.0102 total) that completed inside the sandbox.
- NOT verified live: a model using `run_command` on its own. In those two runs it made 0 `run_command` calls (it chose `read_file` and `search_code`). The next live run uses `--confirm-in-sandbox`, which changes the task goal to tell the agent to use `run_command`; its output line is labelled "instructed to use run_command" and it shows only that the tool works for a model that is asked to use it. The goal is saved as `goal.txt` beside the run. `evals.run --summary` shows no regression. Targets usually have no `.git`, so `git` is checked by the docker tests only (`git --version`, `git init` in `/scratch`).

## Follow-ups (not this lane)
1. After both lanes merge, a small commit: `evals/live.py` accepts `--sandbox docker` (passes `create_sandbox`) and its summary records `sandbox="docker"` instead of the hardcoded `"none"`. Then retire `live_run.py`.
2. `--sandbox docker` flag in `src/agent/run.py` (lane/agent).
3. Semgrep: install it in the image (pinned), bake rules in, and add a `builtin` config alias (needs a change to `src/agent/tools/sandboxed.py`).

## Files to create
`src/sandbox/{__init__,docker_sandbox,config,live_run,drive_tools}.py`, `src/sandbox/Dockerfile`, `tests/sandbox/{__init__,conftest,test_config,test_docker_sandbox,test_live_run,test_drive_tools}.py`.

## Decisions and open questions
- Decided: keep `git` as specified but verify it in the docker tests only (targets usually have no `.git`, so a live run answers "not a repository"); no semgrep in the image; no image build on first use; limits as above; the live run uses ledger_api (lane/evals has merged).
- Open: none.

## Risks
- `docker exec` timeouts do not kill the in-container process; hence in-container `timeout`, with the host deadline as backstop.
- OOM kill and timeout kill both exit 137; tell them apart by elapsed time.
- macOS Docker Desktop bind mounts and a non-root uid may make target files unreadable.
- A read-only root fs breaks tools that write to HOME unless HOME is `/scratch`.
- Unpinned apt packages make the image not fully reproducible.
- A noisy process can flood output before the cap bites if the stream is read naively.
