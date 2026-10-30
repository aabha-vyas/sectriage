"""Failure injection for the swarm: SIGKILL a worker at seeded points, then check the recovery.

    uv run python -m src.swarm.chaos --trials 20 --seed 1

Each trial enqueues one task on `evals/targets/vuln_app`, starts two workers with a short lease and
kills one of them. It then measures completion, attempts, result rows, time from kill to result,
whether the lease expired as designed, and whether the findings and a replay of the winning log
match an uninterrupted control run. The model is the DEMO-ONLY scripted one in `fake_model.py`: no
network, no API key, no Docker, no spend. See `docs/experiments/failure-injection.md`.

Kill windows. `random_time` is a SIGKILL from this harness at a seeded offset after the claim. The
other three are reached by hooks that run inside the worker processes, which then SIGKILL
themselves: `tool_call` (inside the one registry-routed tool), `heartbeat` (before or inside the
heartbeat transaction) and `pre_ack` (before `ack`, or inside its transaction between the `done`
update and the `results` insert). The hooks are installed when a worker imports this module as its
model factory (`chaos_factory`); the queue, worker and replay code is not changed. They match
queue SQL text, so `tests/swarm/test_chaos.py` fails if a queue edit stops them firing.

Exit codes: 0 pass; 3 an invariant failed (a possible system bug: stop, nothing is retried or
worked around); 4 too many kills did not test recovery; 5 the harness or the control run is not
sound, or the runtime cap was hit.
"""

import argparse
import functools
import hashlib
import json
import math
import multiprocessing
import os
import random
import shutil
import signal
import sqlite3
import statistics
import subprocess
import sys
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Sequence
from contextlib import contextmanager
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, NamedTuple

from src.agent.loop import REPO_ROOT
from src.agent.model import DEFAULT_MODEL
from src.agent.run import GOAL, TARGETS_DIR
from src.agent.tools.registry import ToolRegistry
from src.contracts import Event, Finding, Task
from src.swarm.fake_model import DELAY_ENV, DEMO_TARGET, DemoModel, demo_factory
from src.swarm.queue import TaskState
from src.swarm.replay import ReplayError, replay_task
from src.swarm.sqlite_queue import SqliteQueue
from src.swarm.worker import WorkerPool

FACTORY = "src.swarm.chaos:chaos_factory"
PLAN_ENV = "SWARM_CHAOS_PLAN"
TASK_ID = f"{DEMO_TARGET}-chaos-1"

RANDOM_TIME, TOOL_CALL, HEARTBEAT, PRE_ACK = "random_time", "tool_call", "heartbeat", "pre_ack"
WINDOWS = (RANDOM_TIME, TOOL_CALL, HEARTBEAT, PRE_ACK)
SUBPOINTS: dict[str, tuple[str | None, ...]] = {
    RANDOM_TIME: (None,),
    TOOL_CALL: ("entry", "after_tool"),
    HEARTBEAT: ("before_tx", "in_tx"),
    PRE_ACK: ("before_ack", "in_tx"),
}
HEARTBEAT_INDEXES = (1, 2)
# What the trace callback looks for: the first statement of a heartbeat and of the result insert.
HEARTBEAT_SQL = "UPDATE tasks SET lease_expires_at"
RESULT_INSERT_SQL = "INSERT OR IGNORE INTO results"

DEFAULT_LEASE_S = 1.2
DEFAULT_STEP_DELAY_S = 0.15
DEFAULT_TRIAL_TIMEOUT_S = 45.0
DEFAULT_MAX_RUNTIME_S = 540.0
POLL_INTERVAL_S = 0.01
RANDOM_MIN_OFFSET_S = 0.05
RANDOM_MAX_FRACTION = 0.9  # of the control run's duration
TAKEOVER_SLACK_S = 0.001  # clock tolerance for "not before the lease expired"
TAKEOVER_MAX_GAP_S = 1.0  # the survivor polls every 0.2 s; a bigger gap is reported as timing
EXCLUDED_LIMIT = 0.25  # of the requested trials

EXIT_PASS, EXIT_VIOLATION, EXIT_EXCLUDED, EXIT_ABORT = 0, 3, 4, 5

INVARIANTS = (
    "completed",
    "attempts",
    "one_result_row",
    "exit_codes",
    "findings_match_control",
    "replay_exact",
    "attempt_logs",
    "lease_takeover",
    "no_spend",
)

RESULTS_ID = "results"


# statistics ---------------------------------------------------------------------------------


def wilson(successes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    """Wilson score interval for a proportion; (0, 1) when there are no trials."""
    if n <= 0:
        return 0.0, 1.0
    p = successes / n
    denom = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denom
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denom
    return max(0.0, centre - half), min(1.0, centre + half)


def canonical_findings(findings: Sequence[Finding]) -> bytes:
    """The bytes two runs must agree on: findings sorted by id, keys sorted, no whitespace."""
    rows = [f.model_dump(mode="json") for f in sorted(findings, key=lambda f: f.finding_id)]
    return json.dumps(rows, sort_keys=True, separators=(",", ":")).encode()


# plan ---------------------------------------------------------------------------------------


@dataclass(frozen=True)
class TrialPlan:
    index: int  # draw number; a redraw takes the next one
    window: str
    sub: str | None = None  # where inside the window
    k: int | None = None  # which heartbeat (heartbeat window)
    frac: float | None = None  # position in the offset range (random_time window)


def parse_windows(spec: str) -> dict[str, str | None]:
    """`window[:point]` items, comma separated, as {window: fixed point or None}. One per window."""
    chosen: dict[str, str | None] = {}
    for item in spec.split(","):
        window, _, sub = item.strip().partition(":")
        if window not in SUBPOINTS:
            raise ValueError(f"unknown window {window!r}; choose from {', '.join(WINDOWS)}")
        if sub and sub not in SUBPOINTS[window]:
            raise ValueError(f"unknown point {sub!r} for {window}; choose from {SUBPOINTS[window]}")
        if window in chosen:
            raise ValueError(f"window {window!r} given twice")
        chosen[window] = sub or None
    return chosen


class Planner:
    """Draws the whole plan from one seeded stream; `redraw` continues the same stream.

    By default 20 trials are 8 random_time, 4 each of the other windows. With `windows` (see
    `parse_windows`) only those windows are used, split evenly (the first ones take the
    remainder), and a window given as `window:sub` always uses that point.
    """

    def __init__(self, trials: int, seed: int, windows: dict[str, str | None] | None = None):
        if trials < 1:
            raise ValueError("trials must be at least 1")
        self._rng = random.Random(seed)
        self._next = 0
        self._fixed = {w: sub for w, sub in (windows or {}).items() if sub}
        if windows:
            names = list(windows)
            order = [names[i % len(names)] for i in range(trials)]
        else:
            each = trials // 5  # three deliberate windows get a fifth each, random_time the rest
            order = [RANDOM_TIME] * (trials - 3 * each)
            for window in (TOOL_CALL, HEARTBEAT, PRE_ACK):
                order += [window] * each
        self._rng.shuffle(order)
        self.drawn = [self.draw(w) for w in order]

    def draw(self, window: str) -> TrialPlan:
        index, self._next = self._next, self._next + 1
        sub = self._fixed.get(window) or self._rng.choice(SUBPOINTS[window])
        k = self._rng.choice(HEARTBEAT_INDEXES) if window == HEARTBEAT else None
        frac = self._rng.random() if window == RANDOM_TIME else None
        return TrialPlan(index, window, sub, k, frac)

    redraw = draw


# child side: kill hooks ---------------------------------------------------------------------


def _kill_self() -> None:
    os.kill(os.getpid(), signal.SIGKILL)


def _fire(cfg: dict[str, Any], point: str) -> None:
    """Take this trial's one kill (the first process to create the marker), record it, die."""
    try:
        fd = os.open(cfg["marker"], os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return
    record = {
        "window": cfg["window"],
        "sub": cfg["sub"],
        "k": cfg["k"],
        "point": point,
        "pid": os.getpid(),
        "t_kill": time.time(),
    }
    with os.fdopen(fd, "w") as f:
        f.write(json.dumps(record))
    _kill_self()


def install_child_hooks(cfg: dict[str, Any]) -> Callable[[], None]:
    """Wrap the kill points. Returns a function that puts the originals back.

    `cfg` is `{"armed", "window", "sub", "k", "marker"}`. With `armed` false the wrappers only pass
    through, so a control run executes the same code as a trial. Call it only in a process meant
    to die: it patches `sqlite3.connect` for every connection opened afterwards.
    """
    armed, window, sub, k = bool(cfg.get("armed")), cfg.get("window"), cfg.get("sub"), cfg.get("k")
    lock = threading.Lock()
    seen = {"heartbeat_calls": 0, "heartbeat_statements": 0}

    orig_connect = sqlite3.connect
    orig_execute = ToolRegistry.execute
    orig_heartbeat = SqliteQueue.heartbeat
    orig_ack = SqliteQueue.ack

    def trace(sql: str) -> None:
        if not armed:
            return
        if window == HEARTBEAT and sub == "in_tx" and sql.startswith(HEARTBEAT_SQL):
            with lock:
                seen["heartbeat_statements"] += 1
                hit = seen["heartbeat_statements"] == k
            if hit:
                _fire(cfg, "heartbeat_in_tx")
        elif window == PRE_ACK and sub == "in_tx" and sql.startswith(RESULT_INSERT_SQL):
            _fire(cfg, "ack_in_tx")

    @functools.wraps(orig_connect)
    def connect(*args: Any, **kwargs: Any) -> sqlite3.Connection:
        conn = orig_connect(*args, **kwargs)
        conn.set_trace_callback(trace)
        return conn

    @functools.wraps(orig_execute)
    def execute(self: ToolRegistry, *args: Any, **kwargs: Any) -> Any:
        if armed and window == TOOL_CALL and sub == "entry":
            _fire(cfg, "tool_entry")
        out = orig_execute(self, *args, **kwargs)
        if armed and window == TOOL_CALL and sub == "after_tool":
            _fire(cfg, "tool_done")
        return out

    @functools.wraps(orig_heartbeat)
    def heartbeat(self: SqliteQueue, *args: Any, **kwargs: Any) -> bool:
        if armed and window == HEARTBEAT and sub == "before_tx":
            with lock:
                seen["heartbeat_calls"] += 1
                hit = seen["heartbeat_calls"] == k
            if hit:
                _fire(cfg, "heartbeat_before_tx")
        return orig_heartbeat(self, *args, **kwargs)

    @functools.wraps(orig_ack)
    def ack(self: SqliteQueue, *args: Any, **kwargs: Any) -> bool:
        if armed and window == PRE_ACK and sub == "before_ack":
            _fire(cfg, "before_ack")
        return orig_ack(self, *args, **kwargs)

    connect._chaos_hook = True  # type: ignore[attr-defined]
    sqlite3.connect = connect  # type: ignore[assignment]
    ToolRegistry.execute = execute  # type: ignore[method-assign]
    SqliteQueue.heartbeat = heartbeat  # type: ignore[method-assign]
    SqliteQueue.ack = ack  # type: ignore[method-assign]

    def uninstall() -> None:
        sqlite3.connect = orig_connect  # type: ignore[assignment]
        ToolRegistry.execute = orig_execute  # type: ignore[method-assign]
        SqliteQueue.heartbeat = orig_heartbeat  # type: ignore[method-assign]
        SqliteQueue.ack = orig_ack  # type: ignore[method-assign]

    return uninstall


def chaos_factory(model_name: str) -> DemoModel:
    """The workers' model factory: the scripted demo model. Importing this module installs hooks."""
    return demo_factory(model_name)


def _install_from_env() -> None:
    """Run at import. Only a spawned child with a plan file is patched, never a harness or pytest.

    The sentinel keeps a second copy of this module (spawn also imports the main module as
    `__mp_main__` under `python -m`) from wrapping the wrappers.
    """
    path = os.environ.get(PLAN_ENV)
    if not path or multiprocessing.parent_process() is None:
        return
    if getattr(sqlite3.connect, "_chaos_hook", False):
        return
    install_child_hooks(json.loads(Path(path).read_text()))


_install_from_env()


# harness: observing one trial ---------------------------------------------------------------


class Sample(NamedTuple):
    t: float
    attempts: int
    state: str
    lease_expires_at: float | None
    updated_at: float
    worker_pid: int | None


class _Poller(threading.Thread):
    """Reads the task row every 10 ms on its own connection and keeps each change."""

    def __init__(self, db: Path, task_id: str):
        super().__init__(name="chaos-poller", daemon=True)
        self.db, self.task_id = db, task_id
        self.samples: list[Sample] = []
        self._halt = threading.Event()

    def run(self) -> None:
        conn = sqlite3.connect(self.db)
        conn.execute("PRAGMA query_only = ON")
        last: tuple | None = None
        try:
            while not self._halt.is_set():
                try:
                    row = conn.execute(
                        "SELECT attempts, state, lease_expires_at, updated_at, worker_pid"
                        " FROM tasks WHERE task_id = ?",
                        (self.task_id,),
                    ).fetchone()
                except sqlite3.Error:
                    row = None
                if row is not None and tuple(row) != last:
                    last = tuple(row)
                    self.samples.append(Sample(time.time(), *row))
                time.sleep(POLL_INTERVAL_S)
        finally:
            conn.close()

    def finish(self) -> None:
        self._halt.set()
        self.join(5)


def analyse_takeover(samples: Sequence[Sample]) -> dict[str, Any]:
    """What the polled rows say about the lease: claim time, last lease seen, takeover time."""
    held = [s for s in samples if s.attempts == 1 and s.state == TaskState.RUNNING.value]
    if not held:
        return {"observed": False, "claim_at": None}
    claim_at = held[0].updated_at
    retaken = next((s for s in samples if s.attempts >= 2), None)
    if retaken is None:
        return {"observed": False, "claim_at": claim_at}
    last_lease = held[-1].lease_expires_at
    first_attempt = [s for s in samples if s.attempts == 1 and s.t >= held[0].t]
    return {
        "observed": True,
        "claim_at": claim_at,
        "last_lease_before": last_lease,
        "takeover_at": retaken.updated_at,
        "gap_s": retaken.updated_at - last_lease if last_lease is not None else None,
        "stayed_running": all(s.state == TaskState.RUNNING.value for s in first_attempt),
    }


@contextmanager
def _environ(changes: dict[str, str | None]) -> Iterator[None]:
    saved = {name: os.environ.get(name) for name in changes}
    try:
        for name, value in changes.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


def spawn_env(plan_path: Path, step_delay_s: float) -> dict[str, str | None]:
    """What the workers inherit. The API key is removed: nothing here can reach a live model."""
    return {
        "PYTHONPATH": str(REPO_ROOT),
        DELAY_ENV: str(step_delay_s),
        PLAN_ENV: str(plan_path),
        "ANTHROPIC_API_KEY": None,
    }


@dataclass(frozen=True)
class Config:
    root: Path
    lease_s: float = DEFAULT_LEASE_S
    step_delay_s: float = DEFAULT_STEP_DELAY_S
    trial_timeout_s: float = DEFAULT_TRIAL_TIMEOUT_S


def _read_log(path: Path) -> tuple[int, bool, str | None]:
    """(events, has a terminal event, sha256) of an attempt log that may end in a torn line."""
    try:
        raw = path.read_bytes()
    except FileNotFoundError:
        return 0, False, None
    events = []
    for line in raw.decode("utf-8", errors="replace").split("\n"):
        if line.strip():
            try:
                events.append(Event.from_jsonl(line))
            except ValueError:
                continue
    terminal = any(e.result_summary.startswith("stop:") for e in events)
    return len(events), terminal, hashlib.sha256(raw).hexdigest()


def _harness_kill(
    plan: TrialPlan,
    queue: SqliteQueue,
    pool: WorkerPool,
    poller: _Poller,
    control_s: float,
    deadline: float,
) -> dict[str, Any] | None:
    """SIGKILL the lease holder at the seeded offset after its claim. None: no kill was made."""
    while not any(s.state == TaskState.RUNNING.value for s in poller.samples):
        if time.monotonic() > deadline:
            return None
        time.sleep(POLL_INTERVAL_S)
    claim_at = next(s for s in poller.samples if s.state == TaskState.RUNNING.value).updated_at
    assert plan.frac is not None
    span = max(0.0, RANDOM_MAX_FRACTION * control_s - RANDOM_MIN_OFFSET_S)
    offset = RANDOM_MIN_OFFSET_S + plan.frac * span
    while time.time() < claim_at + offset:
        time.sleep(0.002)
    record = queue.get(TASK_ID)
    if record is None or record.state is not TaskState.RUNNING or record.attempts != 1:
        return None
    pid = record.worker_pid
    if pid is None or pid not in pool.pids:
        raise RuntimeError(f"lease holder pid {pid} is not one of the pool's workers")
    t_kill = time.time()
    os.kill(pid, signal.SIGKILL)
    return {"pid": pid, "t_kill": t_kill, "offset_s": offset, "point": "harness"}


def run_trial(
    plan: TrialPlan | None,
    cfg: Config,
    *,
    control_s: float | None = None,
    control_bytes: bytes | None = None,
) -> tuple[dict[str, Any], bytes]:
    """One trial (or, with `plan=None`, an uninterrupted control run). Returns the record and the
    canonical findings bytes. The work directory is left for the caller to remove."""
    workdir = Path(tempfile.mkdtemp(prefix="chaos-", dir=cfg.root))
    db, events_dir, marker = workdir / "queue.db", workdir / "events", workdir / "killed.json"
    worker_side = plan is not None and plan.window != RANDOM_TIME
    plan_path = workdir / "plan.json"
    plan_path.write_text(
        json.dumps(
            {
                "armed": worker_side,
                "window": plan.window if plan else None,
                "sub": plan.sub if plan else None,
                "k": plan.k if plan else None,
                "marker": str(marker),
            }
        )
    )
    queue = SqliteQueue(db)
    queue.enqueue(
        Task(
            task_id=TASK_ID,
            target_path=str(TARGETS_DIR / DEMO_TARGET),
            goal=GOAL,
            max_steps=30,
            timeout_s=60,
            model=DEFAULT_MODEL,
        )
    )
    pool = WorkerPool(db, events_dir, FACTORY, workers=2, lease_s=cfg.lease_s)
    poller = _Poller(db, TASK_ID)
    started = time.time()
    deadline = time.monotonic() + cfg.trial_timeout_s
    harness_kill: dict[str, Any] | None = None
    codes: list[int | None] = []
    try:
        poller.start()
        with _environ(spawn_env(plan_path, cfg.step_delay_s)):
            pool.start()
        if plan is not None and plan.window == RANDOM_TIME:
            assert control_s is not None
            harness_kill = _harness_kill(plan, queue, pool, poller, control_s, deadline)
        while time.monotonic() < deadline:
            current = queue.get(TASK_ID)
            if current is not None and current.state in (TaskState.DONE, TaskState.DEAD):
                break
            if not any(p.is_alive() for p in pool.processes):
                break
            time.sleep(0.02)
        codes = pool.join(15)
        time.sleep(2 * POLL_INTERVAL_S)  # let the poller see the last change
    finally:
        pool.kill()
        poller.finish()

    final = queue.get(TASK_ID)
    result = queue.result(TASK_ID)
    with sqlite3.connect(db) as raw:
        rows_task = raw.execute("SELECT COUNT(*) FROM results WHERE task_id = ?", (TASK_ID,))
        rows_task_n = rows_task.fetchone()[0]
        rows_total_n = raw.execute("SELECT COUNT(*) FROM results").fetchone()[0]
    raw.close()

    kill: dict[str, Any] | None = harness_kill
    if worker_side and marker.exists():
        kill = json.loads(marker.read_text())
    takeover = analyse_takeover(poller.samples)

    killed_log = events_dir / f"{TASK_ID}.1.jsonl"
    n_events, has_terminal, sha_before = _read_log(killed_log)
    replay: dict[str, Any] = {"error": None, "identical": None, "stop_equal": None}
    findings_bytes = canonical_findings(result.findings) if result else b""
    if result is not None:
        try:
            report = replay_task(queue, TASK_ID)
            replay["identical"] = canonical_findings(report.recorded) == canonical_findings(
                report.replayed
            )
            replay["stop_equal"] = report.recorded_stop == report.replayed_stop
        except ReplayError as e:
            replay["error"] = str(e)
    _, _, sha_after = _read_log(killed_log)
    queue.close()

    record: dict[str, Any] = {
        "plan": asdict(plan) if plan else None,
        "window": plan.window if plan else "control",
        "workdir": str(workdir),
        "fired": kill is not None,
        "kill": kill,
        "completed": bool(final and final.state is TaskState.DONE and result is not None),
        "state": final.state.value if final else None,
        "stop_reason": result.stop_reason if result else None,
        "attempts": final.attempts if final else None,
        "last_error": final.last_error if final else None,
        "winning_attempt": result.attempt if result else None,
        "result_rows": rows_task_n,
        "result_rows_total": rows_total_n,
        "log_name": Path(result.log_path).name if result else None,
        "finished_at": result.finished_at if result else None,
        "kill_to_result_s": (
            result.finished_at - kill["t_kill"] if result and kill is not None else None
        ),
        "duration_s": (
            result.finished_at - takeover["claim_at"]
            if result and takeover.get("claim_at") is not None
            else None
        ),
        "wall_s": time.time() - started,
        "exit_codes": codes,
        "takeover": takeover,
        "killed_log": {
            "events": n_events,
            "has_terminal": has_terminal,
            "unchanged_by_replay": sha_before == sha_after,
        },
        "findings_sha256": hashlib.sha256(findings_bytes).hexdigest() if result else None,
        "findings_equal_control": (
            None if control_bytes is None or result is None else findings_bytes == control_bytes
        ),
        "replay": replay,
        "cost_usd": result.cost_usd if result else None,
        "steps": result.steps if result else None,
        "samples": [s._asdict() for s in poller.samples],
        "violations": [],
    }
    return record, findings_bytes


# harness: judging trials --------------------------------------------------------------------


def classify(record: dict[str, Any]) -> str:
    """counted (recovery was exercised), late_kill, not_fired, or control."""
    if record["window"] == "control":
        return "control"
    if not record["fired"]:
        # a harness kill that found the task already done is late; a hook that never ran is not
        return "late_kill" if record["winning_attempt"] == 1 else "not_fired"
    return "late_kill" if record["winning_attempt"] == 1 else "counted"


def check_trial(record: dict[str, Any]) -> list[dict[str, str]]:
    """Every invariant a counted trial must meet. `timing` marks a miss that load could cause."""
    bad: list[dict[str, str]] = []

    def fail(invariant: str, detail: str, kind: str = "bug") -> None:
        bad.append({"invariant": invariant, "kind": kind, "detail": detail})

    window = record["window"]
    if not record["completed"] or record["stop_reason"] != "finish":
        fail("completed", f"state={record['state']} stop_reason={record['stop_reason']}")
    if record["attempts"] != 2:
        fail("attempts", f"attempts={record['attempts']}, expected 2")
    if record["result_rows"] != 1 or record["result_rows_total"] != 1:
        fail(
            "one_result_row",
            f"{record['result_rows']} rows for the task, {record['result_rows_total']} in total",
        )
    codes = record["exit_codes"]
    if None in codes or sorted(codes) != [-signal.SIGKILL, 0]:
        fail("exit_codes", f"worker exit codes {codes}, expected one SIGKILL and one clean exit")
    if record["findings_equal_control"] is not True:
        fail("findings_match_control", f"sha256 {record['findings_sha256']} differs from control")
    replay = record["replay"]
    if replay["error"] or replay["identical"] is not True or replay["stop_equal"] is not True:
        fail("replay_exact", f"replay: {replay}")
    log = record["killed_log"]
    if record["log_name"] != f"{TASK_ID}.2.jsonl":
        fail("attempt_logs", f"winning log is {record['log_name']}, expected attempt 2's")
    if not log["unchanged_by_replay"]:
        fail("attempt_logs", "replay changed the killed attempt's log")
    if window in (TOOL_CALL, HEARTBEAT) and log["has_terminal"]:
        fail("attempt_logs", "the killed attempt's log has a terminal event")
    if record["cost_usd"] != 0:
        fail("no_spend", f"cost_usd={record['cost_usd']}")

    t = record["takeover"]
    if not t["observed"]:
        fail("lease_takeover", "the second claim was not observed", "timing")
    else:
        if t["takeover_at"] < t["last_lease_before"] - TAKEOVER_SLACK_S:
            fail(
                "lease_takeover",
                f"claimed {t['last_lease_before'] - t['takeover_at']:.3f} s before the lease"
                " ran out",
            )
        if not t["stayed_running"]:
            fail("lease_takeover", "the task left `running` before it was claimed again")
        if record["last_error"] != "lease expired":
            fail("lease_takeover", f"last_error={record['last_error']!r}, expected 'lease expired'")
        if t["gap_s"] > TAKEOVER_MAX_GAP_S:
            fail(
                "lease_takeover",
                f"claimed {t['gap_s']:.3f} s after the lease ran out (limit {TAKEOVER_MAX_GAP_S})",
                "timing",
            )
    return bad


def check_control(record: dict[str, Any]) -> list[str]:
    """A clean run needs one attempt, one row, two clean exits and no lease expiry."""
    problems = []
    if not record["completed"] or record["stop_reason"] != "finish":
        problems.append(f"not completed (state={record['state']})")
    if record["attempts"] != 1 or record["last_error"] is not None:
        problems.append(f"attempts={record['attempts']} last_error={record['last_error']!r}")
    if record["result_rows"] != 1:
        problems.append(f"{record['result_rows']} result rows")
    if record["exit_codes"] != [0, 0]:
        problems.append(f"exit codes {record['exit_codes']}")
    if record["cost_usd"] != 0:
        problems.append(f"cost_usd={record['cost_usd']}")
    return problems


def aggregate(counted: Sequence[dict[str, Any]]) -> dict[str, Any]:
    """Per invariant: trials that met it, out of the counted ones, with a Wilson 95% interval."""

    def one(records: Sequence[dict[str, Any]]) -> dict[str, Any]:
        n = len(records)
        out: dict[str, Any] = {"n": n}
        for name in INVARIANTS:
            ok = sum(1 for r in records if all(v["invariant"] != name for v in r["violations"]))
            lo, hi = wilson(ok, n)
            out[name] = {"ok": ok, "n": n, "wilson_lo": round(lo, 4), "wilson_hi": round(hi, 4)}
        return out

    return {
        "overall": one(counted),
        "by_window": {w: one([r for r in counted if r["window"] == w]) for w in WINDOWS},
    }


# harness: the experiment --------------------------------------------------------------------

ControlFn = Callable[[], tuple[dict[str, Any], bytes]]
TrialFn = Callable[[TrialPlan, float, bytes], dict[str, Any]]


def run_experiment(
    trials: int,
    seed: int,
    *,
    control_fn: ControlFn,
    trial_fn: TrialFn,
    max_runtime_s: float = DEFAULT_MAX_RUNTIME_S,
    cleanup: Callable[[dict[str, Any]], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
    windows: dict[str, str | None] | None = None,
) -> dict[str, Any]:
    """Controls, then trials until `trials` of them are counted. Stops at the first violation."""
    started = clock()
    planner = Planner(trials, seed, windows)
    report: dict[str, Any] = {
        "seed": seed,
        "windows": windows,
        "trials_requested": trials,
        "plan_drawn": [asdict(p) for p in planner.drawn],
        "controls": [],
        "trials": [],
        "excluded": {"not_fired": 0, "late_kill": 0},
        "verdict": "abort",
        "exit_code": EXIT_ABORT,
        "reason": None,
        "first_violation": None,
    }

    def finish(verdict: str, code: int, reason: str | None = None) -> dict[str, Any]:
        counted = [r for r in report["trials"] if classify(r) == "counted"]
        report.update(verdict=verdict, exit_code=code, reason=reason)
        report["counted"] = sum(1 for r in counted if not r["violations"])
        report["aggregates"] = aggregate(counted)
        report["wall_s"] = round(clock() - started, 1)
        return report

    try:
        first, first_bytes = control_fn()
        second, second_bytes = control_fn()
    except Exception as e:  # a harness failure is not a verdict on the system
        return finish("abort", EXIT_ABORT, f"control run failed: {type(e).__name__}: {e}")
    report["controls"] = [first, second]
    problems = check_control(first) + check_control(second)
    if first_bytes != second_bytes:
        problems.append("the two control runs disagree on findings")
    if problems or first["duration_s"] is None:
        return finish("abort", EXIT_ABORT, "control run is not clean: " + "; ".join(problems))
    control_s = first["duration_s"]

    pending = list(planner.drawn)
    counted = 0
    while counted < trials:
        if clock() - started > max_runtime_s:
            return finish("abort", EXIT_ABORT, f"runtime cap of {max_runtime_s:g} s reached")
        plan = pending.pop(0)
        try:
            record = trial_fn(plan, control_s, first_bytes)
        except Exception as e:
            return finish("abort", EXIT_ABORT, f"trial {plan.index} failed: {e!r}")
        report["trials"].append(record)
        kind = classify(record)
        if kind == "counted":
            record["violations"] = check_trial(record)
            if record["violations"]:
                report["first_violation"] = {"trial": plan.index, **record["violations"][0]}
                return finish("fail", EXIT_VIOLATION, "an invariant failed; see first_violation")
            counted += 1
            if cleanup:
                cleanup(record)
            continue
        report["excluded"][kind] += 1
        if cleanup:
            cleanup(record)
        if sum(report["excluded"].values()) > EXCLUDED_LIMIT * trials:
            return finish(
                "fail",
                EXIT_EXCLUDED,
                f"more than {EXCLUDED_LIMIT:.0%} of {trials} trials did not test recovery",
            )
        pending.append(planner.redraw(plan.window))
    return finish("pass", EXIT_PASS)


# output -------------------------------------------------------------------------------------


def _interval(cell: dict[str, Any]) -> str:
    return f"{cell['ok']}/{cell['n']} ({cell['wilson_lo']:.2f} to {cell['wilson_hi']:.2f})"


def render_results(report: dict[str, Any], command: str) -> str:
    """The generated part of the doc: headline, invariants, per-window counts, kill points."""
    agg = report["aggregates"]
    counted = [r for r in report["trials"] if classify(r) == "counted"]
    shown = [w for w in WINDOWS if agg["by_window"][w]["n"]]
    state = "dirty tree" if report.get("git_dirty") else "clean tree"
    lines = [
        f"Command: `{command}`. Seed {report['seed']}, "
        f"git `{report.get('git_sha', 'unknown')[:9]}` ({state} when the run started).",
        "",
        f"**Verdict: {report['verdict']}** ({report['counted']} of {report['trials_requested']} "
        f"trials counted; not fired {report['excluded']['not_fired']}, "
        f"late kill {report['excluded']['late_kill']}; wall time {report['wall_s']} s).",
    ]
    if report["reason"]:
        lines.append(f"Reason: {report['reason']}")
    if report["first_violation"]:
        v = report["first_violation"]
        lines.append(
            f"First violation: trial {v['trial']}, `{v['invariant']}` ({v['kind']}): {v['detail']}"
        )
    lines += [
        "",
        "Trials that met each invariant, with a Wilson 95% interval:",
        "",
        "| invariant | overall | " + " | ".join(shown) + " |",
        "|---|---|" + "---|" * len(shown),
    ]
    for name in INVARIANTS:
        cells = [_interval(agg["overall"][name])] + [
            _interval(agg["by_window"][w][name]) for w in shown
        ]
        lines.append(f"| `{name}` | " + " | ".join(cells) + " |")
    lines += ["", "Kill points (counted trials):", ""]
    lines += [
        "| trial | window | point | attempts | rows | kill to result s | takeover gap s | result |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for r in counted:
        kill, t = r["kill"] or {}, r["takeover"]
        plan = r["plan"]
        point = kill.get("point", "?")
        if plan["window"] == RANDOM_TIME:
            point = f"harness +{kill.get('offset_s', 0):.2f} s"
        elif plan["k"] is not None:
            point = f"{point} #{plan['k']}"
        gap = f"{t['gap_s']:.2f}" if t.get("gap_s") is not None else "n/a"
        took = r["kill_to_result_s"]
        failed = ", ".join(f"FAIL {v['invariant']} ({v['kind']})" for v in r["violations"])
        lines.append(
            f"| {plan['index']} | {plan['window']} | {point} | {r['attempts']} | "
            f"{r['result_rows']} | {'n/a' if took is None else f'{took:.2f}'} | {gap} | "
            f"{failed or 'ok'} |"
        )
    times = [r["kill_to_result_s"] for r in counted if r["kill_to_result_s"] is not None]
    if times:
        lines += [
            "",
            f"Kill to result: min {min(times):.2f} s, median {statistics.median(times):.2f} s, "
            f"max {max(times):.2f} s.",
        ]
    return "\n".join(lines)


def splice_results(doc: str, block: str, results_id: str = RESULTS_ID) -> str:
    """Replace what is between `<!-- ID:start -->` and `<!-- ID:end -->`; the rest stays as is."""
    open_, close = f"<!-- {results_id}:start -->", f"<!-- {results_id}:end -->"
    start, end = doc.find(open_), doc.find(close)
    if start < 0 or end < start:
        raise ValueError(f"the doc needs {open_} and {close} markers")
    return doc[: start + len(open_)] + "\n\n" + block + "\n\n" + doc[end:]


def compact(report: dict[str, Any]) -> dict[str, Any]:
    """The committed form: no polled samples and no temp paths."""
    slim = json.loads(json.dumps(report))
    for record in slim["controls"] + slim["trials"]:
        record.pop("samples", None)
        record.pop("workdir", None)
    return slim


def _write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, sort_keys=True) + "\n")
    os.replace(tmp, path)


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10, check=True
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip()


def _publish(report: dict[str, Any], args: argparse.Namespace) -> None:
    """Write the compact JSON and fill the doc's result markers, as asked."""
    if args.runs_json:
        _write_json(args.runs_json, compact(report))
    if args.markdown:
        block = render_results(report, report.get("command", "uv run python -m src.swarm.chaos"))
        args.markdown.write_text(splice_results(args.markdown.read_text(), block, args.results_id))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.swarm.chaos", description=__doc__)
    parser.add_argument("--trials", type=int, default=20)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--lease-s", type=float, default=DEFAULT_LEASE_S)
    parser.add_argument("--step-delay-s", type=float, default=DEFAULT_STEP_DELAY_S)
    parser.add_argument("--trial-timeout-s", type=float, default=DEFAULT_TRIAL_TIMEOUT_S)
    parser.add_argument("--max-runtime-s", type=float, default=DEFAULT_MAX_RUNTIME_S)
    parser.add_argument("--out", type=Path, help="full JSON (default logs/chaos/...-seed<N>.json)")
    parser.add_argument("--runs-json", type=Path, help="compact JSON, for committing")
    parser.add_argument("--markdown", type=Path, help="doc whose result markers are filled in")
    parser.add_argument("--results-id", default=RESULTS_ID, help="marker name in the doc")
    parser.add_argument(
        "--windows",
        help="only these windows, e.g. pre_ack:in_tx (window[:point], comma separated)",
    )
    parser.add_argument(
        "--from-json", type=Path, help="do not run: write --runs-json/--markdown from this JSON"
    )
    parser.add_argument("--keep", action="store_true", help="keep every trial's temp directory")
    args = parser.parse_args(argv)
    if args.lease_s <= 0 or args.step_delay_s < 0 or args.trials < 1:
        parser.error("--lease-s must be positive, --step-delay-s not negative, --trials at least 1")

    if args.from_json:
        report = json.loads(args.from_json.read_text())
        _publish(report, args)
        return report["exit_code"]
    try:
        windows = parse_windows(args.windows) if args.windows else None
    except ValueError as e:
        parser.error(str(e))

    os.environ.pop("ANTHROPIC_API_KEY", None)  # nothing below may reach a live model
    git_sha = _git("rev-parse", "HEAD") or "unknown"
    git_dirty = bool(_git("status", "--porcelain"))  # before the run: outputs must not count
    root = Path(tempfile.mkdtemp(prefix="swarm-chaos-"))
    cfg = Config(root, args.lease_s, args.step_delay_s, args.trial_timeout_s)
    command = f"uv run python -m src.swarm.chaos --trials {args.trials} --seed {args.seed}"
    if args.windows:
        command += f" --windows {args.windows}"

    def control_fn() -> tuple[dict[str, Any], bytes]:
        record, data = run_trial(None, cfg)
        cleanup(record)
        return record, data

    def trial_fn(plan: TrialPlan, control_s: float, control_bytes: bytes) -> dict[str, Any]:
        return run_trial(plan, cfg, control_s=control_s, control_bytes=control_bytes)[0]

    def cleanup(record: dict[str, Any]) -> None:
        if not args.keep and not record["violations"]:
            shutil.rmtree(record["workdir"], ignore_errors=True)

    report = run_experiment(
        args.trials,
        args.seed,
        control_fn=control_fn,
        trial_fn=trial_fn,
        max_runtime_s=args.max_runtime_s,
        cleanup=cleanup,
        windows=windows,
    )
    report["command"] = command
    report["git_sha"] = git_sha
    report["git_dirty"] = git_dirty
    report["config"] = {
        "workers": 2,
        "lease_s": cfg.lease_s,
        "step_delay_s": cfg.step_delay_s,
        "trial_timeout_s": cfg.trial_timeout_s,
        "target": DEMO_TARGET,
        "model_factory": FACTORY,
    }
    out = args.out or REPO_ROOT / "logs" / "chaos" / f"failure-injection-seed{args.seed}.json"
    _write_json(out, report)
    _publish(report, args)

    print(
        f"{report['verdict']}: {report['counted']} of {args.trials} trials counted, "
        f"excluded {report['excluded']}, {report['wall_s']} s"
    )
    if report["reason"]:
        print(f"reason: {report['reason']}")
    if report["first_violation"]:
        print(f"first violation: {report['first_violation']}")
        failed = next((r for r in report["trials"] if r["violations"]), None)
        if failed:
            print(f"kept for inspection: {failed['workdir']}")
    print(f"full JSON: {out}")
    if not any(r["violations"] for r in report["trials"]) and not args.keep:
        shutil.rmtree(root, ignore_errors=True)
    return report["exit_code"]


if __name__ == "__main__":
    sys.exit(main())
