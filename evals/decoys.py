"""Decoy manifest: code that looks risky but is not. Analysis only; it never changes a score.

A decoy is a region of a target that resembles a vulnerability (parameterized queries,
escaped output, shell=False, contained paths, environment secrets) and is shown safe by a
named behavioral test. Used to say where an agent's false positives landed.
"""

import json
from collections import Counter
from enum import StrEnum
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from evals.scorer import (
    GROUND_TRUTH_DIR,
    MAX_SPAN_LINES,
    GroundTruth,
    Span,
    false_positives,
)
from src.contracts import Finding


class DecoyKind(StrEnum):
    PARAMETERIZED_QUERY = "parameterized_query"
    ESCAPED_OUTPUT = "escaped_output"
    SHELL_FALSE = "shell_false"
    CONTAINED_PATH = "contained_path"
    ENV_SECRET = "env_secret"
    ALLOWLIST = "allowlist"
    CONSTANT_INTERPOLATION = "constant_interpolation"


class Decoy(Span):
    id: str
    kind: DecoyKind
    description: str
    snippet: str
    test: str  # name of the behavioral test that shows this region is safe


class DecoyManifest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    target: str
    decoys: list[Decoy]


class DecoyReport(BaseModel):
    fp_total: int
    on_decoy: int  # false positives that overlap a decoy
    wide: int  # false positives wider than MAX_SPAN_LINES (never match anything)
    elsewhere: int  # false positives on neither a decoy nor a wide span
    by_kind: dict[str, int]
    by_decoy: dict[str, int]


def load_decoys(target: str, directory: Path = GROUND_TRUTH_DIR) -> DecoyManifest | None:
    """None when the target has no manifest."""
    path = directory / f"{target}.decoys.json"
    if not path.is_file():
        return None
    return DecoyManifest.model_validate(json.loads(path.read_text()))


def classify_false_positives(
    findings: list[Finding], truth: GroundTruth, manifest: DecoyManifest
) -> DecoyReport:
    by_kind: Counter[str] = Counter()
    by_decoy: Counter[str] = Counter()
    on_decoy = wide = elsewhere = 0
    fps = false_positives(findings, truth)
    for f in fps:
        if f.line_end - f.line_start + 1 > MAX_SPAN_LINES:
            wide += 1
            continue
        hit = [
            d
            for d in manifest.decoys
            if d.file == f.file and f.line_start <= d.line_end and d.line_start <= f.line_end
        ]
        if not hit:
            elsewhere += 1
            continue
        on_decoy += 1
        for d in hit:
            by_decoy[d.id] += 1
            by_kind[d.kind.value] += 1
    return DecoyReport(
        fp_total=len(fps),
        on_decoy=on_decoy,
        wide=wide,
        elsewhere=elsewhere,
        by_kind=dict(sorted(by_kind.items())),
        by_decoy=dict(sorted(by_decoy.items())),
    )


def render_report(r: DecoyReport) -> str:
    kinds = ", ".join(f"{k}={n}" for k, n in r.by_kind.items()) or "none"
    return (
        f"fp on decoys  {r.on_decoy} of {r.fp_total} "
        f"(wide={r.wide}, elsewhere={r.elsewhere}; kinds: {kinds})"
    )
