"""Demo: kill a worker mid-task and watch the task finish exactly once.

    python -m src.swarm.demo            # lease 5 s, takes about 20 s

It drives the real CLI as subprocesses with the DEMO-ONLY scripted model in `fake_model.py`:
no network, no API key, no Docker, no spend. The queue and logs live in a temp directory.

1. `enqueue` one task.          2. `work` starts; the demo reads the worker's pid from the queue
database and SIGKILLs it mid-task.          3. A second `work` waits out the 5 s lease, runs the
task again as attempt 2 and finishes it.          4. The demo prints the single result row.
"""

import argparse
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from contextlib import closing
from pathlib import Path

from src.agent.events import read_events
from src.agent.loop import REPO_ROOT
from src.swarm.queue import TaskState
from src.swarm.sqlite_queue import SqliteQueue
from src.swarm.worker import attempt_log_path

FACTORY = "src.swarm.fake_model:demo_factory"
TARGET = "evals/targets/vuln_app"
BATCH = "demo"
TASK_ID = f"vuln_app-{BATCH}-1"
KILL_AFTER_EVENTS = 2  # kill once attempt 1 has logged this many steps: mid-task, not at the end
WAIT_S = 60.0


def say(msg: str = "") -> None:
    print(msg, flush=True)


def cli(*args: str) -> list[str]:
    return [sys.executable, "-m", "src.swarm", *args]


def child_env() -> dict[str, str]:
    """The children never need a key: drop it so nothing can reach a live model."""
    return {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}


def wait_until(condition, what: str) -> None:
    deadline = time.monotonic() + WAIT_S
    while not condition():
        if time.monotonic() >= deadline:
            raise TimeoutError(f"timed out waiting for {what}")
        time.sleep(0.05)


def logged_steps(path: Path) -> int:
    """Events in a log that a worker may be writing right now (a torn last line counts as 0)."""
    try:
        return len(read_events(path))
    except (OSError, ValueError):
        return 0


def run(workdir: Path, lease_s: float) -> int:
    db, events = workdir / "queue.db", workdir / "events"
    paths = ["--db", str(db), "--events", str(events)]
    work = cli(
        "work", *paths, "--workers", "1", "--lease-s", str(lease_s), "--model-factory", FACTORY
    )
    popen = {"cwd": REPO_ROOT, "env": child_env()}
    procs: list[subprocess.Popen] = []

    try:
        say(f"== 1. enqueue one task (queue and logs in {workdir})")
        subprocess.run(
            cli("enqueue", *paths, "--target", TARGET, "-n", "1", "--batch", BATCH),
            check=True,
            **popen,
        )

        say(f"\n== 2. start a worker (lease {lease_s:g} s), then kill it mid-task")
        first = subprocess.Popen(work, **popen)
        procs.append(first)
        queue = SqliteQueue(db, target_check=None)
        log1 = attempt_log_path(events, TASK_ID, 1)

        def mid_task() -> bool:
            record = queue.get(TASK_ID)
            return (
                record is not None
                and record.state is TaskState.RUNNING
                and record.worker_pid is not None
                and logged_steps(log1) >= KILL_AFTER_EVENTS
            )

        wait_until(mid_task, "the worker to be mid-task")
        record = queue.get(TASK_ID)
        assert record is not None and record.worker_pid is not None
        say(
            f"worker pid {record.worker_pid} (from the queue database) has logged "
            f"{logged_steps(log1)} steps of attempt 1; sending SIGKILL"
        )
        os.kill(record.worker_pid, signal.SIGKILL)
        say(f"first `work` exited with code {first.wait(timeout=WAIT_S)}")
        say(f"queue now: {queue.status()}")

        say("\n== 3. start a second worker: it waits out the lease, then runs attempt 2")
        second = subprocess.Popen(work, **popen)
        procs.append(second)
        say(f"second `work` exited with code {second.wait(timeout=WAIT_S)}")

        say("\n== 4. result")
        subprocess.run(cli("status", *paths), check=True, **popen)
        return report(db)
    except (TimeoutError, subprocess.CalledProcessError) as e:
        say(f"DEMO FAILED: {e}")
        return 1
    finally:
        for p in procs:
            if p.poll() is None:
                p.kill()
                p.wait()


def report(db: Path) -> int:
    with closing(sqlite3.connect(db)) as conn:
        rows = conn.execute(
            "SELECT task_id, attempt, stop_reason, steps, cost_usd, log_path FROM results"
        ).fetchall()
        attempts = conn.execute(
            "SELECT attempts FROM tasks WHERE task_id = ?", (TASK_ID,)
        ).fetchone()[0]
    say(f"results rows: {len(rows)}")
    for task_id, attempt, stop, steps, cost, log_path in rows:
        say(
            f"  task_id={task_id} winning_attempt={attempt} stop_reason={stop} steps={steps} "
            f"cost_usd={cost}"
        )
        say(f"  log_path={log_path}")
    say(f"attempts={attempts}")
    ok = len(rows) == 1 and attempts == 2 and rows[0][2] == "finish"
    say("DEMO OK: one result, two attempts" if ok else "DEMO FAILED: expected 1 result, 2 attempts")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m src.swarm.demo")
    parser.add_argument("--lease-s", type=float, default=5.0)
    parser.add_argument("--keep", action="store_true", help="keep the temp directory")
    args = parser.parse_args(argv)
    workdir = Path(tempfile.mkdtemp(prefix="swarm-demo-"))
    try:
        return run(workdir, args.lease_s)
    finally:
        if not args.keep:
            shutil.rmtree(workdir, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
