from evals.scorer import GroundTruth, TruthItem, dedupe, score
from src.contracts import Finding
from tests.fixtures import oracle_findings


def truth() -> GroundTruth:
    def item(id_, start, end, cls):
        return TruthItem(
            id=id_,
            file="a.py",
            line_start=start,
            line_end=end,
            vuln_class=cls,
            severity="high",
            description="d",
            snippet="s",
        )

    return GroundTruth(
        target="t",
        findings=[item("one", 10, 12, "sqli"), item("two", 20, 22, "xss")],
    )


def finding(start, end, file="a.py", cls="sqli", fid="f") -> Finding:
    return Finding(
        finding_id=fid,
        file=file,
        line_start=start,
        line_end=end,
        vuln_class=cls,
        severity="low",
        title="t",
        explanation="e",
        confidence=1.0,
    )


def test_oracle_scores_perfect_on_real_ground_truth():
    from evals.scorer import load_ground_truth

    gt = load_ground_truth("vuln_app")
    s = score(oracle_findings(gt), gt)
    assert (s.precision, s.recall, s.f1) == (1.0, 1.0, 1.0)
    assert all(r == 1.0 for r in s.recall_by_class.values())


def test_empty_findings():
    s = score([], truth())
    assert s.precision is None
    assert s.recall == 0.0 and s.fn == 2


def test_one_tp_one_fp():
    s = score([finding(10, 10), finding(50, 51)], truth())
    assert (s.tp, s.fp, s.fn) == (1, 1, 1)
    assert s.precision == 0.5 and s.recall == 0.5


def test_duplicates_collapse():
    dup = [finding(10, 12, fid="a"), finding(10, 12, fid="b")]
    assert len(dedupe(dup)) == 1
    s = score(dup, truth())
    assert (s.tp, s.fp) == (1, 0)


def test_two_distinct_predictions_on_one_truth_is_one_tp_one_fp():
    s = score([finding(10, 11), finding(12, 12)], truth())
    assert (s.tp, s.fp, s.fn) == (1, 1, 1)


def test_other_file_does_not_match():
    s = score([finding(10, 12, file="b.py")], truth())
    assert (s.tp, s.fp) == (0, 1)


def test_adjacent_ranges_do_not_overlap_and_edges_do():
    assert score([finding(13, 14)], truth()).tp == 0
    assert score([finding(9, 9)], truth()).tp == 0
    assert score([finding(12, 15)], truth()).tp == 1
    assert score([finding(8, 10)], truth()).tp == 1


def test_class_ignored_for_matching_but_per_class_recall_reported():
    s = score([finding(10, 12, cls="xss")], truth())
    assert s.tp == 1
    assert s.recall_by_class == {"sqli": 1.0, "xss": 0.0}


def test_score_is_independent_of_prediction_order():
    wide, narrow = finding(10, 22, fid="wide"), finding(10, 10, fid="narrow")
    a = score([wide, narrow], truth())
    b = score([narrow, wide], truth())
    assert a == b
    assert (a.tp, a.fp, a.fn) == (2, 0, 0)


def test_truth_item_rejects_reversed_range():
    import pytest
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        TruthItem(
            id="x",
            file="a.py",
            line_start=5,
            line_end=2,
            vuln_class="xss",
            severity="low",
            description="d",
            snippet="s",
        )


def test_max_span_prediction_still_matches():
    from evals.scorer import MAX_SPAN_LINES

    s = score([finding(10, 10 + MAX_SPAN_LINES - 1)], truth())
    assert (s.tp, s.fp) == (1, 0)


def test_prediction_wider_than_max_span_is_fp_even_if_it_overlaps():
    from evals.scorer import MAX_SPAN_LINES

    s = score([finding(10, 10 + MAX_SPAN_LINES)], truth())
    assert (s.tp, s.fp, s.fn) == (0, 1, 2)


def test_oversized_prediction_does_not_block_a_narrow_one():
    s = score([finding(1, 500, fid="huge"), finding(10, 12, fid="ok")], truth())
    assert (s.tp, s.fp, s.fn) == (1, 1, 1)


def test_load_ground_truth_rejects_bad_data(tmp_path):
    import json

    import pytest
    from pydantic import ValidationError

    from evals.scorer import load_ground_truth

    good = {
        "id": "x",
        "file": "a.py",
        "line_start": 1,
        "line_end": 2,
        "vuln_class": "xss",
        "severity": "low",
        "description": "d",
        "snippet": "s",
    }

    def write(name, payload):
        (tmp_path / f"{name}.json").write_text(
            payload if isinstance(payload, str) else json.dumps(payload)
        )

    write("notjson", "{nope")
    write(
        "reversed", {"target": "reversed", "findings": [{**good, "line_start": 5, "line_end": 2}]}
    )
    write("badclass", {"target": "badclass", "findings": [{**good, "vuln_class": "nope"}]})
    write("extra", {"target": "extra", "findings": [{**good, "bogus": 1}]})
    write("ok", {"target": "ok", "findings": [good]})

    assert len(load_ground_truth("ok", tmp_path).findings) == 1
    for name in ("notjson", "reversed", "badclass", "extra"):
        with pytest.raises(ValueError):
            load_ground_truth(name, tmp_path)
    with pytest.raises(ValidationError):
        load_ground_truth("reversed", tmp_path)
    with pytest.raises(FileNotFoundError):
        load_ground_truth("missing", tmp_path)
