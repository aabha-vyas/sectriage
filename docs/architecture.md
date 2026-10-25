# Architecture

Notes that don't fit in a `CLAUDE.md`. Keep it short.

## Agent lane (`src/agent/`)

- Tools: `read_file` and `search_code` run on the host, confined to `Task.target_path`.
  `run_command`, `semgrep`, and `git` only build an argv on the host and execute it through the
  `Sandbox` Protocol in `src/agent/sandbox_protocol.py`. The sandbox lane implements it.
- Without a sandbox the CLI uses `UnavailableSandbox`, which refuses every exec.
- The semgrep and git argv shapes are unverified until the sandbox lane runs them for real.
- Done in the prep commit: `findings.meta.json` has a `"tokens"` object with integer keys `input`
  (uncached input only), `output`, `cache_read` and `cache_write`, which `evals/metrics.py` reads.
  The totals come from `RunResult.usage`, written by `_write_outputs`.

## Prep commit on main (before lane/sandbox and lane/swarm branch)

Three small changes, one commit, so replay and the evals summary have what they need:
- `Event.stop_reason: str | None = None` in `src/contracts.py` (additive). The loop sets it from `ModelResponse.stop_reason` on every Event written after a model call. It is the model API's value (`tool_use`, `end_turn`, `max_tokens`, `refusal`), not the loop's `StopReason`. Replay uses it and infers only for old logs.
- `findings.meta.json` gains a `tokens` object (input, output, cache_read, cache_write) so evals can report token totals.
- `_check_target` in `src/agent/run.py` becomes public `check_target`; swarm and evals import it.

## TODO: report findings through the contract

The agent emits findings with agent-local control tools (`report_finding`, `finish`). Their Events
have `tool=None`, because `contracts.py` has no tool name for them. On main, add a small additive
commit with `ToolName.REPORT_FINDING` and a `ReportFindingArgs` model in `TOOL_ARGS` (optionally
`ToolName.FINISH`), then rebase `lane/agent` and switch the loop to the contract names. Old JSONL
logs still load, since the Event schema is unchanged.
