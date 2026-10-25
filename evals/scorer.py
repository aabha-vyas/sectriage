"""Scores predicted findings against ground truth by file and line-range overlap."""

import json
from collections import defaultdict
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field, model_validator

from src.contracts import Finding, Severity, VulnClass

GROUND_TRUTH_DIR = Path(__file__).parent / "ground_truth"
MAX_SPAN_LINES = 25  # wider predictions never match; they count as false positives


class Span(BaseModel):
    model_config = ConfigDict(extra="forbid")

    file: str
    line_start: int = Field(ge=1)
    line_end: int = Field(ge=1)

    @model_validator(mode="after")
    def _range_ordered(self) -> "Span":
        if self.line_end < self.line_start:
            raise ValueError("line_end must be >= line_start")
        return self


class Location(Span):
    """An alternate place a correct report may cite, such as the entry point of a taint path."""

    snippet: str


class TruthItem(Span):
    id: str
    vuln_class: VulnClass
    severity: Severity
    description: str
    snippet: str
    also_accept: list[Location] = Field(default_factory=list)

    @property
    def spans(self) -> list[Span]:
        return [self, *self.also_accept]


class GroundTruth(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    findings: list[TruthItem]


class Score(BaseModel):
    tp: int
    fp: int
    fn: int
    precision: float | None
    recall: float | None
    f1: float | None
    recall_by_class: dict[str, float | None]


def load_ground_truth(target: str, directory: Path = GROUND_TRUTH_DIR) -> GroundTruth:
    return GroundTruth.model_validate(json.loads((directory / f"{target}.json").read_text()))


def dedupe(findings: list[Finding]) -> list[Finding]:
    seen: dict[tuple, Finding] = {}
    for f in findings:
        seen.setdefault(f.dedupe_key, f)
    return list(seen.values())


def _overlaps(a: Finding, t: TruthItem) -> bool:
    """True if the prediction overlaps the primary location or any also_accept location."""
    return any(
        a.file == s.file and a.line_start <= s.line_end and s.line_start <= a.line_end
        for s in t.spans
    )


def _ratio(num: int, den: int) -> float | None:
    return num / den if den else None


def _match(predictions: list[Finding], truth: GroundTruth) -> dict[int, int]:
    """Maximum bipartite matching of truth items to predictions: truth index -> prediction index."""
    overlaps = [
        [j for j, t in enumerate(truth.findings) if _overlaps(p, t)]
        if p.line_end - p.line_start + 1 <= MAX_SPAN_LINES
        else []
        for p in predictions
    ]
    match_of_truth: dict[int, int] = {}

    def augment(i: int, seen: set[int]) -> bool:
        for j in overlaps[i]:
            if j in seen:
                continue
            seen.add(j)
            if j not in match_of_truth or augment(match_of_truth[j], seen):
                match_of_truth[j] = i
                return True
        return False

    for i in range(len(predictions)):
        augment(i, set())
    return match_of_truth


def false_positives(findings: list[Finding], truth: GroundTruth) -> list[Finding]:
    """The deduplicated predictions that are not matched to any truth item."""
    predictions = dedupe(findings)
    matched = set(_match(predictions, truth).values())
    return [p for i, p in enumerate(predictions) if i not in matched]


def score(findings: list[Finding], truth: GroundTruth) -> Score:
    predictions = dedupe(findings)
    match_of_truth = _match(predictions, truth)
    tp = len(match_of_truth)
    claimed = {truth.findings[j].id for j in match_of_truth}
    fp = len(predictions) - tp
    fn = len(truth.findings) - tp

    precision = _ratio(tp, tp + fp)
    recall = _ratio(tp, tp + fn)
    f1 = (
        2 * precision * recall / (precision + recall)
        if precision is not None and recall is not None and precision + recall > 0
        else None
    )

    totals: dict[str, int] = defaultdict(int)
    hits: dict[str, int] = defaultdict(int)
    for t in truth.findings:
        totals[t.vuln_class.value] += 1
        hits[t.vuln_class.value] += t.id in claimed
    recall_by_class = {c: _ratio(hits[c], n) for c, n in sorted(totals.items())}

    return Score(
        tp=tp,
        fp=fp,
        fn=fn,
        precision=precision,
        recall=recall,
        f1=f1,
        recall_by_class=recall_by_class,
    )
