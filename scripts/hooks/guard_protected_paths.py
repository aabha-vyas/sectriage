#!/usr/bin/env python3
"""PreToolUse guard: keep agents off frozen eval files and secrets.

Protected: evals/baselines/, evals/targets/, evals/targets.lock.json, src/contracts.py, every
existing file in evals/results/ (new files there are allowed), and .env / .env.* (but not
.env.example).

Exit 2 blocks the call; stderr is the reason Claude sees. File-tool checks fail closed. The Bash
branch is best effort, not a sandbox: it does not see writes made by a Python process.
Stdlib only and Python 3.9 compatible, because `python3` on PATH varies between machines.
"""

from __future__ import annotations

import glob
import json
import os
import re
import shlex
import sys

BLOCK_DIRS = ("evals/baselines", "evals/targets")
BLOCK_FILES = ("evals/targets.lock.json", "src/contracts.py")
RESULTS_DIR = "evals/results"

FILE_TOOLS = ("Write", "Edit", "NotebookEdit", "Read")
DESTROY_VERBS = ("rm", "mv", "truncate", "shred", "unlink", "rmdir")
COPY_VERBS = ("cp", "install", "rsync")
GIT_WRITE_SUBCOMMANDS = ("checkout", "restore", "rm", "mv")

EXIT_BLOCK = 2


class Block(Exception):
    pass


def _fold(text):
    # macOS filesystems are case-insensitive by default, so compare folded.
    return text.lower()


def _root(data):
    root = os.environ.get("CLAUDE_PROJECT_DIR") or data.get("cwd") or os.getcwd()
    return os.path.realpath(root)


def _abs(path, root):
    return os.path.realpath(path if os.path.isabs(path) else os.path.join(root, path))


def _rel(abs_path, root):
    """Path relative to the project root, folded, or None when outside it."""
    if _fold(abs_path) == _fold(root):
        return ""
    prefix = _fold(root).rstrip(os.sep) + os.sep
    folded = _fold(abs_path)
    if not folded.startswith(prefix):
        return None
    return folded[len(prefix) :].replace(os.sep, "/")


def _is_env(path):
    name = _fold(os.path.basename(path.rstrip("/\\")))
    return name == ".env" or (name.startswith(".env.") and name != ".env.example")


def _in_dir(rel, directory):
    return rel == directory or rel.startswith(directory + "/")


def _protected_reason(rel):
    if rel is None:
        return None
    if any(_in_dir(rel, d) for d in BLOCK_DIRS) or rel in BLOCK_FILES:
        return f"{rel} is frozen or protected"
    return None


def _has_glob(text):
    return any(c in text for c in "*?[")


def _results_exist(abs_path):
    if _has_glob(abs_path):
        return bool(glob.glob(abs_path))
    return os.path.exists(abs_path)


def _file_tool_reason(tool, path, root):
    """Why a file-tool call must be blocked, or None."""
    if _is_env(path):
        return f"{os.path.basename(path)} may hold secrets; use .env.example"
    if tool == "Read":
        return None
    abs_path = _abs(path, root)
    rel = _rel(abs_path, root)
    reason = _protected_reason(rel)
    if reason:
        return reason
    if rel is not None and _in_dir(rel, RESULTS_DIR):
        if tool in ("Edit", "NotebookEdit"):
            return f"{rel} is an existing result file; results are write-once"
        if os.path.isfile(abs_path):
            return f"{rel} already exists; results are write-once (new files are allowed)"
    return None


def _tokenize(command):
    lexer = shlex.shlex(command.replace("\n", " ; "), posix=True, punctuation_chars=True)
    lexer.whitespace_split = True
    lexer.commenters = ""
    return list(lexer)


def _is_operator(token):
    return bool(token) and all(c in "();|&" for c in token)


def _is_redirect(token):
    return bool(token) and ">" in token and all(c in "();|&<>" for c in token)


def _segments(tokens):
    """Split tokens into (words, redirect targets), one pair per simple command."""
    segments = []
    words, targets = [], []
    skip = False
    for index, token in enumerate(tokens):
        if skip:
            skip = False
            continue
        if _is_redirect(token):
            if index + 1 < len(tokens):
                targets.append(tokens[index + 1])
                skip = True
        elif _is_operator(token):
            segments.append((words, targets))
            words, targets = [], []
        else:
            words.append(token)
    segments.append((words, targets))
    return segments


def _positional(words):
    return [w for w in words if not w.startswith("-")]


def _check_destroys(path, root):
    """rm/mv/sed -i style: the path is changed or removed."""
    abs_path = _abs(path, root)
    rel = _rel(abs_path, root)
    reason = _protected_reason(rel)
    if reason:
        return reason
    if rel is not None and _in_dir(rel, RESULTS_DIR) and _results_exist(abs_path):
        return f"{rel} is existing result data; results are write-once"
    return None


def _check_writes(path, root):
    """cp dest / redirect / tee: the path is created or overwritten."""
    abs_path = _abs(path, root)
    rel = _rel(abs_path, root)
    reason = _protected_reason(rel)
    if reason:
        return reason
    if rel is not None and _in_dir(rel, RESULTS_DIR) and os.path.isfile(abs_path):
        return f"{rel} already exists; results are write-once (new files are allowed)"
    return None


def _sed_in_place(args):
    for arg in args:
        if arg == "--in-place" or arg.startswith("--in-place="):
            return True
        if not arg.startswith("--") and re.match(r"-[a-zA-Z]*i", arg):
            return True
    return False


def _segment_reasons(words, targets, root):
    reasons = [_check_writes(t, root) for t in targets]
    for i, word in enumerate(words):
        verb = os.path.basename(word)
        rest = words[i + 1 :]
        paths = _positional(rest)
        if verb in DESTROY_VERBS:
            reasons += [_check_destroys(p, root) for p in paths]
        elif verb == "tee":
            reasons += [_check_writes(p, root) for p in paths]
        elif verb in COPY_VERBS:
            into_dir = any(w in ("-t", "--target-directory") or w.startswith("-t") for w in rest)
            dests = paths if into_dir else paths[-1:]
            reasons += [_check_writes(p, root) for p in dests]
        elif verb == "sed" and _sed_in_place(rest):
            reasons += [_check_destroys(p, root) for p in paths]
        elif verb == "git" and any(w in GIT_WRITE_SUBCOMMANDS for w in paths[:1]):
            reasons += [_check_destroys(p, root) for p in paths[1:]]
    return reasons


def _is_update_baseline(token):
    # argparse accepts any unambiguous prefix (--update-b) and --flag=value; evals/run.py has no
    # other flag starting with --u, so every prefix from --u up is this flag.
    name = token.split("=", 1)[0]
    return len(name) >= 3 and "--update-baseline".startswith(name)


def _bash_decision(command, root):
    """Return ('block', reason), ('ask', reason) or None. Raises ValueError on a bad parse."""
    tokens = _tokenize(command)
    for index, token in enumerate(tokens):
        previous = tokens[index - 1] if index else ""
        if _is_env(token) and previous != "--env-file":
            return "block", f"{os.path.basename(token)} may hold secrets; use .env.example"
    for words, targets in _segments(tokens):
        reasons = [r for r in _segment_reasons(words, targets, root) if r]
        if reasons:
            return "block", reasons[0]
    if "evals.run" in tokens and any(_is_update_baseline(t) for t in tokens):
        return "ask", (
            "--update-baseline rewrites evals/baselines/ from inside Python; "
            "confirm the promotion with the user"
        )
    return None


def _decide(data):
    tool = data.get("tool_name")
    tool_input = data.get("tool_input") or {}
    root = _root(data)
    if tool in FILE_TOOLS:
        path = tool_input.get("file_path") or tool_input.get("notebook_path")
        if not path:
            raise Block(f"no file path in {tool} input; refusing to guess")
        reason = _file_tool_reason(tool, path, root)
        if reason:
            raise Block(reason)
    elif tool == "Bash":
        command = tool_input.get("command") or ""
        try:
            decision = _bash_decision(command, root)
        except Exception as exc:  # best effort: a parse failure must not wedge every command
            sys.stderr.write(
                f"guard_protected_paths: warning: could not check the Bash command ({exc}); "
                "protected paths were not checked\n"
            )
            return
        if decision and decision[0] == "block":
            raise Block(decision[1])
        if decision:
            print(
                json.dumps(
                    {
                        "hookSpecificOutput": {
                            "hookEventName": "PreToolUse",
                            "permissionDecision": "ask",
                            "permissionDecisionReason": decision[1],
                        }
                    }
                )
            )


def main():
    try:
        data = json.load(sys.stdin)
        _decide(data)
    except Block as exc:
        sys.stderr.write(f"Blocked by guard_protected_paths: {exc}\n")
        return EXIT_BLOCK
    except Exception as exc:  # fail closed: a guard that silently fails open is worse
        sys.stderr.write(f"Blocked by guard_protected_paths: hook error: {exc!r}\n")
        return EXIT_BLOCK
    return 0


if __name__ == "__main__":
    sys.exit(main())
