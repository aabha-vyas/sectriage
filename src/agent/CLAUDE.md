# src/agent

- The loop is plain Python: plan, call a tool, observe, update memory, stop at the step budget.
- Tools are registered in one registry and validated against schemas in `src/contracts.py`.
- Long-term memory dedupes findings by (file, line range, vulnerability class).
- Emit a JSONL event for every step before returning its result.
- Tests for this folder go in `tests/agent/`.
