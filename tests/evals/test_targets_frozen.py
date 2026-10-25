import pytest

from evals.frozen import FROZEN, current_hashes, differences, load_lock


def test_targets_match_the_freeze_lock():
    diff = differences(load_lock(), current_hashes())
    assert not diff, (
        "the eval targets are frozen; tell the user before changing them, then run "
        "`python -m evals.frozen --update`:\n" + "\n".join(diff)
    )


@pytest.mark.parametrize("target", FROZEN)
def test_lock_covers_every_target(target):
    assert any(f"/{target}/" in p for p in load_lock())


def test_differences_reports_changed_added_and_removed_files():
    lock = {"a": "1", "b": "2", "c": "3"}
    now = {"a": "1", "b": "9", "d": "4"}
    assert differences(lock, now) == ["changed: b", "added: d", "removed: c"]
    assert differences(lock, lock) == []
