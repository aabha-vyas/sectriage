import subprocess
from pathlib import Path

import pytest

from src.agent.paths import (
    MAX_OUTPUT_BYTES,
    TRUNCATION_MARKER,
    PathError,
    cap_output,
    relative_posix,
    resolve_root,
    safe_resolve,
    safe_resolve_file,
    sandbox_path,
)


@pytest.mark.parametrize(
    "rel",
    [
        "../outside/secret.txt",
        "docs/../../outside/secret.txt",
        "/etc/passwd",
        "\\x",
        "C:\\x",
        "",
        "a\0b",
    ],
)
def test_safe_resolve_rejects_bad_paths(target: Path, rel: str):
    with pytest.raises(PathError):
        safe_resolve(target, rel)


@pytest.mark.parametrize("rel", ["link_file", "link_dir", "link_dir/secret.txt"])
def test_safe_resolve_rejects_symlink_escapes(target: Path, rel: str):
    with pytest.raises(PathError, match="escapes"):
        safe_resolve(target, rel)


def test_safe_resolve_rejects_symlink_loop(target: Path):
    (target / "loop_a").symlink_to(target / "loop_b")
    (target / "loop_b").symlink_to(target / "loop_a")
    with pytest.raises(PathError):
        safe_resolve(target, "loop_a")


def test_safe_resolve_allows_inside_paths_and_inner_symlinks(target: Path):
    assert safe_resolve(target, "docs/readme.txt") == target / "docs" / "readme.txt"
    assert safe_resolve(target, "docs/../app.py") == target / "app.py"
    assert safe_resolve(target, "inner_link") == target / "app.py"
    assert safe_resolve(target, ".") == target


def test_errors_do_not_leak_host_paths(target: Path, outside: Path):
    with pytest.raises(PathError) as e:
        safe_resolve(target, "link_file")
    assert str(outside) not in str(e.value)
    assert str(target) not in str(e.value)


def test_safe_resolve_file_requires_a_regular_file(target: Path):
    assert safe_resolve_file(target, "app.py") == target / "app.py"
    with pytest.raises(PathError, match="not a regular file"):
        safe_resolve_file(target, "docs")
    with pytest.raises(PathError, match="no such file"):
        safe_resolve_file(target, "missing.py")


def test_resolve_root_requires_a_directory(target: Path):
    with pytest.raises(PathError):
        resolve_root(target / "app.py")
    with pytest.raises(FileNotFoundError):
        resolve_root(target / "nope")


def test_sandbox_path_is_relative_posix_and_argv_safe(target: Path):
    assert sandbox_path(target, "docs/../docs/readme.txt") == "docs/readme.txt"
    assert sandbox_path(target, ".") == "."
    (target / "-weird.py").write_text("x = 1\n")
    assert sandbox_path(target, "-weird.py") == "./-weird.py"
    with pytest.raises(PathError, match="no such path"):
        sandbox_path(target, "missing.py")
    with pytest.raises(PathError):
        sandbox_path(target, "link_file")


def test_relative_posix(target: Path):
    assert relative_posix(target, target / "docs" / "readme.txt") == "docs/readme.txt"


def test_cap_output_leaves_short_text_alone():
    assert cap_output("hello", 100) == ("hello", False)
    assert cap_output("x" * 100, 100) == ("x" * 100, False)


def test_cap_output_truncates_within_limit_with_marker():
    text, truncated = cap_output("x" * 5000, 1000)
    assert truncated
    assert len(text.encode()) <= 1000
    assert text.endswith(TRUNCATION_MARKER)


def test_cap_output_cuts_on_a_character_boundary():
    # 3-byte characters: a naive byte cut would split one.
    text, truncated = cap_output("€" * 2000, 1001)
    assert truncated
    assert len(text.encode()) <= 1001
    body = text.removesuffix(TRUNCATION_MARKER)
    assert set(body) == {"€"}


def test_default_cap_is_16_kib():
    assert MAX_OUTPUT_BYTES == 16 * 1024


def test_conftest_blocks_host_execution():
    with pytest.raises(RuntimeError, match="forbidden"):
        subprocess.run(["true"], check=False)
