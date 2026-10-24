"""Append-only JSONL event log: one line per agent step, flushed before the step returns."""

import os
from pathlib import Path
from types import TracebackType
from typing import Self

from src.contracts import Event


class EventLog:
    """Appends to `path`; reruns append, so callers wanting a fresh log unlink it first."""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._f = open(self.path, "a", encoding="utf-8")

    def append(self, event: Event) -> None:
        self._f.write(event.to_jsonl() + "\n")
        self._f.flush()
        os.fsync(self._f.fileno())

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
        return [Event.from_jsonl(line) for line in f if line.strip()]
