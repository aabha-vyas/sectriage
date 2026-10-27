"""Test doubles for the swarm lane. Nothing here touches the network, Docker or a model."""

import json
import os
import time
from pathlib import Path

from src.agent.memory import make_finding_id
from src.agent.model import ModelClient, ModelResponse
from src.agent.run import TARGETS_DIR
from src.contracts import Finding, Severity, Task, VulnClass
from src.swarm.queue import TaskResult
from tests.agent.fakes import FakeClock, FakeModel, tool_call

__all__ = [
    "GROUND_TRUTH",
    "HANG_MARKER_ENV",
    "TARGET",
    "FakeClock",
    "FakeModel",
    "clean_factory",
    "expected_finding_ids",
    "hang_once_factory",
    "make_finding",
    "make_result",
    "make_task",
    "tool_call",
    "vuln_app_script",
]

# A seeded, intentionally vulnerable local target. Tests only read it, through the host tools.
TARGET: Path = TARGETS_DIR / "vuln_app"
GROUND_TRUTH: Path = TARGETS_DIR.parent / "ground_truth" / "vuln_app.json"
HANG_MARKER_ENV = "SWARM_FAKE_HANG_MARKER"


def make_task(task_id: str = "t-1", **fields: object) -> Task:
    values: dict[str, object] = {
        "task_id": task_id,
        "target_path": str(TARGET),
        "goal": "review the target",
        "max_steps": 30,
        "timeout_s": 60,
        "model": "claude-haiku-5-5",
        **fields,
    }
    return Task.model_validate(values)


def make_finding(line: int = 3) -> Finding:
    return Finding(
        finding_id=f"f_{line}",
        file="app.py",
        line_start=line,
        line_end=line,
        vuln_class=VulnClass.SQLI,
        severity=Severity.HIGH,
        title="sql injection",
        explanation="user input is concatenated into a query",
        confidence=0.9,
    )


def make_result(**fields: object) -> TaskResult:
    values: dict[str, object] = {
        "stop_reason": "finish",
        "findings": [make_finding()],
        "steps": 4,
        "cost_usd": 0.01,
        "log_path": "events/t-1.1.jsonl",
        **fields,
    }
    return TaskResult(**values)  # type: ignore[arg-type]


def vuln_app_script() -> list[ModelResponse]:
    """A clean run over vuln_app: read the file, report every ground-truth bug, finish."""
    truth = json.loads(GROUND_TRUTH.read_text())["findings"]
    prefix = f"evals/targets/{TARGET.name}/"
    script = [tool_call("read_file", {"path": "app.py"})]
    for bug in truth:
        script.append(
            tool_call(
                "report_finding",
                {
                    "file": bug["file"].removeprefix(prefix),
                    "line_start": bug["line_start"],
                    "line_end": bug["line_end"],
                    "vuln_class": bug["vuln_class"],
                    "severity": bug["severity"],
                    "title": bug["id"],
                    "explanation": bug["description"],
                    "confidence": 0.9,
                },
            )
        )
    script.append(tool_call("finish"))
    return script


def clean_factory(model_name: str) -> ModelClient:
    """Importable as `tests.swarm.fakes:clean_factory`: every call is a fresh clean run."""
    return FakeModel(vuln_app_script())


def hang_once_factory(model_name: str) -> ModelClient:
    """Like `clean_factory`, but the first call of the first run to reach step 2 never returns.

    The marker file (path in `HANG_MARKER_ENV`) records that a run hung, so the retry goes through.
    The test SIGKILLs the hung worker.
    """
    marker = Path(os.environ[HANG_MARKER_ENV])
    calls = 0

    def on_call() -> None:
        nonlocal calls
        calls += 1
        if calls == 2 and not marker.exists():
            marker.touch()
            time.sleep(3600)

    return FakeModel(vuln_app_script(), on_call=on_call)


def expected_finding_ids() -> set[str]:
    """Ids of the ground-truth bugs, as `run_task` derives them from repo-relative paths."""
    return {
        make_finding_id(b["file"], b["line_start"], b["line_end"], b["vuln_class"])
        for b in json.loads(GROUND_TRUTH.read_text())["findings"]
    }
