from pathlib import Path

import anthropic
import pytest

from src.agent.paths import resolve_root


@pytest.fixture(autouse=True)
def _no_network_and_no_host_exec(monkeypatch: pytest.MonkeyPatch) -> None:
    """Agent tests never call the API and never run a process on the host."""

    def _forbidden(*args: object, **kwargs: object) -> None:
        raise RuntimeError("forbidden in agent tests: network or host execution")

    monkeypatch.setattr(anthropic.Anthropic, "__init__", _forbidden)
    monkeypatch.setattr(anthropic.AsyncAnthropic, "__init__", _forbidden)
    monkeypatch.setattr("subprocess.Popen.__init__", _forbidden)
    monkeypatch.setattr("os.system", _forbidden)
    monkeypatch.setattr("os.popen", _forbidden)


def clean_git(args: list[str], cwd: Path) -> str | None:
    """A git that reports commit 40x`c` and a clean tree, without running a process."""
    return "c" * 40 + "\n" if args[0] == "rev-parse" else ""


@pytest.fixture(autouse=True)
def _fake_git(monkeypatch: pytest.MonkeyPatch) -> None:
    """Run provenance asks git for the commit and dirty flag; the CLI default is replaced."""
    monkeypatch.setattr("src.agent.run.run_git", clean_git)


@pytest.fixture
def outside(tmp_path: Path) -> Path:
    d = tmp_path / "outside"
    d.mkdir()
    (d / "secret.txt").write_text("TOP SECRET\n")
    return d


@pytest.fixture
def target(tmp_path: Path, outside: Path) -> Path:
    """A small target tree with symlinks that point outside and inside it."""
    root = tmp_path / "target"
    root.mkdir()
    (root / "app.py").write_text("import os\n\ndef run(cmd):\n    os.system(cmd)\n")
    (root / "rules.yml").write_text("rules: []\n")
    (root / "docs").mkdir()
    (root / "docs" / "readme.txt").write_text("hello world\n")
    (root / "big.txt").write_text("".join(f"line {i} {'x' * 50}\n" for i in range(600)))
    (root / "blob.bin").write_bytes(b"\x00\x01binary")
    (root / "link_file").symlink_to(outside / "secret.txt")
    (root / "link_dir").symlink_to(outside, target_is_directory=True)
    (root / "inner_link").symlink_to(root / "app.py")
    return resolve_root(root)
