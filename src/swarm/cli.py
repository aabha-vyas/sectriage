"""CLI: `python -m src.swarm {enqueue,work,status,replay}`.

    enqueue --target evals/targets/vuln_app -n 3 --batch b1     ids vuln_app-b1-1 .. vuln_app-b1-3
    work --workers 2 --lease-s 60 --model-factory pkg.mod:callable
    status
    replay vuln_app-b1-1

`--model-factory` has no default, so nothing can reach a live model by accident. There is no
sandbox flag: workers run the host read-only tools only. The queue database and the event logs
default to `logs/swarm/` and must not be inside `evals/targets/`.
"""

import argparse
import logging
import sqlite3
import sys
from pathlib import Path

from src.agent.loop import DEFAULT_MAX_COST_USD, REPO_ROOT
from src.agent.model import DEFAULT_MODEL
from src.agent.run import GOAL, TARGETS_DIR
from src.contracts import Task
from src.swarm.queue import QueueFullError
from src.swarm.replay import ReplayError, replay_task
from src.swarm.sqlite_queue import DEFAULT_MAX_DEPTH, SqliteQueue
from src.swarm.worker import WorkerPool

DEFAULT_DB = REPO_ROOT / "logs" / "swarm" / "queue.db"
DEFAULT_EVENTS = REPO_ROOT / "logs" / "swarm" / "events"
DEFAULT_LEASE_S = 60.0
ALREADY_QUEUED = "already queued, payload unchanged"


def _fail(msg: str) -> int:
    print(f"error: {msg}", file=sys.stderr)
    return 2


def _check_location(path: Path, what: str) -> Path:
    """The database and the logs are written to, so they must not land in the scanned targets."""
    if path.resolve().is_relative_to(TARGETS_DIR.resolve()):
        raise ValueError(f"{what} {path} must not be inside {TARGETS_DIR.relative_to(REPO_ROOT)}/")
    return path


def _existing_db(path: Path) -> Path:
    if not path.is_file():
        raise ValueError(f"no queue database at {path}; run enqueue first")
    return path


def _enqueue(args: argparse.Namespace) -> int:
    if args.n < 1 or args.max_depth < 1:
        raise ValueError("-n and --max-depth must be at least 1")
    queue = SqliteQueue(args.db, max_depth=args.max_depth)
    try:
        target = Path(args.target).resolve()  # "." or "a/../b" must name the real directory
        added = skipped = 0
        for i in range(1, args.n + 1):
            task = Task(
                task_id=f"{target.name}-{args.batch}-{i}",
                target_path=str(target),
                goal=GOAL,
                max_steps=args.max_steps,
                timeout_s=args.timeout_s,
                model=args.model,
            )
            try:
                fresh = queue.enqueue(task)
            except QueueFullError as e:
                raise QueueFullError(f"{e} ({added} of {args.n} enqueued before it filled)") from e
            if fresh:
                added += 1
            else:
                skipped += 1
                print(f"{task.task_id}: {ALREADY_QUEUED}")
        print(f"enqueued {added} of {args.n}" + (f" ({skipped} skipped)" if skipped else ""))
    finally:
        queue.close()
    return 0


def _work(args: argparse.Namespace) -> int:
    _existing_db(args.db)
    pool = WorkerPool(
        args.db,
        args.events,
        args.model_factory,
        workers=args.workers,
        lease_s=args.lease_s,
        max_cost_usd=args.max_cost_usd,
    )
    return pool.run()


def _status(args: argparse.Namespace) -> int:
    queue = SqliteQueue(_existing_db(args.db), target_check=None)
    try:
        counts = queue.status()
    finally:
        queue.close()
    rows = [
        ("queued", counts.queued),
        ("running", counts.running),
        ("done", counts.done),
        ("dead", counts.dead),
        ("active", counts.active),
    ]
    print(f"{'state':<8} count")
    for state, n in rows:
        print(f"{state:<8} {n}")
    return 0


def _replay(args: argparse.Namespace) -> int:
    queue = SqliteQueue(_existing_db(args.db), target_check=None)
    try:
        report = replay_task(queue, args.task_id)
    finally:
        queue.close()
    print(report.render())
    return report.exit_code


def build_parser() -> argparse.ArgumentParser:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--db", type=Path, default=DEFAULT_DB, help="queue database file")
    common.add_argument("--events", type=Path, default=DEFAULT_EVENTS, help="event log directory")

    parser = argparse.ArgumentParser(prog="python -m src.swarm")
    sub = parser.add_subparsers(dest="command", required=True)

    p = sub.add_parser("enqueue", parents=[common], help="add tasks to the queue")
    p.add_argument("--target", required=True, help="a dir under evals/targets/")
    p.add_argument("-n", type=int, default=1, help="how many tasks")
    p.add_argument("--batch", required=True, help="batch name: ids are <target>-<batch>-<i>")
    p.add_argument("--max-steps", type=int, default=30)
    p.add_argument("--timeout-s", type=int, default=600)
    p.add_argument("--model", default=DEFAULT_MODEL, help="model name handed to the factory")
    p.add_argument("--max-depth", type=int, default=DEFAULT_MAX_DEPTH, help="queued + running cap")
    p.set_defaults(run=_enqueue)

    p = sub.add_parser("work", parents=[common], help="run workers until the queue is drained")
    p.add_argument("--workers", type=int, default=1)
    p.add_argument("--lease-s", type=float, default=DEFAULT_LEASE_S)
    p.add_argument("--max-cost-usd", type=float, default=DEFAULT_MAX_COST_USD, help="per task")
    p.add_argument(
        "--model-factory", required=True, help="module:callable taking a model name; no default"
    )
    p.set_defaults(run=_work)

    p = sub.add_parser("status", parents=[common], help="count tasks by state")
    p.set_defaults(run=_status)

    p = sub.add_parser("replay", parents=[common], help="re-run a finished task from its log")
    p.add_argument("task_id")
    p.set_defaults(run=_replay)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s: %(message)s")
    try:
        _check_location(args.db, "database")
        _check_location(args.events, "event directory")
        return args.run(args)
    except (ValueError, QueueFullError, ReplayError, sqlite3.Error) as e:
        return _fail(str(e))
