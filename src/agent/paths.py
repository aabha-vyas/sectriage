"""Path confinement and output caps shared by the agent tools."""

import stat
from pathlib import Path

MAX_OUTPUT_BYTES = 16 * 1024
TRUNCATION_MARKER = "\n[output truncated]"


class PathError(ValueError):
    """A model-supplied path is not allowed. The message is safe to show the model."""


def resolve_root(target_path: str | Path) -> Path:
    root = Path(target_path).resolve(strict=True)
    if not root.is_dir():
        raise PathError("target is not a directory")
    return root


def safe_resolve(root: Path, rel: str) -> Path:
    """Resolve `rel` under `root`, following symlinks, and reject anything that lands outside.

    `root` must already be resolved (see `resolve_root`).
    """
    if not rel or "\0" in rel:
        raise PathError("invalid path")
    if rel.startswith(("/", "\\")) or Path(rel).is_absolute() or (len(rel) > 1 and rel[1] == ":"):
        raise PathError("path must be relative to the target")
    try:
        candidate = (root / rel).resolve()
    except (OSError, RuntimeError):  # symlink loops raise RuntimeError on 3.12
        raise PathError("cannot resolve path") from None
    if not candidate.is_relative_to(root):
        raise PathError("path escapes the target directory")
    return candidate


def safe_resolve_file(root: Path, rel: str) -> Path:
    candidate = safe_resolve(root, rel)
    try:
        mode = candidate.stat().st_mode
    except OSError:
        raise PathError("no such file") from None
    if not stat.S_ISREG(mode):
        raise PathError("not a regular file")
    return candidate


def relative_posix(root: Path, path: Path) -> str:
    return path.relative_to(root).as_posix()


def sandbox_path(root: Path, rel: str) -> str:
    """Validate that `rel` exists inside the target and return it as an argv-safe relative path."""
    candidate = safe_resolve(root, rel)
    if not candidate.exists():
        raise PathError("no such path")
    out = relative_posix(root, candidate)
    return f"./{out}" if out.startswith("-") else out


def cap_output(text: str, limit: int = MAX_OUTPUT_BYTES) -> tuple[str, bool]:
    """Cut `text` to at most `limit` UTF-8 bytes (marker included). Returns (text, truncated)."""
    data = text.encode("utf-8", errors="replace")
    if len(data) <= limit:
        return text, False
    keep = max(limit - len(TRUNCATION_MARKER.encode()), 0)
    return data[:keep].decode("utf-8", errors="ignore") + TRUNCATION_MARKER, True
