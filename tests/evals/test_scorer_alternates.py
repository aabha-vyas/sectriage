import pytest
from pydantic import ValidationError

from evals.scorer import GroundTruth, Location, TruthItem, false_positives, score
from tests.evals.test_scorer import finding


def item(id_, start, end, cls="sqli", alt=()):
    return TruthItem(
        id=id_,
        file="sink.py",
        line_start=start,
        line_end=end,
        vuln_class=cls,
        severity="high",
        description="d",
        snippet="s",
        also_accept=[Location(file=f, line_start=a, line_end=b, snippet="s") for f, a, b in alt],
    )


def truth(*items) -> GroundTruth:
    return GroundTruth(target="t", findings=list(items))


CROSS = item("cross", 10, 12, alt=[("route.py", 5, 6), ("helper.py", 30, 30)])


def test_primary_location_matches():
    assert score([finding(10, 10, file="sink.py")], truth(CROSS)).tp == 1


@pytest.mark.parametrize("file,start,end", [("route.py", 6, 8), ("helper.py", 30, 30)])
def test_any_alternate_location_matches(file, start, end):
    s = score([finding(start, end, file=file)], truth(CROSS))
    assert (s.tp, s.fp, s.fn, s.recall) == (1, 0, 0, 1.0)


def test_alternate_matches_only_its_own_file():
    s = score([finding(5, 6, file="sink.py")], truth(CROSS))
    assert (s.tp, s.fp) == (0, 1)


def test_alternate_range_edges():
    assert score([finding(7, 9, file="route.py")], truth(CROSS)).tp == 0
    assert score([finding(6, 9, file="route.py")], truth(CROSS)).tp == 1


def test_reports_at_two_locations_of_one_bug_are_one_tp_one_fp():
    s = score(
        [finding(10, 10, file="sink.py", fid="a"), finding(5, 5, file="route.py", fid="b")],
        truth(CROSS),
    )
    assert (s.tp, s.fp, s.fn) == (1, 1, 0)


def test_alternate_of_one_item_can_free_the_primary_of_another():
    a = item("a", 10, 12, alt=[("other.py", 1, 3)])
    b = item("b", 11, 11, cls="xss")
    # One prediction sits on a's primary and b's primary; the other only on a's alternate.
    s = score(
        [finding(11, 11, file="sink.py", fid="p"), finding(1, 1, file="other.py", fid="q")],
        truth(a, b),
    )
    assert (s.tp, s.fp, s.fn) == (2, 0, 0)


def test_alternates_do_not_bypass_max_span():
    from evals.scorer import MAX_SPAN_LINES

    s = score([finding(1, 1 + MAX_SPAN_LINES, file="route.py")], truth(CROSS))
    assert (s.tp, s.fp) == (0, 1)


def test_per_class_recall_counts_alternate_hits():
    s = score([finding(30, 30, file="helper.py")], truth(CROSS, item("x", 40, 41, cls="xss")))
    assert s.recall_by_class == {"sqli": 1.0, "xss": 0.0}


def test_alternate_rejects_reversed_range_and_unknown_keys():
    with pytest.raises(ValidationError):
        Location(file="a.py", line_start=5, line_end=2, snippet="s")
    with pytest.raises(ValidationError):
        Location(file="a.py", line_start=1, line_end=2, snippet="s", extra=1)


def test_also_accept_defaults_to_empty_for_old_ground_truth():
    assert item("p", 1, 2).also_accept == []


# --- empty ground truth ----------------------------------------------------------------------


def test_empty_truth_no_findings_is_all_na_without_error():
    s = score([], truth())
    assert (s.tp, s.fp, s.fn) == (0, 0, 0)
    assert s.precision is None and s.recall is None and s.f1 is None
    assert s.recall_by_class == {}


def test_empty_truth_any_finding_is_a_false_positive():
    s = score([finding(1, 1, file="x.py"), finding(9, 9, file="y.py", fid="g")], truth())
    assert (s.tp, s.fp, s.fn) == (0, 2, 0)
    assert s.precision == 0.0 and s.recall is None and s.f1 is None


def test_false_positives_excludes_matches_on_primary_and_alternates():
    hits = [finding(10, 10, file="sink.py"), finding(5, 5, file="route.py")]
    other = finding(50, 50, file="sink.py")
    fps = false_positives([*hits, other], truth(CROSS))
    # one bug reported twice: one TP, the second report is a false positive, plus the stray one
    assert len(fps) == 2 and other in fps


def test_false_positives_agrees_with_score():
    findings = [finding(10, 10, file="sink.py"), finding(50, 50, file="sink.py")]
    assert len(false_positives(findings, truth(CROSS))) == score(findings, truth(CROSS)).fp


def test_false_positives_on_empty_truth_is_every_prediction():
    findings = [finding(10, 10, file="sink.py")]
    assert false_positives(findings, truth()) == findings
    assert false_positives([], truth()) == []


def test_false_positives_counts_wide_spans():
    wide = finding(1, 40, file="sink.py")
    assert false_positives([wide], truth(CROSS)) == [wide]
