"""Run provenance: what produced a result, recorded in meta.json, the result row and the log header.

Content hashes cover the inputs that change what the model sees or what the numbers mean (rendered
system prompt, the tool list as sent, the target tree, the price row), so a changed input is caught
even when `PROMPT_VERSION` or a model name was not bumped. Git state and library versions say what
code ran. `src/contracts.py` is not involved: the log header is its own record type.
"""

import hashlib
import json
import platform
import subprocess
from collections.abc import Callable
from datetime import UTC, datetime
from importlib import metadata
from pathlib import Path
from typing import Any

from pydantic import BaseModel, ConfigDict

from src.agent.pricing import VERIFIED_ON, price_table_version
from src.agent.prompts import PROMPT_VERSION

LOCK_NAME = "targets.lock.json"
# Not part of "the code that ran": earlier --save runs write results there.
DIRTY_EXCLUDES = ("evals/results", "evals/baselines")
GIT_TIMEOUT_S = 30

# (args, cwd) -> stdout, or None if git is unavailable or the command failed
GitRunner = Callable[[list[str], Path], str | None]


class Provenance(BaseModel):
    model_config = ConfigDict(extra="forbid")

    model: str
    prompt_version: str
    system_prompt_sha256: str
    tool_schemas_sha256: str
    tool_names: list[str]
    # Request settings; None when the client does not expose them (test doubles).
    effort: str | None = None
    max_tokens: int | None = None
    caching: bool | None = None
    git_commit: str | None  # None = git unavailable
    git_dirty: bool | None  # None = unknown, which is not the same as clean
    target_sha256: str  # live tree: sha256 of {repo-relative path: sha256 of bytes}
    target_lock_sha256: str | None  # the same map as in targets.lock.json; None if not locked
    price_table_version: str | None
    price_verified_on: str
    sandbox_mode: str  # "none" or "docker"
    max_steps: int
    python_version: str
    anthropic_sdk_version: str | None
    started_at: str
    finished_at: str | None = None  # set when the run ends; absent from the log header


def sha256_json(obj: Any) -> str:
    blob = json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(blob.encode()).hexdigest()


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def tree_hashes(target: Path, repo_root: Path) -> dict[str, str]:
    """Repo-relative POSIX path -> sha256 of the bytes, skipping `__pycache__` and `.pyc`.

    The same rule and keys as `evals/frozen.py`, so a frozen target's tree matches its lock.
    """
    out: dict[str, str] = {}
    for path in sorted(target.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
            rel = path.relative_to(repo_root).as_posix()
            out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def lock_hash(target: Path, repo_root: Path) -> str | None:
    """Hash of this target's entries in the lock file; None if it is not locked or no lock."""
    try:
        files = json.loads((repo_root / "evals" / LOCK_NAME).read_text())["files"]
    except (OSError, ValueError, KeyError, TypeError):
        return None
    prefix = target.relative_to(repo_root).as_posix() + "/"
    mine = {p: h for p, h in files.items() if p.startswith(prefix)}
    return sha256_json(mine) if mine else None


def run_git(args: list[str], cwd: Path) -> str | None:
    try:
        out = subprocess.run(
            ["git", *args], cwd=cwd, capture_output=True, text=True, timeout=GIT_TIMEOUT_S
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout if out.returncode == 0 else None


def git_state(repo_root: Path, git: GitRunner = run_git) -> tuple[str | None, bool | None]:
    """(full commit sha, dirty) for the repo; each None when git cannot say."""
    head = git(["rev-parse", "HEAD"], repo_root)
    excludes = [f":(exclude){p}" for p in DIRTY_EXCLUDES]
    status = git(
        ["status", "--porcelain", "--untracked-files=all", "--", ".", *excludes], repo_root
    )
    commit = (head.strip() or None) if head is not None else None
    return commit, None if status is None else bool(status.strip())


def _sdk_version() -> str | None:
    try:
        return metadata.version("anthropic")
    except metadata.PackageNotFoundError:
        return None


def _utc(now: datetime) -> str:
    return now.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def collect(
    *,
    model: Any,
    system_prompt: str,
    tools: list[dict[str, Any]],
    sandbox_mode: str,
    max_steps: int,
    target: Path,
    repo_root: Path,
    now: datetime | None = None,
    git: GitRunner = run_git,
) -> Provenance:
    """Everything knowable at the start of a run. `tools` is the list exactly as sent.

    `model` is a `ModelClient`; `effort`, `max_tokens` and `caching` are read if it has them.
    """
    commit, dirty = git_state(repo_root, git)
    return Provenance(
        model=model.model,
        prompt_version=PROMPT_VERSION,
        system_prompt_sha256=sha256_text(system_prompt),
        tool_schemas_sha256=sha256_json(tools),
        tool_names=[t["name"] for t in tools],
        effort=getattr(model, "effort", None),
        max_tokens=getattr(model, "max_tokens", None),
        caching=getattr(model, "caching", None),
        git_commit=commit,
        git_dirty=dirty,
        target_sha256=sha256_json(tree_hashes(target, repo_root)),
        target_lock_sha256=lock_hash(target, repo_root),
        price_table_version=price_table_version(model.model),
        price_verified_on=VERIFIED_ON,
        sandbox_mode=sandbox_mode,
        max_steps=max_steps,
        python_version=platform.python_version(),
        anthropic_sdk_version=_sdk_version(),
        started_at=_utc(now or datetime.now(UTC)),
    )


def finished(p: Provenance, now: datetime | None = None) -> Provenance:
    return p.model_copy(update={"finished_at": _utc(now or datetime.now(UTC))})
