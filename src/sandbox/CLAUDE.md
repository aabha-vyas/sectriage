# src/sandbox

- One container per task. Network off by default; an egress allowlist needs explicit approval.
- Always set CPU, memory, and wall-clock limits.
- Mount the target read-only; write outputs to a separate scratch volume.
- Never run user or model-supplied commands on the host.
- Tests that need Docker are marked `@pytest.mark.docker`.
- `/tmp` (64 MiB) and `/scratch` (256 MiB) are tmpfs, so they may count against the 512 MiB memory
  limit. Filling both while also using memory is untested.
- `timeout -s KILL` exits 137, the same as an OOM kill; `docker_sandbox.py` tells them apart by
  elapsed time, not exit code.
