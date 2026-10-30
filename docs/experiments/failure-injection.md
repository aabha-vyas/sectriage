# Experiment: failure injection for the swarm (SIGKILL a worker, check the recovery)

Status: **exploratory run, seed 1: 20 of 20 trials counted, pass.** It was made on an uncommitted
working tree (the harness itself was uncommitted, based on `cafde59`), so it is not the confirmatory
run. The pre-registered rule below was written into this file before that run, but nothing was
committed in between, so the ordering rests on this document and the timestamps of the generated
JSON, not on git history. The results block below and `failure-injection/runs.json` are that run.

## Question

The swarm claims that a worker killed mid-task costs one retry and nothing else: the lease expires,
the other worker claims the task again, exactly one row ends up in `results`, and the winning
attempt's event log replays exactly. Until now that rested on one hand-timed kill
(`tests/swarm/test_crash_recovery.py`). This experiment kills a worker at seeded points, including
the windows most likely to break the claim, and counts how often the claim holds.

## Setup

| setting | value |
|---|---|
| command | `uv run python -m src.swarm.chaos --trials 20 --seed 1` |
| model | `DEMO-ONLY-fake-model` (scripted, `src/swarm/fake_model.py`); no network, no API key, no Docker, no spend |
| target | `evals/targets/vuln_app` (7 scripted steps: read, 5 findings, finish) |
| pool | 2 workers, lease 1.2 s (heartbeat every 0.3 s), 0.15 s per scripted step |
| per trial | fresh temp directory and `queue.db`, one task, one SIGKILL |
| code under test | `SqliteQueue`, `WorkerPool`, `replay_task`, unchanged. The harness only observes them and, for three windows, wraps four call points inside the worker processes |

Hooks reach the workers without a change to worker code: workers load their model factory by import
path, and `src.swarm.chaos:chaos_factory` installs the wrappers when the child imports the module.
The pool's parent process and the pytest process are never patched. Control runs use the same
factory with the plan disarmed, so control and trial run identical code.

## Kill windows

Each trial has exactly one kill (the first worker to reach the point takes it, with an `O_EXCL`
marker file). 20 trials are 8 `random_time`, 4 `tool_call`, 4 `heartbeat`, 4 `pre_ack`, shuffled
and drawn from `random.Random(seed)` before any trial runs.

| window | killed by | point |
|---|---|---|
| `random_time` | the harness | SIGKILL of the lease holder at a seeded offset between 0.05 s and 0.9 x the control run's duration after the claim |
| `tool_call` | the worker itself | inside the patched `ToolRegistry.execute`: at entry, or after the tool ran and before its event is written |
| `heartbeat` | the worker itself | k-th heartbeat (k in 1, 2): before the heartbeat transaction, or inside it at the `UPDATE tasks SET lease_expires_at` statement, uncommitted |
| `pre_ack` | the worker itself | after the terminal event is flushed: before `ack` is called, or inside the ack transaction at the `INSERT OR IGNORE INTO results` statement, after the `done` update and before the commit |

Mid-transaction kills use SQLite's trace callback, which fires when a statement starts, so the
transaction is open and uncommitted at that moment. The hooks match queue SQL text; if a queue edit
changes that text, `tests/swarm/test_chaos.py` fails instead of the window silently never firing.

## Pre-registered rule

Written before the first full run.

- A trial is **counted** if the kill fired and recovery was exercised (the winning attempt is 2).
- **Pass**: every counted trial meets every invariant below, and 20 trials are counted.
- **Any violation stops the run** (exit 3). The trial's directory is kept and the violation is
  reported as a negative result. It is not retried, skipped or worked around. A miss in the
  takeover timing bound alone is labelled `timing` rather than `bug` (it could be machine load) and
  also stops the run.
- `not_fired` (the kill point was never reached) and `late_kill` (the ack had already committed, so
  the winning attempt is 1) do not test recovery. They are left out of the denominator and the
  trial is redrawn from the same seeded stream. **If `not_fired` plus `late_kill` exceeds 25% of the
  requested trials (more than 5 of 20), the run stops (exit 4) and is reported as it stands.** The
  offsets are not re-tuned to get under the threshold.
- A control run that is not clean, or two control runs that disagree, aborts the experiment (exit
  5). So does the runtime cap (`--max-runtime-s`, default 540 s, against a 10 minute budget).

### Invariants (per counted trial)

1. `completed`: task `done` with stop reason `finish` within the trial timeout.
2. `attempts`: exactly 2.
3. `one_result_row`: `SELECT COUNT(*) FROM results` is 1 for the task and in total.
4. `exit_codes`: one worker exits -9 (SIGKILL), the other 0.
5. `findings_match_control`: canonical findings bytes (sorted by `finding_id`, sorted keys, no
   whitespace) equal the control run's.
6. `replay_exact`: `replay_task` on the winning log gives identical findings and the same stop reason.
7. `attempt_logs`: the winning log is attempt 2's; replay leaves the killed attempt's log unchanged;
   for `tool_call` and `heartbeat` kills the killed log has no terminal event.
8. `lease_takeover`, from a 10 ms poller of the task row: the second claim is not before the last
   lease seen for attempt 1 ran out (1 ms tolerance); it comes within 1.0 s after that (the
   surviving worker polls every 0.2 s); the task stayed `running` until then; final `last_error` is
   `lease expired`.
9. `no_spend`: `cost_usd` is 0.

Per trial the JSON also records the kill point, attempts, result rows, and kill to result time
(`results.finished_at` minus the kill time, one host and clock).

## Results

<!-- results:start -->

Command: `uv run python -m src.swarm.chaos --trials 20 --seed 1`. Seed 1, git `cafde591e`.

**Verdict: pass** (20 of 20 trials counted; not fired 0, late kill 0; wall time 107.1 s).

Trials that met each invariant, with a Wilson 95% interval:

| invariant | overall | random_time | tool_call | heartbeat | pre_ack |
|---|---|---|---|---|---|
| `completed` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `attempts` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `one_result_row` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `exit_codes` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `findings_match_control` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `replay_exact` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `attempt_logs` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `lease_takeover` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |
| `no_spend` | 20/20 (0.84 to 1.00) | 8/8 (0.68 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) | 4/4 (0.51 to 1.00) |

Kill points (counted trials):

| trial | window | point | attempts | rows | kill to result s | takeover gap s |
|---|---|---|---|---|---|---|
| 0 | tool_call | tool_done | 2 | 1 | 2.15 | 0.02 |
| 1 | random_time | harness +0.72 s | 2 | 1 | 2.21 | 0.03 |
| 2 | pre_ack | before_ack | 2 | 1 | 2.25 | 0.13 |
| 3 | pre_ack | before_ack | 2 | 1 | 2.25 | 0.13 |
| 4 | tool_call | tool_done | 2 | 1 | 2.16 | 0.03 |
| 5 | random_time | harness +0.07 s | 2 | 1 | 2.24 | 0.02 |
| 6 | pre_ack | before_ack | 2 | 1 | 2.25 | 0.12 |
| 7 | random_time | harness +0.69 s | 2 | 1 | 2.25 | 0.03 |
| 8 | heartbeat | heartbeat_in_tx #1 | 2 | 1 | 2.06 | 0.05 |
| 9 | random_time | harness +0.76 s | 2 | 1 | 2.17 | 0.02 |
| 10 | tool_call | tool_done | 2 | 1 | 2.17 | 0.03 |
| 11 | heartbeat | heartbeat_before_tx #2 | 2 | 1 | 2.13 | 0.13 |
| 12 | heartbeat | heartbeat_before_tx #1 | 2 | 1 | 2.02 | 0.02 |
| 13 | heartbeat | heartbeat_in_tx #2 | 2 | 1 | 2.13 | 0.13 |
| 14 | random_time | harness +0.44 s | 2 | 1 | 2.29 | 0.13 |
| 15 | random_time | harness +0.22 s | 2 | 1 | 2.14 | 0.02 |
| 16 | tool_call | tool_done | 2 | 1 | 2.15 | 0.02 |
| 17 | random_time | harness +0.74 s | 2 | 1 | 2.19 | 0.02 |
| 18 | pre_ack | ack_in_tx | 2 | 1 | 2.25 | 0.12 |
| 19 | random_time | harness +0.33 s | 2 | 1 | 2.39 | 0.13 |

Kill to result: min 2.02 s, median 2.18 s, max 2.39 s.

<!-- results:end -->

## Limits

- **Identical findings are claimed only for the scripted model.** With a live model a retry is a
  fresh attempt (`_execute` builds a new model and a new log), so its findings may differ from the
  first attempt's. For a live model the claims are recovery, exactly one result row, and exact
  replay of the winning attempt's log. They were not measured with a live model here.
- The scripted findings are copied from the answer key. This tests the queue, lease and replay
  machinery, not detection quality.
- `tool_call` kills cover step 1 only. Only `read_file` goes through `ToolRegistry.execute` in the
  scripted run; `report_finding` and `finish` do not, and the window was not extended to
  `EventLog.append`.
- SIGKILL of one process, not power loss or a kernel crash. WAL with `synchronous=FULL` durability
  is not tested.
- One host and one clock; one task and two workers, so no queue contention; lease 1.2 s against the
  60 s default; host read-only tools, so no container to leak.
- The Wilson intervals describe this stratified, hand-chosen set of kill points, not crashes in
  general. 20 of 20 supports "at least about 84% at 95% confidence", not "always". Per window
  (n = 4) the lower bound is 0.51.
- **Sub-point coverage is thin.** Seed 1 drew `tool_call` at `after_tool` four times and never at
  `entry`, `pre_ack` at `before_ack` three times and at `ack_in_tx` once, and `heartbeat` twice
  before the transaction and twice inside it. The only mid-ack-transaction kill is one trial. The
  per-window intervals say little; `tests/swarm/test_chaos.py` (`-m chaos`) runs each of the
  window's key points once more, but a different seed is needed to cover the rest.
- Times are one machine's, and the 1.0 s takeover bound is a choice made before the run.
