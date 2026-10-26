"""Test doubles for the swarm lane. Nothing here touches the network, Docker or a model."""

from pathlib import Path

from src.agent.run import TARGETS_DIR
from src.contracts import Finding, Severity, Task, VulnClass
from src.swarm.queue import TaskResult
from tests.agent.fakes import FakeClock

__all__ = ["TARGET", "FakeClock", "make_finding", "make_result", "make_task"]

# A seeded, intentionally vulnerable local target; the queue only validates that it is allowed.
TARGET: Path = TARGETS_DIR / "vuln_app"


def make_task(task_id: str = "t-1", **fields: object) -> Task:
    values: dict[str, object] = {
        "task_id": task_id,
        "target_path": str(TARGET),
        "goal": "review the target",
        "max_steps": 5,
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
