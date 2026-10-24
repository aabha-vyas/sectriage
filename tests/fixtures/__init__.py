"""Test-only helpers."""

from evals.scorer import GroundTruth
from src.contracts import Finding


def oracle_findings(truth: GroundTruth) -> list[Finding]:
    """Findings that exactly mirror the ground truth. Test use only, never the smoke run."""
    return [
        Finding(
            finding_id=t.id,
            file=t.file,
            line_start=t.line_start,
            line_end=t.line_end,
            vuln_class=t.vuln_class,
            severity=t.severity,
            title=t.id,
            explanation=t.description,
            confidence=1.0,
        )
        for t in truth.findings
    ]
