# Architecture

Notes that don't fit in a `CLAUDE.md`. Keep it short.

## Agent lane (`src/agent/`)

- Tools: `read_file` and `search_code` run on the host, confined to `Task.target_path`.
  `run_command`, `semgrep`, and `git` only build an argv on the host and execute it through the
  `Sandbox` Protocol in `src/agent/sandbox_protocol.py`. The sandbox lane implements it.
- Without a sandbox the CLI uses `UnavailableSandbox`, which refuses every exec.
- The semgrep and git argv shapes are unverified until the sandbox lane runs them for real.

## TODO: report findings through the contract

The agent emits findings with agent-local control tools (`report_finding`, `finish`). Their Events
have `tool=None`, because `contracts.py` has no tool name for them. On main, add a small additive
commit with `ToolName.REPORT_FINDING` and a `ReportFindingArgs` model in `TOOL_ARGS` (optionally
`ToolName.FINISH`), then rebase `lane/agent` and switch the loop to the contract names. Old JSONL
logs still load, since the Event schema is unchanged.
