"""Freeze for the new eval targets: a content hash of every file, checked by a test.

Changing a target after agent runs have been scored on it makes the numbers incomparable, so a
change must be deliberate: edit the target, run `python -m evals.frozen --update`, and say so in
the commit. `tests/evals/test_targets_frozen.py` fails on any difference from the lock.
"""

import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TARGETS_DIR = ROOT / "targets"
LOCK = ROOT / "targets.lock.json"
FROZEN = ["ledger_api", "notes_portal", "static_builder"]


def current_hashes() -> dict[str, str]:
    """Repo-relative POSIX path -> sha256 of the bytes, for every file in the frozen targets."""
    out: dict[str, str] = {}
    for name in FROZEN:
        for path in sorted((TARGETS_DIR / name).rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts and path.suffix != ".pyc":
                rel = path.relative_to(ROOT.parent).as_posix()
                out[rel] = hashlib.sha256(path.read_bytes()).hexdigest()
    return out


def load_lock() -> dict[str, str]:
    return json.loads(LOCK.read_text())["files"]


def differences(lock: dict[str, str], now: dict[str, str]) -> list[str]:
    changed = [f"changed: {p}" for p in sorted(lock.keys() & now.keys()) if lock[p] != now[p]]
    added = [f"added: {p}" for p in sorted(now.keys() - lock.keys())]
    removed = [f"removed: {p}" for p in sorted(lock.keys() - now.keys())]
    return changed + added + removed


def main(argv: list[str]) -> int:
    if argv == ["--update"]:
        LOCK.write_text(json.dumps({"frozen": FROZEN, "files": current_hashes()}, indent=2) + "\n")
        print(f"wrote {LOCK.relative_to(ROOT.parent)}")
        return 0
    diff = differences(load_lock(), current_hashes())
    print("\n".join(diff) or "targets match the lock")
    return 1 if diff else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
