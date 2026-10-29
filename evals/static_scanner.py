"""Static-scanner baseline: Semgrep output scored like an agent run, as one results row.

`python -m evals.static_scanner` scans the new targets with fixed rule packs (needs network for
the registry rules; metrics stay off) and writes, under evals/results/:
  static-scanner-semgrep-<version>.json           the row, in the Summary format
  static-scanner-semgrep-<version>.findings.json  the converted findings, rescorable offline
  static-scanner-semgrep-<version>.decoys.json    where its false positives landed

Results outside the five scored classes are dropped before scoring (they are out of scope).
"""

import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

from pydantic import BaseModel, ConfigDict

from evals.decoys import DecoyReport, classify_false_positives, load_decoys
from evals.metrics import run_summary
from evals.results import LEGACY_SCHEMA_VERSION, RESULTS_DIR, Summary, aggregate, save
from evals.scorer import GroundTruth, TruthItem, load_ground_truth, score
from evals.summary import CLEAN_STOPS, git_sha
from src.agent.memory import make_finding_id
from src.contracts import Finding, Severity, VulnClass

TARGETS = ["ledger_api", "notes_portal", "static_builder"]
PACKS = ["p/python", "p/flask", "p/security-audit", "p/secrets"]
ROW_TARGET = "new_targets"  # the three targets scored together

# First match wins. Matched against the last segment of the Semgrep rule id.
_CLASS_BY_RULE_PART: list[tuple[str, VulnClass]] = [
    ("sql", VulnClass.SQLI),
    ("subprocess", VulnClass.COMMAND_INJECTION),
    ("shell", VulnClass.COMMAND_INJECTION),
    ("command", VulnClass.COMMAND_INJECTION),
    ("traversal", VulnClass.PATH_TRAVERSAL),
    ("path", VulnClass.PATH_TRAVERSAL),
    ("xss", VulnClass.XSS),
    ("unescaped", VulnClass.XSS),
    ("raw-html", VulnClass.XSS),
    ("format-string", VulnClass.XSS),
    ("secret", VulnClass.HARDCODED_SECRET),
    ("password", VulnClass.HARDCODED_SECRET),
    ("hardcoded", VulnClass.HARDCODED_SECRET),
    ("api-key", VulnClass.HARDCODED_SECRET),
]
_SEVERITY = {"ERROR": Severity.HIGH, "WARNING": Severity.MEDIUM, "INFO": Severity.LOW}
_CONFIDENCE = {"HIGH": 0.9, "MEDIUM": 0.6, "LOW": 0.3}


class ScannerRow(BaseModel):
    """What the row records beyond the Summary: how many results were out of scope."""

    model_config = ConfigDict(extra="forbid")

    scanner: str
    version: str
    packs: list[str]
    raw_results: int
    dropped_out_of_scope: int


def vuln_class_for(rule_id: str) -> VulnClass | None:
    name = rule_id.rsplit(".", 1)[-1].lower()
    return next((c for part, c in _CLASS_BY_RULE_PART if part in name), None)


def convert(semgrep: dict) -> tuple[list[Finding], int]:
    """Semgrep JSON -> (findings in the five classes, number of results dropped)."""
    findings: list[Finding] = []
    dropped = 0
    for r in semgrep["results"]:
        cls = vuln_class_for(r["check_id"])
        if cls is None:
            dropped += 1
            continue
        extra = r["extra"]
        start, end = r["start"]["line"], r["end"]["line"]
        findings.append(
            Finding(
                finding_id=make_finding_id(r["path"], start, end, cls),
                file=r["path"],
                line_start=start,
                line_end=end,
                vuln_class=cls,
                severity=_SEVERITY.get(extra.get("severity", ""), Severity.MEDIUM),
                title=r["check_id"].rsplit(".", 1)[-1],
                explanation=extra.get("message", "").strip()[:300],
                confidence=_CONFIDENCE.get(extra.get("metadata", {}).get("confidence", ""), 0.5),
            )
        )
    return findings, dropped


def combined_truth(targets: list[str]) -> GroundTruth:
    items: list[TruthItem] = [t for n in targets for t in load_ground_truth(n).findings]
    return GroundTruth(target=ROW_TARGET, findings=items)


def build_row(
    findings: list[Finding], *, version: str, wall_s: float, now: datetime | None = None
) -> Summary:
    truth = combined_truth(TARGETS)
    meta = {
        "task_id": f"semgrep-{version}",
        "stop_reason": "finish",
        "steps": 1,
        "cost_usd": 0.0,
        "tokens": {},  # no model calls
    }
    run = run_summary(meta, [], score(findings, truth), wall_s)
    # A scanner row is not an agent run (no model, prompt or tool list), so it has no provenance.
    return Summary(
        schema_version=LEGACY_SCHEMA_VERSION,
        created_at=(now or datetime.now(UTC)).strftime("%Y-%m-%dT%H:%M:%SZ"),
        git_sha=git_sha(),
        target=ROW_TARGET,
        n_truth=len(truth.findings),
        model=f"static-scanner:semgrep-{version}",
        prompt_version="packs:" + "+".join(PACKS),
        sandbox="none",
        tools=["semgrep"],
        max_cost_usd=0.0,
        n_runs=1,
        runs=[run],
        aggregate=aggregate([run], CLEAN_STOPS),
    )


def decoy_report(findings: list[Finding]) -> DecoyReport:
    """False positives of the combined row, classified against all three decoy manifests."""
    manifests = [m for t in TARGETS if (m := load_decoys(t)) is not None]
    merged = manifests[0].model_copy(
        update={"target": ROW_TARGET, "decoys": [d for m in manifests for d in m.decoys]}
    )
    return classify_false_positives(findings, combined_truth(TARGETS), merged)


def stem(version: str) -> str:
    return f"static-scanner-semgrep-{version}"


def _semgrep() -> str:
    exe = shutil.which("semgrep")
    if exe is None:
        raise SystemExit("error: semgrep is not on PATH (uv tool install semgrep)")
    return exe


def semgrep_version() -> str:
    out = subprocess.run([_semgrep(), "--version"], capture_output=True, text=True, timeout=60)
    return out.stdout.strip().splitlines()[-1]


def scan(target: str) -> tuple[dict, float]:
    """Run Semgrep on one target from the repo root; returns its JSON and the wall seconds."""
    cmd = [_semgrep(), "--metrics=off", "--json", "--quiet"]
    for pack in PACKS:
        cmd += ["--config", pack]
    cmd.append(f"evals/targets/{target}")
    start = time.monotonic()
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    wall = time.monotonic() - start
    if out.returncode not in (0, 1):  # 1 = findings present with --error; be lenient
        raise RuntimeError(f"semgrep failed on {target}: exit {out.returncode}")
    data = json.loads(out.stdout)
    if data.get("errors"):
        raise RuntimeError(f"semgrep reported {len(data['errors'])} error(s) on {target}")
    return data, wall


def main(results_dir: Path = RESULTS_DIR) -> int:
    version = semgrep_version()
    findings: list[Finding] = []
    raw = dropped = 0
    wall = 0.0
    for target in TARGETS:
        data, seconds = scan(target)
        got, drop = convert(data)
        findings += got
        raw += len(data["results"])
        dropped += drop
        wall += seconds
    row = build_row(findings, version=version, wall_s=wall)
    base = results_dir / stem(version)
    save(row, Path(f"{base}.json"))
    Path(f"{base}.findings.json").write_text(
        json.dumps([f.model_dump(mode="json") for f in findings], indent=2) + "\n"
    )
    info = ScannerRow(
        scanner="semgrep",
        version=version,
        packs=PACKS,
        raw_results=raw,
        dropped_out_of_scope=dropped,
    )
    Path(f"{base}.decoys.json").write_text(
        json.dumps(
            {"row": info.model_dump(), "decoys": decoy_report(findings).model_dump()}, indent=2
        )
        + "\n"
    )
    run = row.runs[0]
    print(f"saved: {base}.json  tp/fp/fn {run.tp}/{run.fp}/{run.fn}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
