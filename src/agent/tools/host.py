"""Read-only tools that run on the host, confined to the target directory.

Nothing here executes model-supplied input: `search_code` is a literal substring match, not a regex.
"""

import os
import stat
from collections.abc import Iterator
from pathlib import Path

from src.agent.paths import PathError, relative_posix, safe_resolve, safe_resolve_file
from src.agent.tools.base import Outcome, ToolError
from src.contracts import ReadFileArgs, SearchCodeArgs

MAX_READ_BYTES = 1024 * 1024
MAX_PATTERN_CHARS = 200
MAX_MATCHES = 200
MAX_FILES_SCANNED = 2000
MAX_LINE_CHARS = 300
_BINARY_SNIFF_BYTES = 8192
_SKIP_DIRS = frozenset({".git", "__pycache__", "node_modules", ".venv"})


def _lines(text: str) -> list[str]:
    """Split on "\\n" only, so line numbers match editors and the ground truth."""
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def _is_binary(head: bytes) -> bool:
    return b"\0" in head[:_BINARY_SNIFF_BYTES]


def read_file(root: Path, args: ReadFileArgs) -> Outcome:
    try:
        path = safe_resolve_file(root, args.path)
    except PathError as e:
        raise ToolError(str(e)) from None
    with open(path, "rb") as f:
        data = f.read(MAX_READ_BYTES + 1)
    truncated = len(data) > MAX_READ_BYTES
    data = data[:MAX_READ_BYTES]
    if _is_binary(data):
        raise ToolError("binary file")
    lines = _lines(data.decode("utf-8", errors="replace"))
    start = args.start_line or 1
    end = args.end_line or len(lines)
    if lines and start > len(lines):
        raise ToolError(f"start_line {start} is past the end of the file ({len(lines)} lines)")
    numbered = (f"{n}: {line}" for n, line in enumerate(lines[start - 1 : end], start))
    return Outcome(output="\n".join(numbered), truncated=truncated)


def line_count(root: Path, rel: str) -> int:
    """Number of lines in a target file, counted the same way `read_file` numbers them."""
    path = safe_resolve_file(root, rel)
    count, last = 0, b"\n"
    with open(path, "rb") as f:
        while chunk := f.read(65536):
            count += chunk.count(b"\n")
            last = chunk[-1:]
    return count + (last != b"\n")


def _walk_files(start: Path) -> Iterator[Path]:
    """Regular files under `start` in a stable order, never following or opening symlinks."""
    for dirpath, dirnames, filenames in os.walk(start, followlinks=False):
        here = Path(dirpath)
        dirnames[:] = sorted(
            d for d in dirnames if d not in _SKIP_DIRS and not (here / d).is_symlink()
        )
        for name in sorted(filenames):
            path = here / name
            try:
                if stat.S_ISREG(path.lstat().st_mode):
                    yield path
            except OSError:
                continue


def search_code(root: Path, args: SearchCodeArgs) -> Outcome:
    needle = args.pattern
    if not needle or len(needle) > MAX_PATTERN_CHARS or "\0" in needle or "\n" in needle:
        raise ToolError(f"pattern must be 1-{MAX_PATTERN_CHARS} characters on a single line")
    try:
        start = safe_resolve(root, args.path or ".")
    except PathError as e:
        raise ToolError(str(e)) from None
    if not start.exists():
        raise ToolError("no such path")

    files = iter([start]) if start.is_file() else _walk_files(start)
    matches: list[str] = []
    scanned = 0
    truncated = False
    for path in files:
        scanned += 1
        if scanned > MAX_FILES_SCANNED:
            truncated = True
            break
        try:
            with open(path, "rb") as f:
                data = f.read(MAX_READ_BYTES + 1)
        except OSError:
            continue
        if len(data) > MAX_READ_BYTES or _is_binary(data):
            continue
        rel = relative_posix(root, path)
        for n, line in enumerate(_lines(data.decode("utf-8", errors="replace")), 1):
            if needle in line:
                matches.append(f"{rel}:{n}: {line[:MAX_LINE_CHARS]}")
                if len(matches) >= MAX_MATCHES:
                    truncated = True
                    break
        if truncated:
            break
    if not matches:
        return Outcome(output="no matches", truncated=truncated)
    return Outcome(output="\n".join(matches), truncated=truncated)
