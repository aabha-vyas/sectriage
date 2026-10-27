"""DEMO ONLY: a scripted stand-in for a model. It is not a model and it finds nothing itself.

`demo_factory` plays back a fixed, perfect run over `evals/targets/vuln_app`: it reads `app.py`,
reports every bug listed in `evals/ground_truth/vuln_app.json` (the eval answer key), then
finishes. It exists so the swarm demo and the CLI tests can run the whole queue, worker, lease and
replay path with no network, no API key, no Docker and no spend. Never use it for an eval or to
judge the agent: its "findings" are copied from the answer key.

Use it as `--model-factory src.swarm.fake_model:demo_factory`. The model name it reports is
`DEMO-ONLY-fake-model`, so its events and costs ($0) cannot be mistaken for a real run.

`SWARM_DEMO_STEP_DELAY_S` (seconds, default 0.7) slows each scripted step, so the demo has time to
kill a worker mid-task. Tests set it to 0.
"""

import itertools
import json
import os
import time
from typing import Any

from src.agent.model import ModelError, ModelResponse, Usage
from src.agent.run import TARGETS_DIR

DEMO_MODEL = "DEMO-ONLY-fake-model"
DELAY_ENV = "SWARM_DEMO_STEP_DELAY_S"
DEFAULT_STEP_DELAY_S = 0.7
DEMO_TARGET = "vuln_app"
GROUND_TRUTH = TARGETS_DIR.parent / "ground_truth" / f"{DEMO_TARGET}.json"


def _tool_call(ids: itertools.count, name: str, args: dict[str, Any]) -> ModelResponse:
    block = {"type": "tool_use", "id": f"toolu_demo_{next(ids)}", "name": name, "input": args}
    return ModelResponse(
        content=[block],
        stop_reason="tool_use",
        usage=Usage(input_tokens=100, output_tokens=10),
        latency_ms=5,
    )


def demo_script() -> list[ModelResponse]:
    """Read the file, report every ground-truth bug, finish."""
    truth = json.loads(GROUND_TRUTH.read_text(encoding="utf-8"))["findings"]
    prefix = f"evals/targets/{DEMO_TARGET}/"
    ids = itertools.count(1)
    script = [_tool_call(ids, "read_file", {"path": "app.py"})]
    for bug in truth:
        args = {
            "file": bug["file"].removeprefix(prefix),
            "line_start": bug["line_start"],
            "line_end": bug["line_end"],
            "vuln_class": bug["vuln_class"],
            "severity": bug["severity"],
            "title": bug["id"],
            "explanation": bug["description"],
            "confidence": 0.9,
        }
        script.append(_tool_call(ids, "report_finding", args))
    script.append(_tool_call(ids, "finish", {}))
    return script


class DemoModel:
    """Plays `demo_script` one turn per call, pausing `delay_s` before each."""

    model = DEMO_MODEL

    def __init__(self, delay_s: float):
        self._delay_s = delay_s
        self._script = iter(demo_script())

    def complete(
        self,
        *,
        system: str,
        tools: list[dict[str, Any]],
        messages: list[dict[str, Any]],
        timeout_s: float,
    ) -> ModelResponse:
        if self._delay_s > 0:
            time.sleep(self._delay_s)
        try:
            return next(self._script)
        except StopIteration:
            raise ModelError("demo script exhausted") from None


def demo_factory(model_name: str) -> DemoModel:
    """Every attempt of every task gets a fresh run of the script; `model_name` is ignored."""
    return DemoModel(float(os.environ.get(DELAY_ENV, DEFAULT_STEP_DELAY_S)))
