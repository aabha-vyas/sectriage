from pathlib import Path

import pytest

from src.agent.sandbox_protocol import ExecResult, UnavailableSandbox
from src.agent.tools import sandboxed
from src.agent.tools.base import Outcome, ToolError
from src.contracts import GitArgs, RunCommandArgs, SemgrepArgs
from tests.agent.fakes import FakeSandbox

GIT = ["git", "--no-pager"]


def run_git(
    root: Path, sandbox: FakeSandbox, subcommand: str, *args: str, remaining_s: int = 100
) -> Outcome:
    return sandboxed.git(
        root, sandbox, GitArgs(subcommand=subcommand, args=list(args)), remaining_s
    )


# --- run_command ---------------------------------------------------------------------------


def test_run_command_passes_argv_through_unchanged():
    sb = FakeSandbox(ExecResult(stdout="hi\n", stderr="", exit_code=0))
    out = sandboxed.run_command(sb, RunCommandArgs(argv=["echo", "hi; rm -rf /"], timeout_s=5), 100)
    assert sb.calls == [(["echo", "hi; rm -rf /"], 5)]
    assert out == Outcome(output="hi\n", exit_code=0)


@pytest.mark.parametrize(
    ("requested", "remaining", "expected"),
    [(500, 30, 30), (5, 30, 5), (5, 0, 1), (5, -3, 1)],
)
def test_run_command_clamps_timeout_to_remaining_time(
    requested: int, remaining: int, expected: int
):
    sb = FakeSandbox()
    sandboxed.run_command(sb, RunCommandArgs(argv=["true"], timeout_s=requested), remaining)
    assert sb.calls[0][1] == expected


def test_run_command_nonzero_exit_is_a_failure_that_keeps_output():
    sb = FakeSandbox(ExecResult(stdout="partial", stderr="boom", exit_code=2))
    out = sandboxed.run_command(sb, RunCommandArgs(argv=["false"], timeout_s=5), 100)
    assert not out.ok
    assert out.error == "exit code 2"
    assert out.exit_code == 2
    assert out.output == "partial\n--- stderr ---\nboom"


def test_run_command_timeout_is_a_failure():
    sb = FakeSandbox(ExecResult(stdout="", stderr="", exit_code=None, timed_out=True))
    out = sandboxed.run_command(sb, RunCommandArgs(argv=["sleep", "9"], timeout_s=5), 100)
    assert not out.ok
    assert out.error == "timed out after 5s"


def test_run_command_stderr_only_output():
    sb = FakeSandbox(ExecResult(stdout="", stderr="warn", exit_code=0))
    out = sandboxed.run_command(sb, RunCommandArgs(argv=["x"], timeout_s=5), 100)
    assert out.output == "warn"


@pytest.mark.parametrize(
    "argv",
    [[""], ["a\0b"], ["x"] * (sandboxed.MAX_ARGV_ITEMS + 1), ["x" * (sandboxed.MAX_ARG_CHARS + 1)]],
)
def test_run_command_rejects_bad_argv(argv: list[str]):
    sb = FakeSandbox()
    with pytest.raises(ToolError):
        sandboxed.run_command(sb, RunCommandArgs(argv=argv, timeout_s=5), 100)
    assert sb.calls == []


def test_unavailable_sandbox_fails_closed():
    with pytest.raises(ToolError, match="sandbox unavailable"):
        sandboxed.run_command(UnavailableSandbox(), RunCommandArgs(argv=["id"], timeout_s=5), 100)


# --- semgrep -------------------------------------------------------------------------------


def test_semgrep_builds_the_exact_argv(target: Path):
    sb = FakeSandbox()
    sandboxed.semgrep(target, sb, SemgrepArgs(config="rules.yml", path="app.py"), 100)
    assert sb.calls == [
        (
            [
                "semgrep",
                "--config",
                "rules.yml",
                "--metrics=off",
                "--disable-version-check",
                "--quiet",
                "--",
                "app.py",
            ],
            60,
        )
    ]


def test_semgrep_target_root_path(target: Path):
    sb = FakeSandbox()
    sandboxed.semgrep(target, sb, SemgrepArgs(config="rules.yml", path="."), 100)
    assert sb.calls[0][0][-1] == "."


@pytest.mark.parametrize(
    ("config", "path"),
    [
        ("p/default", "app.py"),
        ("https://semgrep.dev/p/default", "app.py"),
        ("auto", "app.py"),
        ("link_file", "app.py"),
        ("../outside/secret.txt", "app.py"),
        ("rules.yml", "../outside"),
        ("rules.yml", "/etc"),
        ("rules.yml", "link_dir"),
        ("rules.yml", "missing.py"),
    ],
)
def test_semgrep_rejects_configs_and_paths_outside_the_target(target: Path, config: str, path: str):
    sb = FakeSandbox()
    with pytest.raises(ToolError, match="inside the target"):
        sandboxed.semgrep(target, sb, SemgrepArgs(config=config, path=path), 100)
    assert sb.calls == []


@pytest.mark.parametrize(("remaining", "expected"), [(500, 60), (60, 60), (12, 12), (0, 1)])
def test_semgrep_timeout_is_capped_and_clamped(target: Path, remaining: int, expected: int):
    sb = FakeSandbox()
    sandboxed.semgrep(target, sb, SemgrepArgs(config="rules.yml", path="app.py"), remaining)
    assert sb.calls[0][1] == expected


# --- git: exact argv -----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("subcommand", "args", "expected"),
    [
        ("log", ["-n", "5"], ["log", "-n", "5"]),
        ("log", ["--oneline", "--max-count=3"], ["log", "--oneline", "--max-count=3"]),
        ("log", ["--oneline", "--", "app.py"], ["log", "--oneline", "--", "app.py"]),
        ("log", ["main..feature"], ["log", "main..feature"]),
        ("show", ["HEAD~2", "--stat"], ["show", "HEAD~2", "--stat"]),
        ("diff", ["--cached", "--stat"], ["diff", "--cached", "--stat"]),
        ("status", ["--short"], ["status", "--short"]),
        ("blame", ["-L", "1,5", "--", "app.py"], ["blame", "-L", "1,5", "--", "app.py"]),
        ("blame", ["-L", "3,+2", "--", "app.py"], ["blame", "-L", "3,+2", "--", "app.py"]),
        ("ls-files", ["-c", "-o"], ["ls-files", "-c", "-o"]),
        ("rev-parse", ["--short", "HEAD"], ["rev-parse", "--short", "HEAD"]),
        ("rev-parse", ["--short=8", "HEAD"], ["rev-parse", "--short=8", "HEAD"]),
        ("grep", ["foo", "--", "app.py"], ["grep", "foo", "--", "app.py"]),
        ("grep", ["-n", "foo", "--", "docs"], ["grep", "-n", "foo", "--", "docs"]),
        ("grep", ["-e", "../x"], ["grep", "-e", "../x"]),
        ("grep", ["../x"], ["grep", "../x"]),
        ("grep", ["-e", "-v", "HEAD"], ["grep", "-e", "-v", "HEAD"]),
        (
            "grep",
            ["-i", "-e", "a", "--", "docs/readme.txt"],
            ["grep", "-i", "-e", "a", "--", "docs/readme.txt"],
        ),
    ],
)
def test_git_builds_the_exact_argv(
    target: Path, subcommand: str, args: list[str], expected: list[str]
):
    sb = FakeSandbox()
    out = run_git(target, sb, subcommand, *args)
    assert out.ok
    assert sb.calls == [([*GIT, *expected], 60)]


def test_git_pathspec_after_double_dash_is_normalized_and_dash_safe(target: Path):
    (target / "-odd.py").write_text("x = 1\n")
    sb = FakeSandbox()
    run_git(target, sb, "log", "--", "docs/../app.py", "-odd.py")
    assert sb.calls[0][0] == [*GIT, "log", "--", "app.py", "./-odd.py"]


def test_git_value_after_separate_option_is_not_treated_as_a_positional(target: Path):
    # "../x" would be rejected as a revision; as the -e value it is a pattern.
    sb = FakeSandbox()
    run_git(target, sb, "grep", "-e", "../x", "-e", "..", "HEAD")
    assert sb.calls[0][0] == [*GIT, "grep", "-e", "../x", "-e", "..", "HEAD"]


# --- git: rejected options (nothing reaches the sandbox) -----------------------------------

# Each forbidden option, its abbreviations, and spellings git would accept for them.
REJECTED_OPTIONS = [
    ("log", ["--output=x"]),
    ("log", ["--out=x"]),
    ("log", ["--output", "x"]),
    ("log", ["--ou=x"]),
    ("diff", ["--ext-diff"]),
    ("diff", ["--ext-di"]),
    ("diff", ["--ext"]),
    ("diff", ["--textconv"]),
    ("diff", ["--text"]),
    ("diff", ["--no-index", "a", "b"]),
    ("diff", ["--no-ind", "a", "b"]),
    ("diff", ["--no-i", "a", "b"]),
    ("log", ["-c", "core.pager=sh"]),
    ("log", ["-ccore.pager=sh"]),
    ("log", ["--exec-path=/tmp"]),
    ("log", ["--exec"]),
    ("log", ["--git-dir=/tmp"]),
    ("log", ["--git-d=/tmp"]),
    ("log", ["--work-tree=/tmp"]),
    ("log", ["--work-t=/tmp"]),
    ("log", ["--upload-pack=sh"]),
    ("log", ["--upload-p=sh"]),
    ("log", ["--config=x"]),
    ("log", ["--conf=x"]),
    ("log", ["--config-env=x=y"]),
    ("log", ["--config-e=x=y"]),
    ("log", ["--namespace=x"]),
    ("log", ["--names=x"]),
    ("grep", ["--open-files-in-pager=sh", "x"]),
    ("grep", ["--open-files", "x"]),
    ("grep", ["-Osh", "x"]),
    ("grep", ["-O", "sh", "x"]),
    ("grep", ["-f", "patterns"]),
    ("ls-files", ["--exclude-from=x"]),
    ("ls-files", ["-x", "x"]),
    ("log", ["--oneline=x"]),  # a flag written with a value
    ("log", ["--onel"]),  # abbreviation of an allowed flag
    ("log", ["-"]),
    ("log", ["--output-indicator-new=x"]),
    ("status", ["-c", "x=y"]),  # -c is only allowed for ls-files
]


@pytest.mark.parametrize(("subcommand", "args"), REJECTED_OPTIONS)
def test_git_rejects_forbidden_options_and_their_abbreviations(
    target: Path, subcommand: str, args: list[str]
):
    sb = FakeSandbox()
    with pytest.raises(ToolError, match="not allowed|needs a valid value"):
        run_git(target, sb, subcommand, *args)
    assert sb.calls == []


def test_git_allowed_option_for_one_subcommand_is_rejected_for_another(target: Path):
    run_git(target, FakeSandbox(), "ls-files", "-c")
    with pytest.raises(ToolError, match="not allowed"):
        run_git(target, FakeSandbox(), "log", "-o")


def test_git_valued_option_without_equals_says_how_to_write_it(target: Path):
    sb = FakeSandbox()
    with pytest.raises(ToolError, match=r"write it as --max-count=value"):
        run_git(target, sb, "log", "--max-count", "5")
    assert sb.calls == []


@pytest.mark.parametrize(
    ("args"),
    [["-n"], ["-n", "five"], ["-n", "-5"], ["-n", "1; ls"], ["-n", ""]],
)
def test_git_separate_value_is_validated(target: Path, args: list[str]):
    sb = FakeSandbox()
    with pytest.raises(ToolError, match="needs a valid value"):
        run_git(target, sb, "log", *args)
    assert sb.calls == []


def test_git_blame_line_range_is_validated(target: Path):
    with pytest.raises(ToolError, match="needs a valid value"):
        run_git(target, FakeSandbox(), "blame", "-L", "/etc/passwd/", "--", "app.py")


@pytest.mark.parametrize(
    "subcommand", ["push", "commit", "config", "clone", "fetch", "checkout", "-c", "", "LOG"]
)
def test_git_subcommand_allowlist(target: Path, subcommand: str):
    sb = FakeSandbox()
    with pytest.raises(ToolError, match="subcommand not allowed"):
        run_git(target, sb, subcommand)
    assert sb.calls == []


@pytest.mark.parametrize(
    "arg", ["/etc/passwd", "~/x", "../x", "a/../../x", "..", "HEAD:../../etc/passwd", "a\\b", ""]
)
def test_git_rejects_escaping_revisions_and_positionals(target: Path, arg: str):
    sb = FakeSandbox()
    with pytest.raises(ToolError, match="revisions"):
        run_git(target, sb, "show", arg)
    assert sb.calls == []


@pytest.mark.parametrize(
    "pathspec",
    [
        "../outside",
        "../outside/secret.txt",
        "/etc/passwd",
        "link_file",
        "link_dir",
        "missing.py",
        ":(top)x",
    ],
)
def test_git_pathspec_outside_the_target_is_rejected(target: Path, pathspec: str):
    sb = FakeSandbox()
    with pytest.raises(ToolError, match="pathspec must be an existing path inside the target"):
        run_git(target, sb, "log", "--", "app.py", pathspec)
    assert sb.calls == []


def test_git_checks_argument_limits(target: Path):
    with pytest.raises(ToolError):
        run_git(target, FakeSandbox(), "log", "--grep=a\0b")
    with pytest.raises(ToolError):
        run_git(target, FakeSandbox(), "grep", "x" * (sandboxed.MAX_ARG_CHARS + 1))


# --- git: exit codes and timeouts ----------------------------------------------------------


def test_git_grep_exit_1_means_no_matches_and_is_ok(target: Path):
    sb = FakeSandbox(ExecResult(stdout="", stderr="", exit_code=1))
    out = run_git(target, sb, "grep", "needle")
    assert out.ok
    assert out.error is None
    assert out.output == ""
    assert out.exit_code == 1


def test_git_grep_other_failures_are_still_failures(target: Path):
    sb = FakeSandbox(ExecResult(stdout="", stderr="fatal: bad", exit_code=128))
    out = run_git(target, sb, "grep", "needle")
    assert not out.ok
    assert out.error == "exit code 128"


def test_git_grep_timeout_is_a_failure(target: Path):
    sb = FakeSandbox(ExecResult(stdout="", stderr="", exit_code=None, timed_out=True))
    assert not run_git(target, sb, "grep", "needle").ok


@pytest.mark.parametrize("subcommand", ["log", "show", "status", "blame", "ls-files", "rev-parse"])
def test_git_exit_1_is_a_failure_outside_grep(target: Path, subcommand: str):
    sb = FakeSandbox(ExecResult(stdout="", stderr="", exit_code=1))
    out = run_git(target, sb, subcommand)
    assert not out.ok
    assert out.error == "exit code 1"


@pytest.mark.parametrize("flag", ["--exit-code", "--quiet"])
def test_git_diff_exit_1_is_ok_with_exit_code_or_quiet(target: Path, flag: str):
    sb = FakeSandbox(ExecResult(stdout="", stderr="", exit_code=1))
    out = run_git(target, sb, "diff", flag)
    assert out.ok
    assert out.exit_code == 1


def test_git_diff_exit_1_is_a_failure_without_exit_code_flag(target: Path):
    sb = FakeSandbox(ExecResult(stdout="", stderr="", exit_code=1))
    assert not run_git(target, sb, "diff", "--stat").ok


def test_git_diff_exit_code_flag_still_fails_on_real_errors(target: Path):
    sb = FakeSandbox(ExecResult(stdout="", stderr="fatal", exit_code=128))
    assert not run_git(target, sb, "diff", "--exit-code").ok


def test_git_pathspec_named_like_a_flag_does_not_enable_exit_1(target: Path):
    (target / "--quiet").write_text("x\n")
    sb = FakeSandbox(ExecResult(stdout="", stderr="", exit_code=1))
    assert not run_git(target, sb, "diff", "--", "--quiet").ok
    assert sb.calls[0][0][-1] == "./--quiet"


@pytest.mark.parametrize(("remaining", "expected"), [(500, 60), (60, 60), (12, 12), (0, 1)])
def test_git_timeout_is_capped_and_clamped(target: Path, remaining: int, expected: int):
    sb = FakeSandbox()
    run_git(target, sb, "status", remaining_s=remaining)
    assert sb.calls[0][1] == expected


def test_git_unavailable_sandbox_fails_closed(target: Path):
    with pytest.raises(ToolError, match="sandbox unavailable"):
        sandboxed.git(target, UnavailableSandbox(), GitArgs(subcommand="status"), 100)
