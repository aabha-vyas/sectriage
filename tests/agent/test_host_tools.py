from pathlib import Path

import pytest

from src.agent.tools import host
from src.agent.tools.base import ToolError
from src.contracts import ReadFileArgs, SearchCodeArgs


def read(root: Path, path: str, **kw: int) -> host.Outcome:
    return host.read_file(root, ReadFileArgs(path=path, **kw))


def search(root: Path, pattern: str, path: str | None = None) -> host.Outcome:
    return host.search_code(root, SearchCodeArgs(pattern=pattern, path=path))


def test_read_file_numbers_lines(target: Path):
    out = read(target, "app.py")
    assert out.output == "1: import os\n2: \n3: def run(cmd):\n4:     os.system(cmd)"
    assert not out.truncated


def test_read_file_line_range_keeps_original_numbers(target: Path):
    assert read(target, "app.py", start_line=3, end_line=4).output == (
        "3: def run(cmd):\n4:     os.system(cmd)"
    )
    assert read(target, "app.py", start_line=4).output == "4:     os.system(cmd)"


def test_read_file_end_past_eof_is_clamped(target: Path):
    assert read(target, "docs/readme.txt", end_line=99).output == "1: hello world"


def test_read_file_start_past_eof_is_an_error(target: Path):
    with pytest.raises(ToolError, match="past the end"):
        read(target, "app.py", start_line=50)


def test_read_file_splits_on_newline_only(target: Path):
    # str.splitlines() would also split on \x0c and  , shifting numbers vs. editors/scorer.
    (target / "odd.txt").write_bytes(b"a\r\nb\x0cstill b\n\xe2\x80\xa8same\n")
    assert read(target, "odd.txt").output == "1: a\n2: b\x0cstill b\n3:  same"


def test_read_file_empty_file(target: Path):
    (target / "empty.txt").write_text("")
    assert read(target, "empty.txt").output == ""


def test_read_file_rejects_binary(target: Path):
    with pytest.raises(ToolError, match="binary"):
        read(target, "blob.bin")


def test_read_file_flags_truncation_of_huge_files(target: Path):
    (target / "huge.txt").write_text("a\n" * (host.MAX_READ_BYTES // 2 + 10))
    out = read(target, "huge.txt")
    assert out.truncated
    assert out.output.count("\n") + 1 == host.MAX_READ_BYTES // 2


@pytest.mark.parametrize(
    "path",
    ["../outside/secret.txt", "/etc/passwd", "link_file", "link_dir/secret.txt", "docs", "nope"],
)
def test_read_file_rejects_escapes_and_non_files(target: Path, path: str):
    with pytest.raises(ToolError):
        read(target, path)


def test_read_file_follows_symlinks_that_stay_inside(target: Path):
    assert read(target, "inner_link").output.startswith("1: import os")


def test_line_count_matches_read_file_numbering(target: Path):
    assert host.line_count(target, "app.py") == 4
    (target / "no_newline.txt").write_text("a\nb")
    (target / "empty.txt").write_text("")
    assert host.line_count(target, "no_newline.txt") == 2
    assert host.line_count(target, "empty.txt") == 0


def test_search_code_finds_literal_matches(target: Path):
    assert search(target, "os.system(").output == "app.py:4:     os.system(cmd)"
    assert search(target, "hello", "docs").output == "docs/readme.txt:1: hello world"


def test_search_code_pattern_is_literal_not_regex(target: Path):
    (target / "dots.txt").write_text("a.b\nab\n")
    assert search(target, "a.b", "dots.txt").output == "dots.txt:1: a.b"
    assert search(target, ".*").output == "no matches"


def test_search_code_is_case_sensitive(target: Path):
    assert search(target, "HELLO").output == "no matches"


def test_search_code_does_not_follow_symlinks(target: Path):
    # link_file/link_dir point at "TOP SECRET" outside the target; inner_link duplicates app.py.
    assert search(target, "TOP SECRET").output == "no matches"
    assert search(target, "import os").output == "app.py:1: import os"


@pytest.mark.parametrize("path", ["../outside", "/etc", "link_dir", "link_file"])
def test_search_code_rejects_escaping_paths(target: Path, path: str):
    with pytest.raises(ToolError):
        search(target, "x", path)


def test_search_code_missing_path(target: Path):
    with pytest.raises(ToolError, match="no such path"):
        search(target, "x", "nope")


@pytest.mark.parametrize("pattern", ["", "a\nb", "a\0b", "x" * (host.MAX_PATTERN_CHARS + 1)])
def test_search_code_rejects_bad_patterns(target: Path, pattern: str):
    with pytest.raises(ToolError, match="pattern"):
        search(target, pattern)


def test_search_code_skips_binaries_and_vcs_dirs(target: Path):
    (target / ".git").mkdir()
    (target / ".git" / "config").write_text("needle\n")
    (target / "blob.bin").write_bytes(b"needle\x00")
    assert search(target, "needle").output == "no matches"


def test_search_code_caps_matches_and_flags_truncation(target: Path):
    (target / "many.txt").write_text("hit\n" * (host.MAX_MATCHES + 50))
    out = search(target, "hit", "many.txt")
    assert out.truncated
    assert len(out.output.splitlines()) == host.MAX_MATCHES


def test_search_code_output_is_in_stable_order(target: Path):
    (target / "b.txt").write_text("zz\n")
    (target / "a.txt").write_text("zz\n")
    assert search(target, "zz").output == "a.txt:1: zz\nb.txt:1: zz"


def test_search_code_clips_long_lines(target: Path):
    (target / "long.txt").write_text("needle" + "x" * 1000 + "\n")
    out = search(target, "needle", "long.txt").output
    assert len(out) == len("long.txt:1: ") + host.MAX_LINE_CHARS
