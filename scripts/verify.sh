#!/usr/bin/env bash
# Static environment checks for SecTriage.
# Prints one line per check (PASS / WARN / FAIL) plus a hint, then a summary.
# Exit code is 1 if any required check fails, otherwise 0.
# Usage: scripts/verify.sh [--quiet]   (--quiet hides PASS lines)

set -u

QUIET=0
[ "${1:-}" = "--quiet" ] && QUIET=1

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT" || exit 1

PASS_COUNT=0
WARN_COUNT=0
FAIL_COUNT=0

pass() { PASS_COUNT=$((PASS_COUNT + 1)); [ "$QUIET" -eq 1 ] || printf 'PASS  %s\n' "$1"; }
warn() { WARN_COUNT=$((WARN_COUNT + 1)); printf 'WARN  %s\n      hint: %s\n' "$1" "$2"; }
fail() { FAIL_COUNT=$((FAIL_COUNT + 1)); printf 'FAIL  %s\n      hint: %s\n' "$1" "$2"; }

have() { command -v "$1" >/dev/null 2>&1; }

# Prefer the project virtualenv so we check what the code will actually import.
if [ -x ".venv/bin/python" ]; then
  PY=".venv/bin/python"
else
  PY="python3"
fi

# --- Git ---------------------------------------------------------------
if git rev-parse --is-inside-work-tree >/dev/null 2>&1; then
  pass "inside a git repository"
else
  fail "not a git repository" "run: git init && git add -A && git commit -m 'initial commit'"
fi

# --- Toolchain ---------------------------------------------------------
if have "$PY"; then
  if "$PY" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)'; then
    pass "python >= 3.12 ($("$PY" --version 2>&1))"
  else
    fail "python is older than 3.12 ($("$PY" --version 2>&1))" "install 3.12+: uv python install 3.12"
  fi
else
  fail "python not found" "install uv, then: uv python install 3.12"
fi

if have uv; then pass "uv installed"; else fail "uv missing" "install: curl -LsSf https://astral.sh/uv/install.sh | sh"; fi
if have jq; then pass "jq installed (needed by hook scripts)"; else fail "jq missing" "install: apt-get install jq  (or brew install jq)"; fi

if have docker; then
  if docker info >/dev/null 2>&1; then
    pass "docker daemon reachable"
  else
    fail "docker CLI found but daemon not reachable" "start Docker Desktop or the docker service (sandbox needs it)"
  fi
else
  fail "docker missing" "install Docker; the per-task sandbox depends on it"
fi

if have ruff || [ -x ".venv/bin/ruff" ]; then pass "ruff available"; else fail "ruff missing" "run: uv sync (ruff is a dev dependency)"; fi
if have shellcheck; then
  if shellcheck scripts/*.sh >/dev/null 2>&1; then pass "shellcheck clean on scripts/"; else fail "shellcheck found issues" "run: shellcheck scripts/*.sh"; fi
else
  warn "shellcheck not installed" "optional; install it to lint scripts/"
fi

# --- Python packages (import check, no network) ---------------------------
# Format: import_name:install_name
REQUIRED_PKGS="anthropic:anthropic pydantic:pydantic pytest:pytest opentelemetry.sdk:opentelemetry-sdk docker:docker"
OPTIONAL_PKGS="semgrep:semgrep"

check_pkg() {
  # $1 = import name; exits 0 if importable spec exists
  "$PY" - "$1" <<'EOF'
import importlib.util, sys
try:
    sys.exit(0 if importlib.util.find_spec(sys.argv[1]) else 1)
except (ImportError, ValueError):
    sys.exit(1)
EOF
}

if have "$PY"; then
  for entry in $REQUIRED_PKGS; do
    mod="${entry%%:*}"; dist="${entry##*:}"
    if check_pkg "$mod"; then pass "python package: $dist"; else fail "python package missing: $dist" "run: uv add $dist  (or uv sync)"; fi
  done
  for entry in $OPTIONAL_PKGS; do
    mod="${entry%%:*}"; dist="${entry##*:}"
    if check_pkg "$mod" || have "$mod"; then pass "optional tool: $dist"; else warn "optional tool missing: $dist" "run: uv add --dev $dist  (used by the semgrep tool)"; fi
  done
fi

# --- Secrets -----------------------------------------------------------
# Only reports whether a key is set; the value is never printed.
if [ -n "${ANTHROPIC_API_KEY:-}" ]; then
  pass "ANTHROPIC_API_KEY is set"
elif [ -f .env ] && grep -qE '^ANTHROPIC_API_KEY=.+' .env; then
  pass "ANTHROPIC_API_KEY is set in .env"
else
  fail "ANTHROPIC_API_KEY not set" "export it, or put ANTHROPIC_API_KEY=... in .env (copy .env.example); live commands: uv run --env-file .env ..."
fi

if [ -f .gitignore ] && grep -qE '^\.env$' .gitignore; then
  pass ".env is gitignored"
else
  fail ".env is not gitignored" "add a line '.env' to .gitignore"
fi

if git ls-files --error-unmatch .env >/dev/null 2>&1; then
  fail ".env is tracked by git" "run: git rm --cached .env"
fi

# --- Claude Code config --------------------------------------------------
if [ -f .claude/settings.json ]; then
  if have jq && jq empty .claude/settings.json >/dev/null 2>&1; then
    pass ".claude/settings.json is valid JSON"
  else
    fail ".claude/settings.json is invalid JSON (or jq missing)" "run: jq . .claude/settings.json"
  fi
else
  fail ".claude/settings.json missing" "restore it from the scaffold"
fi

if [ -f CLAUDE.md ]; then
  lines=$(wc -l < CLAUDE.md | tr -d ' ')
  if [ "$lines" -le 80 ]; then pass "CLAUDE.md is lean ($lines lines)"; else warn "CLAUDE.md is $lines lines" "keep it under ~80; move detail to docs/ or per-folder CLAUDE.md"; fi
else
  fail "CLAUDE.md missing" "create a short root CLAUDE.md"
fi

for f in .claude/agents/code-reviewer.md .claude/skills/verify/SKILL.md; do
  if [ -f "$f" ] && head -1 "$f" | grep -q '^---$'; then pass "$f has frontmatter"; else fail "$f missing or has no frontmatter" "restore it from the scaffold"; fi
done

# --- Project layout ------------------------------------------------------
for p in README.md pyproject.toml; do
  if [ -f "$p" ]; then pass "file exists: $p"; else fail "file missing: $p" "create it"; fi
done
for d in src evals tests; do
  if [ -d "$d" ]; then pass "directory exists: $d/"; else warn "directory missing: $d/" "create it when you start that lane"; fi
done
if [ -f src/contracts.py ]; then pass "src/contracts.py exists"; else warn "src/contracts.py not written yet" "write the event/task/tool schemas first, on main, before opening lanes"; fi

# --- Summary -------------------------------------------------------------
printf '\nSummary: %d passed, %d warnings, %d failed\n' "$PASS_COUNT" "$WARN_COUNT" "$FAIL_COUNT"
[ "$FAIL_COUNT" -eq 0 ]
