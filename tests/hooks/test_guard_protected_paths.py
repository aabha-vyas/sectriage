import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "hooks" / "guard_protected_paths.py"
PYTHON = os.environ.get("GUARD_TEST_PYTHON", sys.executable)


@pytest.fixture
def root(tmp_path):
    """A mirror of the protected layout; the hook is pointed here, never at the real repo."""
    files = {
        "evals/baselines/vuln_app.json": "{}",
        "evals/results/old-run.json": "{}",
        "evals/targets/ledger_api/app.py": "x = 1\n",
        "evals/targets.lock.json": "{}",
        "src/contracts.py": "",
        "README.md": "",
        ".env": "KEY=secret\n",
        ".env.example": "KEY=your-key-here\n",
    }
    for rel, text in files.items():
        path = tmp_path / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text)
    (tmp_path / "link").symlink_to(tmp_path / "evals" / "baselines")
    return tmp_path


def run_hook(root, tool, tool_input, stdin=None):
    payload = stdin
    if payload is None:
        body = json.dumps({"tool_name": tool, "tool_input": tool_input, "cwd": str(root)})
        payload = body.replace("{root}", str(root))
    env = {**os.environ, "CLAUDE_PROJECT_DIR": str(root)}
    return subprocess.run(
        [PYTHON, str(SCRIPT)], input=payload, capture_output=True, text=True, env=env, timeout=20
    )


def bash(command):
    return ("Bash", {"command": command})


def file_call(tool, rel):
    return (tool, {"file_path": "{root}/" + rel})


BLOCKED = [
    file_call("Edit", "evals/baselines/vuln_app.json"),
    file_call("Write", "evals/baselines/new.json"),
    file_call("Edit", "evals/results/old-run.json"),
    file_call("Write", "evals/results/old-run.json"),
    file_call("Write", "evals/targets/ledger_api/x.py"),
    file_call("Edit", "evals/targets/ledger_api/app.py"),
    file_call("Edit", "evals/targets.lock.json"),
    file_call("Write", "evals/targets.lock.json"),
    file_call("Edit", "src/contracts.py"),
    file_call("Read", ".env"),
    file_call("Write", ".env"),
    file_call("Edit", ".env.local"),
    file_call("Read", "evals/../.env"),
    file_call("Edit", "EVALS/Baselines/vuln_app.json"),
    file_call("Write", "link/new.json"),
    ("NotebookEdit", {"notebook_path": "{root}/evals/baselines/n.ipynb"}),
    bash("cat .env"),
    bash("grep KEY .env"),
    bash("cp .env /tmp/leak"),
    bash("echo 'x' | tee .env.local"),
    bash("sed -i s/a/b/ evals/baselines/vuln_app.json"),
    bash("sed -ni p evals/baselines/vuln_app.json"),
    bash("echo x > evals/targets.lock.json"),
    bash("echo x>evals/targets.lock.json"),
    bash("echo x >> evals/results/old-run.json"),
    bash("rm evals/results/old-run.json"),
    bash("rm evals/results/*.json"),
    bash("mv evals/baselines/vuln_app.json /tmp/"),
    bash("cp /tmp/x evals/baselines/vuln_app.json"),
    bash("tee evals/targets/ledger_api/app.py"),
    bash("git checkout -- evals/targets"),
    bash("ls && rm -rf evals/targets"),
    bash("echo hi\nrm evals/results/old-run.json"),
    bash("cp x.py src/contracts.py"),
]

ALLOWED = [
    file_call("Write", "evals/results/new-run.json"),
    file_call("Write", ".env.example"),
    file_call("Read", ".env.example"),
    file_call("Edit", "README.md"),
    file_call("Read", "evals/baselines/vuln_app.json"),
    file_call("Read", "evals/targets/ledger_api/app.py"),
    file_call("Write", "src/other.py"),
    bash("uv run --env-file .env python -m evals.run --summary"),
    bash("uv run python -m evals.run --summary-file s.json --baseline b.json"),
    bash("git status"),
    bash("cat .env.example"),
    bash("cat evals/baselines/vuln_app.json"),
    bash("cp evals/targets/ledger_api/app.py /tmp/copy.py"),
    bash("echo x > evals/results/new.json"),
    bash("cp s.json evals/results/"),
    bash("grep -r pattern src"),
]


@pytest.mark.parametrize(("tool", "tool_input"), BLOCKED)
def test_blocked(root, tool, tool_input):
    result = run_hook(root, tool, tool_input)
    assert result.returncode == 2, (tool, tool_input, result.stderr)
    assert "Blocked by guard_protected_paths" in result.stderr


@pytest.mark.parametrize(("tool", "tool_input"), ALLOWED)
def test_allowed(root, tool, tool_input):
    result = run_hook(root, tool, tool_input)
    assert result.returncode == 0, (tool, tool_input, result.stderr)
    assert result.stdout == ""


def test_new_file_in_results_is_allowed_but_existing_is_not(root):
    new = run_hook(root, *file_call("Write", "evals/results/brand-new.json"))
    old = run_hook(root, *file_call("Write", "evals/results/old-run.json"))
    assert (new.returncode, old.returncode) == (0, 2)


@pytest.mark.parametrize(
    "flag",
    ["--update-baseline", "--update-baseline=evals/baselines/x.json", "--update-b", "--u"],
)
def test_update_baseline_asks(root, flag):
    command = f"uv run python -m evals.run --summary-file s.json {flag}"
    result = run_hook(root, *bash(command))
    assert result.returncode == 0
    out = json.loads(result.stdout)["hookSpecificOutput"]
    assert out["hookEventName"] == "PreToolUse"
    assert out["permissionDecision"] == "ask"


def test_gating_without_update_flag_does_not_ask(root):
    command = "uv run python -m evals.run --summary-file s.json --baseline b.json --summary"
    result = run_hook(root, *bash(command))
    assert (result.returncode, result.stdout) == (0, "")


def test_unparseable_bash_is_allowed_with_a_warning(root):
    result = run_hook(root, *bash('echo "unterminated'))
    assert result.returncode == 0
    assert "warning" in result.stderr


@pytest.mark.parametrize("tool", ["Write", "Edit", "Read", "NotebookEdit"])
def test_file_tools_fail_closed_on_malformed_stdin(root, tool):
    result = run_hook(root, tool, {}, stdin="not json")
    assert result.returncode == 2


@pytest.mark.parametrize("tool", ["Write", "Edit", "Read", "NotebookEdit"])
def test_file_tools_fail_closed_without_a_path(root, tool):
    result = run_hook(root, tool, {})
    assert result.returncode == 2


def test_other_tools_are_ignored(root):
    assert run_hook(root, "Grep", {"pattern": ".env"}).returncode == 0


def test_hook_is_registered_in_project_settings():
    settings = json.loads((SCRIPT.parents[2] / ".claude" / "settings.json").read_text())
    groups = settings["hooks"]["PreToolUse"]
    assert len(groups) == 1
    matcher = set(groups[0]["matcher"].split("|"))
    assert matcher == {"Write", "Edit", "NotebookEdit", "Read", "Bash"}
    command = groups[0]["hooks"][0]["command"]
    assert '"${CLAUDE_PROJECT_DIR}/scripts/hooks/guard_protected_paths.py"' in command
