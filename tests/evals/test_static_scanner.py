import json
from collections import Counter

import pytest

from evals.results import RESULTS_DIR, load_summary
from evals.scorer import score
from evals.static_scanner import (
    PACKS,
    ROW_TARGET,
    TARGETS,
    build_row,
    combined_truth,
    convert,
    decoy_report,
    stem,
    vuln_class_for,
)
from src.contracts import Finding, VulnClass

VERSION = "1.182.1"
BASE = RESULTS_DIR / stem(VERSION)


def _result(rule, path="evals/targets/x/a.py", start=3, end=3, severity="ERROR"):
    return {
        "check_id": f"python.lang.security.{rule}",
        "path": path,
        "start": {"line": start},
        "end": {"line": end},
        "extra": {"severity": severity, "message": "m", "metadata": {"confidence": "HIGH"}},
    }


@pytest.mark.parametrize(
    "rule,expected",
    [
        ("subprocess-shell-true", VulnClass.COMMAND_INJECTION),
        ("template-unescaped-with-safe", VulnClass.XSS),
        ("raw-html-format", VulnClass.XSS),
        ("directly-returned-format-string", VulnClass.XSS),
        ("sqlalchemy-execute-raw-query", VulnClass.SQLI),
        ("path-traversal-open", VulnClass.PATH_TRAVERSAL),
        ("hardcoded-password-default-argument", VulnClass.HARDCODED_SECRET),
        ("avoid-pickle", None),
        ("debug-enabled", None),
    ],
)
def test_rule_to_class_mapping(rule, expected):
    assert vuln_class_for(f"python.lang.security.{rule}") == expected


def test_convert_drops_out_of_scope_results_and_keeps_scored_ones():
    data = {"results": [_result("subprocess-shell-true"), _result("debug-enabled")]}
    findings, dropped = convert(data)
    assert dropped == 1 and len(findings) == 1
    f = findings[0]
    assert f.vuln_class == VulnClass.COMMAND_INJECTION and f.severity.value == "high"
    assert (f.file, f.line_start, f.line_end) == ("evals/targets/x/a.py", 3, 3)


def test_build_row_has_no_model_cost_and_scores_against_the_combined_truth():
    row = build_row([], version=VERSION, wall_s=1.5)
    run = row.runs[0]
    assert row.target == ROW_TARGET and row.n_truth == 10 and row.sandbox == "none"
    assert row.model == "static-scanner:semgrep-1.182.1"
    assert (run.tp, run.fp, run.fn) == (0, 0, 10)
    assert run.cost_usd == 0.0 and run.tokens.input is None and run.tokens.output is None


def test_combined_truth_is_the_three_targets():
    truth = combined_truth(TARGETS)
    assert len(truth.findings) == 10
    assert len({t.id for t in truth.findings}) == 10


# --- the committed row ---------------------------------------------------------------------


def _committed_findings() -> list[Finding]:
    raw = json.loads((BASE.parent / f"{BASE.name}.findings.json").read_text())
    return [Finding.model_validate(f) for f in raw]


def test_committed_row_is_3_of_10_found_with_one_false_positive():
    row = load_summary(BASE.parent / f"{BASE.name}.json")
    run = row.runs[0]
    assert (run.tp, run.fp, run.fn) == (3, 1, 7)
    assert row.model == "static-scanner:semgrep-1.182.1"
    assert row.prompt_version == "packs:" + "+".join(PACKS)


def test_committed_row_rescores_from_the_committed_findings():
    row = load_summary(BASE.parent / f"{BASE.name}.json")
    s = score(_committed_findings(), combined_truth(TARGETS))
    run = row.runs[0]
    assert (s.tp, s.fp, s.fn) == (run.tp, run.fp, run.fn)
    assert s.recall_by_class == run.recall_by_class


def test_committed_false_positive_is_the_escaped_greeting_decoy():
    report = json.loads((BASE.parent / f"{BASE.name}.decoys.json").read_text())["decoys"]
    assert report == decoy_report(_committed_findings()).model_dump()
    assert report["fp_total"] == 1 and report["by_decoy"] == {"notes-dec-greeting": 1}


def _class_counts(findings, truth):
    """(hits, truth) per class, recovered from the recall ratios the Score reports."""
    n = Counter(t.vuln_class.value for t in truth.findings)
    s = score(findings, truth)
    return {c: (round(s.recall_by_class[c] * k), k) for c, k in n.items()}, s


def test_per_class_counts_sum_to_the_totals_on_the_committed_findings():
    truth = combined_truth(TARGETS)
    counts, s = _class_counts(_committed_findings(), truth)
    assert sum(h for h, _ in counts.values()) == s.tp == 3
    assert sum(k for _, k in counts.values()) == len(truth.findings) == s.tp + s.fn == 10
    assert counts == {
        "command_injection": (2, 2),
        "xss": (1, 2),
        "sqli": (0, 2),
        "path_traversal": (0, 2),
        "hardcoded_secret": (0, 2),
    }


def test_per_class_counts_sum_to_the_totals_for_any_subset_of_hits():
    truth = combined_truth(TARGETS)
    items = truth.findings
    for take in (0, 1, 4, 7, 10):
        hits = [
            Finding(
                finding_id=f"f{i}",
                file=t.file,
                line_start=t.line_start,
                line_end=t.line_end,
                vuln_class=t.vuln_class,
                severity=t.severity,
                title="t",
                explanation="e",
                confidence=0.5,
            )
            for i, t in enumerate(items[:take])
        ]
        counts, s = _class_counts(hits, truth)
        assert sum(h for h, _ in counts.values()) == s.tp == take
        assert sum(k for _, k in counts.values()) == s.tp + s.fn == 10
