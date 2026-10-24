# src/sandbox

- One container per task. Network off by default; an egress allowlist needs explicit approval.
- Always set CPU, memory, and wall-clock limits.
- Mount the target read-only; write outputs to a separate scratch volume.
- Never run user or model-supplied commands on the host.
- Tests that need Docker are marked `@pytest.mark.docker`.
