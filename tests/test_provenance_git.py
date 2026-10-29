"""The real git path of run provenance, in a throwaway repo (the agent tests forbid processes)."""

import shutil
import subprocess
from pathlib import Path

import pytest

from src.agent.provenance import git_state

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="git not installed")


def git(repo: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.com", *args],
        cwd=repo,
        check=True,
        capture_output=True,
    )


def test_commit_dirty_flag_and_exclusions(tmp_path: Path):
    git(tmp_path, "init", "-q")
    (tmp_path / "evals" / "results").mkdir(parents=True)
    (tmp_path / "a.py").write_text("x = 1\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-qm", "init")

    commit, dirty = git_state(tmp_path)
    assert commit is not None and len(commit) == 40 and dirty is False

    (tmp_path / "evals" / "results" / "new.json").write_text("{}")  # a prior --save
    assert git_state(tmp_path)[1] is False

    (tmp_path / "untracked.py").write_text("")
    assert git_state(tmp_path)[1] is True
    (tmp_path / "untracked.py").unlink()
    (tmp_path / "a.py").write_text("x = 2\n")
    assert git_state(tmp_path)[1] is True


def test_not_a_repo_is_unknown(tmp_path: Path):
    assert git_state(tmp_path) == (None, None)
