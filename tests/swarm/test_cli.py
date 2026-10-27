import subprocess
import sys
from pathlib import Path

import pytest

from src.agent.events import read_events
from src.agent.loop import REPO_ROOT
from src.swarm.cli import ALREADY_QUEUED, main
from src.swarm.fake_model import DELAY_ENV, DEMO_MODEL
from src.swarm.queue import QueueCounts
from src.swarm.sqlite_queue import SqliteQueue
from tests.swarm.fakes import TARGET, expected_finding_ids

FACTORY = "src.swarm.fake_model:demo_factory"
TARGET_ARG = "evals/targets/vuln_app"


@pytest.fixture(autouse=True)
def _env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PYTHONPATH", str(REPO_ROOT))  # `work` spawns processes
    monkeypatch.setenv(DELAY_ENV, "0")


@pytest.fixture
def paths(tmp_path: Path) -> list[str]:
    return ["--db", str(tmp_path / "queue.db"), "--events", str(tmp_path / "events")]


@pytest.fixture
def run(paths: list[str], capsys: pytest.CaptureFixture[str]):
    """Run the CLI in process: (exit code, stdout, stderr)."""

    def call(*args: str) -> tuple[int, str, str]:
        code = main([args[0], *paths, *args[1:]])
        out = capsys.readouterr()
        return code, out.out, out.err

    return call


def queue_of(tmp_path: Path) -> SqliteQueue:
    return SqliteQueue(tmp_path / "queue.db", target_check=None)


# enqueue ------------------------------------------------------------------------------------


def test_enqueue_names_tasks_target_batch_index(run, tmp_path: Path):
    code, out, err = run("enqueue", "--target", TARGET_ARG, "-n", "3", "--batch", "b1")

    assert (code, err) == (0, "")
    assert out.strip() == "enqueued 3 of 3"
    queue = queue_of(tmp_path)
    assert queue.status() == QueueCounts(queued=3)
    for i in (1, 2, 3):
        record = queue.get(f"vuln_app-b1-{i}")
        assert record is not None and record.task.target_path == str(TARGET)
    assert queue.get("vuln_app-b1-4") is None


def test_enqueue_again_says_already_queued_and_leaves_the_payload(run, tmp_path: Path):
    run("enqueue", "--target", TARGET_ARG, "-n", "2", "--batch", "b1", "--max-steps", "7")

    code, out, _ = run(
        "enqueue", "--target", TARGET_ARG, "-n", "3", "--batch", "b1", "--max-steps", "99"
    )

    assert code == 0
    assert out.splitlines() == [
        f"vuln_app-b1-1: {ALREADY_QUEUED}",
        f"vuln_app-b1-2: {ALREADY_QUEUED}",
        "enqueued 1 of 3 (2 skipped)",
    ]
    queue = queue_of(tmp_path)
    assert queue.status() == QueueCounts(queued=3)
    first, third = queue.get("vuln_app-b1-1"), queue.get("vuln_app-b1-3")
    assert first is not None and first.task.max_steps == 7  # unchanged
    assert third is not None and third.task.max_steps == 99


def test_enqueue_into_a_full_queue_is_an_error(run, tmp_path: Path):
    code, out, err = run(
        "enqueue", "--target", TARGET_ARG, "-n", "3", "--batch", "b1", "--max-depth", "2"
    )

    assert code == 2 and out == ""
    assert err.startswith("error: queue is full")
    assert "2 of 3 enqueued before it filled" in err
    assert queue_of(tmp_path).status() == QueueCounts(queued=2)


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--target", "evals/targets/nope", "--batch", "b"], "does not exist"),
        (["--target", ".", "--batch", "b"], "inside evals/targets"),
        (["--target", TARGET_ARG, "--batch", "has space"], "invalid task id"),
        (["--target", TARGET_ARG, "--batch", "b", "-n", "0"], "at least 1"),
    ],
)
def test_enqueue_errors_go_to_stderr_with_exit_2(run, args: list[str], message: str):
    code, out, err = run("enqueue", *args)
    assert code == 2 and out == ""
    assert err.startswith("error: ") and message in err and err.count("\n") == 1


def test_db_and_events_must_stay_out_of_the_targets_tree(capsys: pytest.CaptureFixture[str]):
    inside = REPO_ROOT / "evals" / "targets" / "vuln_app"
    for flag in ("--db", "--events"):
        code = main(["status", flag, str(inside / "x")])
        assert code == 2
        assert "must not be inside evals/targets" in capsys.readouterr().err
    assert not (inside / "x").exists()


# status -------------------------------------------------------------------------------------


def test_status_prints_a_table(run):
    run("enqueue", "--target", TARGET_ARG, "-n", "2", "--batch", "b1")
    code, out, _ = run("status")
    assert code == 0
    assert [line.split() for line in out.splitlines()] == [
        ["state", "count"],
        ["queued", "2"],
        ["running", "0"],
        ["done", "0"],
        ["dead", "0"],
        ["active", "2"],
    ]


@pytest.mark.parametrize(
    "command", [["status"], ["replay", "x"], ["work", "--model-factory", FACTORY]]
)
def test_a_missing_database_is_an_error_not_an_empty_queue(run, tmp_path: Path, command):
    code, out, err = run(*command)
    assert code == 2 and out == ""
    assert err.startswith("error: no queue database at ")
    assert not (tmp_path / "queue.db").exists()


# work ---------------------------------------------------------------------------------------


def test_work_requires_a_model_factory(run, capsys: pytest.CaptureFixture[str]):
    run("enqueue", "--target", TARGET_ARG, "--batch", "b1")
    with pytest.raises(SystemExit) as exc:
        run("work")
    assert exc.value.code == 2
    assert "--model-factory" in capsys.readouterr().err


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--model-factory", "nonsense"], "must look like"),
        (["--model-factory", "no.such.module:thing"], "cannot load model factory"),
        (["--model-factory", FACTORY, "--workers", "0"], "must be positive"),
        (["--model-factory", FACTORY, "--lease-s", "0"], "must be positive"),
    ],
)
def test_work_rejects_bad_arguments(run, args: list[str], message: str):
    run("enqueue", "--target", TARGET_ARG, "--batch", "b1")
    code, out, err = run("work", *args)
    assert code == 2 and out == ""
    assert err.startswith("error: ") and message in err


def test_work_drains_the_queue_and_replay_matches(run, tmp_path: Path):
    run("enqueue", "--target", TARGET_ARG, "-n", "2", "--batch", "b1")

    code, _, err = run("work", "--workers", "2", "--model-factory", FACTORY)

    assert (code, err) == (0, "")
    queue = queue_of(tmp_path)
    assert queue.status() == QueueCounts(done=2)
    for task_id in ("vuln_app-b1-1", "vuln_app-b1-2"):
        result = queue.result(task_id)
        assert result is not None and result.stop_reason == "finish"
        assert {f.finding_id for f in result.findings} == expected_finding_ids()
        assert {e.model for e in read_events(result.log_path)} == {DEMO_MODEL}

    code, out, _ = run("replay", "vuln_app-b1-1")
    assert code == 0
    assert out.splitlines()[0] == "vuln_app-b1-1: identical (5 recorded, 5 replayed)"
    assert "replay log: " in out


# replay -------------------------------------------------------------------------------------


def test_replay_prints_the_diff_and_exits_1_when_findings_differ(run, tmp_path: Path):
    run("enqueue", "--target", TARGET_ARG, "--batch", "b1")
    run("work", "--model-factory", FACTORY)
    log = Path(queue_of(tmp_path).result("vuln_app-b1-1").log_path)  # type: ignore[union-attr]
    log.write_text("".join(e.to_jsonl() + "\n" for e in read_events(log)[:3]))  # cut the log short

    code, out, err = run("replay", "vuln_app-b1-1")

    assert (code, err) == (1, "")
    lines = out.splitlines()
    assert lines[0] == "vuln_app-b1-1: findings differ (5 recorded, 2 replayed)"
    assert any(line.startswith("-") for line in lines)
    assert "note: stop finish recorded, model_error replayed (replay log exhausted)" in lines


def test_replay_of_an_unknown_or_unfinished_task_is_an_error(run):
    run("enqueue", "--target", TARGET_ARG, "--batch", "b1")
    code, _, err = run("replay", "nope")
    assert code == 2 and err == "error: unknown task: nope\n"
    code, _, err = run("replay", "vuln_app-b1-1")
    assert code == 2 and err == "error: vuln_app-b1-1 has no result yet (state: queued)\n"


# wiring -------------------------------------------------------------------------------------


def test_python_dash_m_src_swarm_runs_the_cli(tmp_path: Path):
    db = tmp_path / "queue.db"
    base = [sys.executable, "-m", "src.swarm"]
    enqueue = [*base, "enqueue", "--db", str(db), "--target", TARGET_ARG, "--batch", "m"]
    status = [*base, "status", "--db", str(db)]
    for argv in (enqueue, status):
        done = subprocess.run(argv, cwd=REPO_ROOT, capture_output=True, text=True, timeout=60)
        assert done.returncode == 0, done.stderr
    assert "queued   1" in done.stdout
