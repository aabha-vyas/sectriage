"""Append-only JSONL event log: one line per agent step, flushed before the step returns.

A log may start with one `run_header` line (the run's provenance). It is not an `Event`: readers
skip it with `is_header`, and logs without it, written before provenance existed, still read.
"""

import json
import os
from pathlib import Path
from types import TracebackType
from typing import Self

from src.agent.provenance import Provenance
from src.contracts import Event

HEADER_RECORD = "run_header"
HEADER_VERSION = 1


def header_line(provenance: Provenance) -> str:
    return json.dumps(
        {
            "record": HEADER_RECORD,
            "header_version": HEADER_VERSION,
            "provenance": provenance.model_dump(mode="json"),
        },
        separators=(",", ":"),
    )


def _parse_header(line: str) -> dict | None:
    if HEADER_RECORD not in line:  # cheap prefilter; events are far more common
        return None
    try:
        obj = json.loads(line)
    except ValueError:
        return None
    return obj if isinstance(obj, dict) and obj.get("record") == HEADER_RECORD else None


def is_header(line: str) -> bool:
    return _parse_header(line) is not None


class EventLog:
    """Appends to `path`; reruns append, so callers wanting a fresh log unlink it first."""

    def __init__(self, path: str | Path, header: Provenance | None = None):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")
        if header is not None:
            self._write(header_line(header))

    def _write(self, line: str) -> None:
        self._f.write(line + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

    def append(self, event: Event) -> None:
        self._write(event.to_jsonl())

    def close(self) -> None:
        if not self._f.closed:
            self._f.close()

    def __enter__(self) -> Self:
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        self.close()


def read_events(path: str | Path) -> list[Event]:
    with open(path, encoding="utf-8") as f:
        return [Event.from_jsonl(line) for line in f if line.strip() and not is_header(line)]


def read_header(path: str | Path) -> Provenance | None:
    """The log's provenance header, or None for a log without one. Raises ValueError if newer."""
    with open(path, encoding="utf-8") as f:
        first = next((line for line in f if line.strip()), "")
    obj = _parse_header(first)
    if obj is None:
        return None
    if obj.get("header_version") != HEADER_VERSION:
        raise ValueError(f"unsupported run_header version: {obj.get('header_version')!r}")
    return Provenance.model_validate(obj["provenance"])
