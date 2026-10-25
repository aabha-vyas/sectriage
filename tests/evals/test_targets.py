"""Ground truth for every target is checked against the target source, and the targets are
checked for hygiene (no hint words, no provider-format secrets)."""

import math
import re
from collections import Counter
from pathlib import Path

import pytest

from evals.scorer import GroundTruth, load_ground_truth

ROOT = Path(__file__).resolve().parents[2]
TARGETS_DIR = ROOT / "evals" / "targets"
ALL = ["vuln_app", "ledger_api", "notes_portal", "static_builder"]
NEW = ["ledger_api", "notes_portal", "static_builder"]
TRUTH = {t: load_ground_truth(t) for t in ALL}

# Words that would tell a reader (or the agent) which code is meant to be a finding. These
# library identifiers contain a banned word and cannot be renamed.
HINT_RE = re.compile(
    r"vuln|unsafe|insecure|bug|safe|decoy|exploit|inject|taint|malicious|attack|secret|hardcode",
    re.IGNORECASE,
)
ALLOWED_TOKENS = ("markupsafe", "|safe", "SECRET_KEY")
PROVIDER_RE = re.compile(r"sk_live_|sk_test_|\bsk-|AKIA|ghp_|gho_|xox[abprs]-?|AIza", re.IGNORECASE)


def source_files(target: str):
    return [
        p
        for p in (TARGETS_DIR / target).rglob("*")
        if p.is_file() and "__pycache__" not in p.parts and p.suffix != ".pyc"
    ]


def spans(truth: GroundTruth):
    for t in truth.findings:
        for s in t.spans:
            yield t.id, s


def entropy_bits_per_char(s: str) -> float:
    counts = Counter(s)
    return -sum(n / len(s) * math.log2(n / len(s)) for n in counts.values())


@pytest.mark.parametrize("target", ALL)
def test_snippets_sit_inside_primary_and_alternate_ranges(target):
    for item_id, s in spans(TRUTH[target]):
        path = ROOT / s.file
        assert path.is_file(), (item_id, s.file)
        lines = path.read_text().splitlines()
        assert s.line_end <= len(lines), (item_id, s.file, s.line_end)
        block = "\n".join(lines[s.line_start - 1 : s.line_end])
        snippet = s.snippet
        assert snippet in block, (item_id, s.file, s.line_start, s.line_end)


@pytest.mark.parametrize("target", ALL)
def test_ground_truth_targets_and_files_belong_together(target):
    truth = TRUTH[target]
    assert truth.target == target
    prefix = f"evals/targets/{target}/"
    for item_id, s in spans(truth):
        assert s.file.startswith(prefix), (item_id, s.file)


@pytest.mark.parametrize("target", ALL)
def test_locations_of_different_findings_never_overlap(target):
    located = [(item_id, s) for item_id, s in spans(TRUTH[target])]
    for i, (id_a, a) in enumerate(located):
        for id_b, b in located[i + 1 :]:
            if id_a == id_b or a.file != b.file:
                continue
            assert a.line_end < b.line_start or b.line_end < a.line_start, (id_a, id_b)


def test_ids_are_unique_across_all_targets():
    ids = [t.id for truth in TRUTH.values() for t in truth.findings]
    assert len(ids) == len(set(ids)) == 5 + 10


def test_new_targets_have_two_findings_per_class():
    counts = Counter(t.vuln_class.value for n in NEW for t in TRUTH[n].findings)
    assert counts == {
        "sqli": 2,
        "command_injection": 2,
        "path_traversal": 2,
        "hardcoded_secret": 2,
        "xss": 2,
    }


def test_per_target_sizes_and_clean_target_is_empty():
    assert [len(TRUTH[n].findings) for n in NEW] == [5, 5, 0]


def test_every_cross_file_finding_has_alternates_in_another_file():
    cross = {
        "ledger-sqli-order",
        "ledger-cmdi-archive",
        "ledger-path-attachment",
        "notes-xss-stored",
        "notes-path-prefix",
        "notes-cmdi-mailer",
        "notes-xss-reflected",
    }
    by_id = {t.id: t for n in NEW for t in TRUTH[n].findings}
    for item_id in cross:
        t = by_id[item_id]
        assert t.also_accept, item_id
        assert any(a.file != t.file for a in t.also_accept), item_id
    # single-location findings carry no alternates; ledger-sqli-second-order is one on purpose:
    # a report at the storage site (users.py) is a false positive, the injection is in reports.py
    for item_id in set(by_id) - cross:  # single-file findings carry no alternates
        assert by_id[item_id].also_accept == [], item_id


def test_targets_are_multi_file():
    assert len([p for p in source_files("ledger_api") if p.suffix == ".py"]) >= 8
    assert len([p for p in source_files("notes_portal") if p.suffix == ".py"]) >= 8
    assert len([p for p in source_files("static_builder") if p.suffix == ".py"]) >= 5


@pytest.mark.parametrize("target", NEW)
def test_no_hint_words_in_target_names_or_contents(target):
    for path in source_files(target):
        rel = path.relative_to(TARGETS_DIR).as_posix()
        text = path.read_text()
        for token in ALLOWED_TOKENS:
            text = text.replace(token, "")
        assert not HINT_RE.search(rel), rel
        hit = HINT_RE.search(text)
        assert hit is None, (rel, hit and hit.group(0))


@pytest.mark.parametrize("target", NEW)
def test_no_provider_format_secrets_in_targets(target):
    for path in source_files(target):
        assert not PROVIDER_RE.search(path.read_text()), path.name


def test_planted_secrets_are_high_entropy_with_neutral_prefix():
    planted = [
        t.snippet for n in NEW for t in TRUTH[n].findings if t.vuln_class == "hardcoded_secret"
    ]
    assert len(planted) == 2
    for value in planted:
        prefix, _, body = value.partition("_")
        assert re.fullmatch(r"[a-z]{2}", prefix), value
        assert len(body) >= 30 and entropy_bits_per_char(body) >= 4.0, value
        assert not PROVIDER_RE.search(value), value


def test_allowed_hint_tokens_are_the_only_exceptions():
    """If this fails, a new library identifier slipped past the hint-word scan."""
    found = set()
    for n in NEW:
        for path in source_files(n):
            text = path.read_text()
            found |= {t for t in ALLOWED_TOKENS if t in text}
    assert found == set(ALLOWED_TOKENS)


@pytest.mark.parametrize("target", NEW)
def test_every_bug_links_to_exactly_one_behavioral_test(target):
    source = (ROOT / f"tests/evals/test_target_{target}.py").read_text()
    defined = set(re.findall(r"^def (test_bug_\w+)", source, re.M))
    linked = [t.test for t in TRUTH[target].findings]
    assert all(linked), [t.id for t in TRUTH[target].findings if not t.test]
    assert len(set(linked)) == len(linked), "two bugs share a test"
    assert set(linked) == defined, "a bug test is unlinked or a link names a missing test"
