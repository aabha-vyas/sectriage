import re
from pathlib import Path

import pytest

from evals.decoys import (
    Decoy,
    DecoyKind,
    DecoyManifest,
    classify_false_positives,
    load_decoys,
    render_report,
)
from evals.scorer import GroundTruth, TruthItem, load_ground_truth
from src.contracts import Finding, Severity, VulnClass

TARGETS = ["ledger_api", "notes_portal", "static_builder"]
ROOT = Path(__file__).resolve().parents[2]


def _finding(file: str, start: int, end: int | None = None) -> Finding:
    return Finding(
        finding_id=f"f-{file}-{start}",
        vuln_class=VulnClass.SQLI,
        severity=Severity.HIGH,
        file=file,
        line_start=start,
        line_end=end or start,
        title="t",
        explanation="e",
        confidence=0.5,
    )


@pytest.mark.parametrize("target", TARGETS)
def test_manifest_snippets_sit_inside_their_ranges(target):
    manifest = load_decoys(target)
    assert manifest is not None and manifest.target == target and manifest.decoys
    for d in manifest.decoys:
        lines = (ROOT / d.file).read_text().splitlines()
        window = "\n".join(lines[d.line_start - 1 : d.line_end])
        assert d.snippet in window, d.id


@pytest.mark.parametrize("target", TARGETS)
def test_decoys_do_not_overlap_truth(target):
    manifest, truth = load_decoys(target), load_ground_truth(target)
    for d in manifest.decoys:
        for t in truth.findings:
            for s in t.spans:
                overlap = (
                    d.file == s.file and d.line_start <= s.line_end and s.line_start <= d.line_end
                )
                assert not overlap, f"{d.id} overlaps {t.id}"


@pytest.mark.parametrize("target", TARGETS)
def test_decoy_ids_unique_and_each_names_an_existing_behavioral_test(target):
    manifest = load_decoys(target)
    ids = [d.id for d in manifest.decoys]
    assert len(ids) == len(set(ids))
    source = (ROOT / f"tests/evals/test_target_{target}.py").read_text()
    defined = set(re.findall(r"^def (test_dec_\w+)", source, re.M))
    for d in manifest.decoys:
        assert d.test in defined, f"{d.id}: no test named {d.test}"


def test_missing_manifest_is_none(tmp_path):
    assert load_decoys("nothing", tmp_path) is None


def test_unknown_kind_is_rejected():
    with pytest.raises(ValueError):
        Decoy.model_validate(
            {
                "id": "x",
                "file": "a.py",
                "line_start": 1,
                "line_end": 1,
                "kind": "magic",
                "description": "d",
                "snippet": "s",
                "test": "t",
            }
        )


def _manifest() -> DecoyManifest:
    return DecoyManifest(
        target="t",
        decoys=[
            Decoy(
                id="d1",
                file="a.py",
                line_start=10,
                line_end=12,
                kind=DecoyKind.PARAMETERIZED_QUERY,
                description="d",
                snippet="s",
                test="test_dec_x",
            ),
            Decoy(
                id="d2",
                file="b.py",
                line_start=5,
                line_end=5,
                kind=DecoyKind.SHELL_FALSE,
                description="d",
                snippet="s",
                test="test_dec_y",
            ),
        ],
    )


def _truth() -> GroundTruth:
    return GroundTruth(
        target="t",
        findings=[
            TruthItem(
                id="t1",
                file="a.py",
                line_start=20,
                line_end=21,
                vuln_class=VulnClass.SQLI,
                severity=Severity.HIGH,
                description="d",
                snippet="s",
            )
        ],
    )


def test_classifier_splits_false_positives_by_where_they_landed():
    findings = [
        _finding("a.py", 20),  # true positive, not counted
        _finding("a.py", 11),  # on d1
        _finding("b.py", 5),  # on d2
        _finding("c.py", 3),  # elsewhere
        _finding("a.py", 1, 40),  # wide: never matches, never attributed
    ]
    r = classify_false_positives(findings, _truth(), _manifest())
    assert (r.fp_total, r.on_decoy, r.wide, r.elsewhere) == (4, 2, 1, 1)
    assert r.by_kind == {"parameterized_query": 1, "shell_false": 1}
    assert r.by_decoy == {"d1": 1, "d2": 1}
    assert "2 of 4" in render_report(r)


def test_classifier_with_no_findings_is_all_zero():
    r = classify_false_positives([], _truth(), _manifest())
    assert (r.fp_total, r.on_decoy, r.wide, r.elsewhere) == (0, 0, 0, 0)
    assert r.by_kind == {} and "none" in render_report(r)


def test_classifier_works_on_a_clean_target():
    truth = GroundTruth(target="t", findings=[])
    r = classify_false_positives([_finding("a.py", 10)], truth, _manifest())
    assert (r.fp_total, r.on_decoy) == (1, 1)


def test_classification_does_not_change_the_score():
    from evals.scorer import score

    findings = [_finding("a.py", 20), _finding("a.py", 11)]
    before = score(findings, _truth())
    classify_false_positives(findings, _truth(), _manifest())
    assert score(findings, _truth()) == before
