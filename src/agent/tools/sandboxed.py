"""Tools that execute inside the sandbox.

The host only validates model input and builds an argv; `Sandbox.exec` is the only thing that runs
it. If a check here is wrong the worst case is a bad argv inside a no-network, read-only container.

NOTE: the semgrep and git argv shapes below are unverified until the sandbox lane runs them for
real (flag spellings, `--` handling, exit codes). Expect to adjust them once that lane lands.
"""

import re
from dataclasses import dataclass, field
from pathlib import Path

from src.agent.paths import PathError, sandbox_path
from src.agent.sandbox_protocol import ExecResult, Sandbox, SandboxUnavailableError
from src.agent.tools.base import Outcome, ToolError
from src.contracts import GitArgs, RunCommandArgs, SemgrepArgs

MAX_ARGV_ITEMS = 64
MAX_ARG_CHARS = 4096
# Per-call wall-clock cap for semgrep and git, applied on top of the remaining task time.
TOOL_TIMEOUT_CAP_S = 60


@dataclass(frozen=True)
class GitOptions:
    """Exact option spellings allowed for one git subcommand.

    git accepts unambiguous abbreviations of long options (`--out` for `--output`), so a denylist
    can't work: anything starting with "-" that is not listed here is rejected.
    """

    flags: frozenset[str] = frozenset()  # exact, takes no value
    valued: frozenset[str] = frozenset()  # exact name, must be written `--name=value`
    separate: dict[str, re.Pattern[str]] = field(default_factory=dict)  # `-n 5`: value is next arg


def _fs(*names: str) -> frozenset[str]:
    return frozenset(names)


_COUNT = re.compile(r"\d{1,6}")
_LINE_RANGE = re.compile(r"\d{1,6}(,\+?\d{1,6})?")
_ANY = re.compile(r".+", re.DOTALL)

_PATCH_FLAGS = _fs("-p", "--patch", "--no-patch", "--stat", "--name-only", "--name-status")

GIT_OPTIONS: dict[str, GitOptions] = {
    "log": GitOptions(
        flags=_PATCH_FLAGS
        | _fs(
            "--oneline",
            "--shortstat",
            "--graph",
            "--decorate",
            "--no-decorate",
            "--all",
            "--reverse",
            "--merges",
            "--no-merges",
            "--first-parent",
            "--follow",
            "--abbrev-commit",
            "--no-color",
        ),
        valued=_fs(
            "--max-count",
            "--since",
            "--until",
            "--author",
            "--grep",
            "--pretty",
            "--format",
            "--abbrev",
            "--diff-filter",
        ),
        separate={"-n": _COUNT},
    ),
    "show": GitOptions(
        flags=_PATCH_FLAGS | _fs("--oneline", "--abbrev-commit", "--no-color"),
        valued=_fs("--pretty", "--format", "--abbrev", "--diff-filter"),
    ),
    "diff": GitOptions(
        flags=_PATCH_FLAGS
        | _fs(
            "--shortstat",
            "--numstat",
            "--summary",
            "--cached",
            "--staged",
            "-w",
            "--ignore-all-space",
            "--exit-code",
            "--quiet",
            "--no-color",
        ),
        valued=_fs("--unified", "--diff-filter"),
    ),
    "status": GitOptions(
        flags=_fs("-s", "--short", "-b", "--branch", "--porcelain", "--ignored"),
        valued=_fs("--untracked-files"),
    ),
    "blame": GitOptions(
        flags=_fs("-l", "-e", "--show-email", "-w", "-s", "--porcelain", "--line-porcelain"),
        separate={"-L": _LINE_RANGE},
    ),
    "ls-files": GitOptions(
        flags=_fs(
            "-c",
            "--cached",
            "-o",
            "--others",
            "-i",
            "--ignored",
            "-m",
            "--modified",
            "-d",
            "--deleted",
            "-s",
            "--stage",
            "--full-name",
            "--exclude-standard",
        ),
    ),
    "grep": GitOptions(
        flags=_fs(
            "-n",
            "--line-number",
            "-i",
            "--ignore-case",
            "-w",
            "--word-regexp",
            "-F",
            "--fixed-strings",
            "-E",
            "--extended-regexp",
            "-l",
            "--files-with-matches",
            "-c",
            "--count",
            "-I",
            "-H",
            "--no-color",
        ),
        separate={"-e": _ANY},
    ),
    "rev-parse": GitOptions(
        flags=_fs(
            "--short", "--abbrev-ref", "--show-toplevel", "--is-inside-work-tree", "--verify"
        ),
        valued=_fs("--short"),
    ),
}
GIT_SUBCOMMANDS = frozenset(GIT_OPTIONS)


def _check_argv(argv: list[str]) -> None:
    if len(argv) > MAX_ARGV_ITEMS:
        raise ToolError(f"too many arguments (max {MAX_ARGV_ITEMS})")
    for item in argv:
        if len(item) > MAX_ARG_CHARS:
            raise ToolError(f"argument too long (max {MAX_ARG_CHARS} characters)")
        if "\0" in item:
            raise ToolError("arguments must not contain NUL bytes")


def _clamp_timeout(requested: int, remaining_s: int) -> int:
    return max(1, min(requested, remaining_s))


def _run(
    sandbox: Sandbox,
    argv: list[str],
    timeout_s: int,
    ok_exit_codes: frozenset[int] = frozenset({0}),
) -> Outcome:
    try:
        result: ExecResult = sandbox.exec(argv, timeout_s=timeout_s)
    except SandboxUnavailableError as e:
        raise ToolError(f"sandbox unavailable: {e}") from None
    output = result.stdout
    if result.stderr:
        output = f"{output}\n--- stderr ---\n{result.stderr}" if output else result.stderr
    if result.timed_out:
        return Outcome(
            output=output,
            ok=False,
            error=f"timed out after {timeout_s}s",
            exit_code=result.exit_code,
        )
    if result.exit_code not in ok_exit_codes:
        return Outcome(
            output=output,
            ok=False,
            error=f"exit code {result.exit_code}",
            exit_code=result.exit_code,
        )
    return Outcome(output=output, exit_code=result.exit_code)


def run_command(sandbox: Sandbox, args: RunCommandArgs, remaining_s: int) -> Outcome:
    _check_argv(args.argv)
    if not args.argv[0]:
        raise ToolError("argv[0] must not be empty")
    return _run(sandbox, list(args.argv), _clamp_timeout(args.timeout_s, remaining_s))


def semgrep(root: Path, sandbox: Sandbox, args: SemgrepArgs, remaining_s: int) -> Outcome:
    try:
        config = sandbox_path(root, args.config)
        path = sandbox_path(root, args.path)
    except PathError as e:
        raise ToolError(f"config and path must be existing paths inside the target: {e}") from None
    argv = [
        "semgrep",
        "--config",
        config,
        "--metrics=off",
        "--disable-version-check",
        "--quiet",
        "--",
        path,
    ]
    return _run(sandbox, argv, _clamp_timeout(TOOL_TIMEOUT_CAP_S, remaining_s))


def _check_revision(arg: str) -> None:
    """A positional before `--` is a revision (or, for git, possibly a path): keep it in-tree."""
    if not arg or arg.startswith(("/", "~")) or "\\" in arg or ".." in arg.split("/"):
        raise ToolError("revisions must not be empty, absolute, or contain '..' path segments")


def _git_argv(root: Path, subcommand: str, args: list[str]) -> list[str]:
    spec = GIT_OPTIONS[subcommand]
    out: list[str] = []
    pattern_pending = subcommand == "grep" and "-e" not in args  # first positional is the pattern
    i = 0
    while i < len(args):
        arg = args[i]
        if arg == "--":
            out.append("--")
            for pathspec in args[i + 1 :]:
                try:
                    out.append(sandbox_path(root, pathspec))
                except PathError as e:
                    raise ToolError(
                        f"pathspec must be an existing path inside the target: {e}"
                    ) from None
            return out
        if arg.startswith("-"):
            name, eq, _ = arg.partition("=")
            if not eq and arg in spec.flags:
                out.append(arg)
            elif eq and name in spec.valued:
                out.append(arg)
            elif not eq and arg in spec.valued:
                raise ToolError(f"git {subcommand}: write it as {arg}=value")
            elif not eq and arg in spec.separate:
                value = args[i + 1] if i + 1 < len(args) else ""
                if not spec.separate[arg].fullmatch(value):
                    raise ToolError(f"git {subcommand}: {arg} needs a valid value after it")
                out += [arg, value]
                i += 2
                continue
            else:
                raise ToolError(f"git {subcommand}: option not allowed: {name[:40]}")
        elif pattern_pending:
            pattern_pending = False
            out.append(arg)
        else:
            _check_revision(arg)
            out.append(arg)
        i += 1
    return out


def git(root: Path, sandbox: Sandbox, args: GitArgs, remaining_s: int) -> Outcome:
    if args.subcommand not in GIT_SUBCOMMANDS:
        allowed = ", ".join(sorted(GIT_SUBCOMMANDS))
        raise ToolError(f"git subcommand not allowed; use one of: {allowed}")
    _check_argv(args.args)
    git_args = _git_argv(root, args.subcommand, args.args)
    argv = ["git", "--no-pager", args.subcommand, *git_args]
    # Exit 1 is an answer, not a failure: `git grep` found nothing, or `git diff --exit-code`
    # (implied by --quiet) found differences. Pathspecs are "./"-prefixed or plain paths that
    # exist in the target, so they can't be mistaken for these flags.
    ok_codes = frozenset({0})
    if args.subcommand == "grep" or (
        args.subcommand == "diff" and {"--exit-code", "--quiet"} & set(git_args)
    ):
        ok_codes = frozenset({0, 1})
    return _run(sandbox, argv, _clamp_timeout(TOOL_TIMEOUT_CAP_S, remaining_s), ok_codes)
