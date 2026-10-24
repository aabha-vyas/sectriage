# Stage 2 plan: lane/agent

Stage 1 is done. This records the approved Stage 2 scope: `src/agent/` and `tests/agent/` only.
Do not touch `src/contracts.py`, `src/sandbox/`, or `evals/` (except the note in Q4).

## Scope

- `src/agent/prompts.py`: stable `SYSTEM_PROMPT` (target is intentionally vulnerable and local, no
  network, report only what was verified in code), `PROMPT_VERSION = "v1"`, control-tool schemas.
- `src/agent/model.py`: `ModelClient` Protocol and `AnthropicModelClient` (default
  `claude-haiku-5-5`, cache_control on tools and system, effort pinned to `medium`, parallel tool
  use disabled, `cost_usd` from `pricing.py`).
- `src/agent/loop.py`: `run_task()`; plan, call tool, observe, update memory; stops at `max_steps`
  or `timeout_s`; one Event per step, written before the step returns; terminal Event for the stop
  reason.
- `src/agent/run.py`: `python -m src.agent.run --target evals/targets/vuln_app --out findings.json`.
- Tests: `test_model.py`, `test_loop.py`, `test_cli.py`, plus a scripted `FakeModel` in `fakes.py`.

## Decisions (Q1 to Q8)

1. **Findings:** `report_finding` and `finish` are agent-local control tools, intercepted by the
   loop. Their Events have `tool=None`. No `contracts.py` change in this lane. The user adds
   `ToolName.REPORT_FINDING` on main later and rebases; the TODO is in `docs/architecture.md`.
2. **Validation errors:** never raise. Unknown tool, bad args, or a bad `report_finding` returns an
   `is_error` tool_result and consumes a step. Three in a row stop the run (`invalid_tool_calls`).
3. **Pricing and prompt:** `pricing.py` has Haiku 5.5 only, verified 2026-10-24. The prompt and
   `PROMPT_VERSION` live in `prompts.py`; the CLI writes `<out>.meta.json` with model, prompt version
   and price date.
4. **Temperature:** keep `Task.temperature`, do not send it, log one warning if non-default,
   `--temperature` stays a documented no-op. First make one tiny live call to confirm the API
   rejects it, and report the result. Add a note to `evals/CLAUDE.md`: comparability comes from
   pinned model + `PROMPT_VERSION` + repeated runs.
5. **Paths:** the model uses target-relative paths; stored Findings use repo-relative paths.
6. **Sandbox:** the sandbox lane owns the factory name. `UnavailableSandbox` is the fallback, so
   until that lane lands only `read_file` and `search_code` work.
7. **search_code:** literal substring only, no regex on the host.
8. **Smaller choices:** one tool call per step; events default to `<out stem>.events.jsonl`; the CLI
   refuses targets outside `evals/targets/`; check `usage.cache_read_input_tokens` in the first live
   run to verify the caching minimum.

## Live run

One live run on `vuln_app` with `--max-steps 30`, only after the user approves it (it spends money
and needs `ANTHROPIC_API_KEY`). Then `evals.run --summary --findings findings.json`; report the
delta only.

## Tests (all offline: FakeModel and FakeSandbox, the Anthropic client is blocked in conftest)

- Step budget stops (`max_steps=3`, endless tool calls end as `max_steps`).
- Timeout stops (injected clock; tool timeout clamped to remaining time).
- An Event is written for every step, before `run_task` returns.
- Traversal rejected and truncation (`truncated=True` over 16 KiB), exercised through the loop.
- Dedupe: two `report_finding` calls with the same key store one, the second reports `duplicate`.
- Invalid model tool call (unknown tool, bad args, truncated tool_use) is handled without a crash.
- Request shape: cache_control placement, no `temperature`, parallel tool use disabled.
- CLI: scripted run on `evals/targets/vuln_app` writes a findings.json that `evals.run --findings`
  accepts; refuses targets outside `evals/targets/`.

Then run pytest, ruff and `scripts/verify.sh`, and stop so the user can commit.
