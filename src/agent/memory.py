"""Short-term scratch memory (per task) and the long-term finding store."""

import hashlib
import json
from typing import Any

from src.contracts import Finding, ToolResult, VulnClass

MAX_NOTES = 20
MAX_NOTE_CHARS = 500


def call_key(name: str, args: dict[str, Any]) -> str:
    return f"{name}:{json.dumps(args, sort_keys=True, default=str)}"


def make_finding_id(file: str, line_start: int, line_end: int, vuln_class: VulnClass | str) -> str:
    key = f"{file}:{line_start}:{line_end}:{VulnClass(vuln_class).value}"
    return "f_" + hashlib.sha1(key.encode()).hexdigest()[:10]


class ScratchMemory:
    """Host-side bookkeeping for one task. It never rewrites the model's message history."""

    def __init__(self) -> None:
        self.notes: list[str] = []
        self.file_lines: dict[str, int] = {}
        self._results: dict[str, ToolResult] = {}

    def note(self, text: str) -> None:
        self.notes.append(text[:MAX_NOTE_CHARS])
        del self.notes[:-MAX_NOTES]

    def seen(self, key: str) -> ToolResult | None:
        return self._results.get(key)

    def remember(self, key: str, result: ToolResult) -> None:
        self._results[key] = result


class FindingStore:
    """Keeps the first finding per `dedupe_key` (same rule as `evals.scorer.dedupe`)."""

    def __init__(self) -> None:
        self._findings: dict[tuple[str, int, int, VulnClass], Finding] = {}

    def add(self, finding: Finding) -> bool:
        if finding.dedupe_key in self._findings:
            return False
        self._findings[finding.dedupe_key] = finding
        return True

    def all(self) -> list[Finding]:
        return list(self._findings.values())

    def __len__(self) -> int:
        return len(self._findings)
