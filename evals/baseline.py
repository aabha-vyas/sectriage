"""Compares a saved summary to a baseline: comparability first, then tolerances.

Gated (regressions): precision, recall, clean-run rate, cost, cost per finding.
On a clean target (baseline n_truth == 0) there is nothing to recall, so false positives are gated
directly: mean false positives per run and the fp-free run rate may not get worse.
Warning only: per-class recall drops. Reported but not gated: latency.
"""

from dataclasses import dataclass, field

from evals.results import Baseline, Stat, Summary

# Setup fields that must match for two summaries to be comparable at all.
_SETUP_FIELDS = (
    "schema_version",
    "target",
    "n_truth",
    "model",
    "prompt_version",
    "sandbox",
    "tools",
    "max_steps",
)
# Inputs the model sees or the cost gate prices; compared only when both sides recorded them.
_PROVENANCE_SETUP = (
    "system_prompt_sha256",
    "tool_schemas_sha256",
    "target_sha256",
    "price_table_version",
    "effort",
)
# Recorded for context. A mismatch is a warning, never "not comparable". git_commit is left out
# on purpose: it differs on every commit.
_PROVENANCE_WATCH = ("anthropic_sdk_version", "max_tokens", "caching")
# Schema 3 only adds provenance, so 2 and 3 are comparable; any other pair must match exactly.
_SCHEMA_FAMILY = {2: 2, 3: 2}
_EPS = 1e-9


@dataclass
class Comparison:
    not_comparable: list[str] = field(default_factory=list)
    regressions: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)


def _mean(s: Stat | None) -> float | None:
    return None if s is None else s.mean


def _lower(name: str, cur: float | None, base: float | None, tol: float, out: list[str]) -> None:
    if base is None:
        return
    if cur is None:
        out.append(f"{name}: no value (baseline {base:.3f})")
    elif cur < base - tol - _EPS:
        out.append(f"{name}: {cur:.3f} < baseline {base:.3f} - {tol:.2f}")


def _higher(name: str, cur: float | None, base: float | None, rel: float, out: list[str]) -> None:
    if base is None:
        return
    if cur is None:
        out.append(f"{name}: no value (baseline {base:.4f})")
    elif cur > base * (1 + rel) + _EPS:
        out.append(f"{name}: {cur:.4f} > baseline {base:.4f} + {rel:.0%}")


def _clean_rate(s: Summary) -> float:
    return s.aggregate.clean_runs / max(s.n_runs, 1)


def _short(v: object) -> object:
    return v[:12] if isinstance(v, str) and len(v) == 64 else v  # sha256 hex


def _minor(version: str) -> str:
    return ".".join(version.split(".")[:2])


def _compare_provenance(summary: Summary, baseline: Baseline, result: Comparison) -> None:
    sp, bp = summary.provenance, baseline.provenance
    if sp is None and bp is None:
        return  # two legacy files: nothing to add to what the gate always did
    if sp is None or bp is None:
        absent = "summary" if sp is None else "baseline"
        result.warnings.append(
            f"provenance absent in {absent}: prompt, tool, target, price and effort "
            "settings not compared"
        )
        return
    for name in _PROVENANCE_SETUP:
        a, b = getattr(sp, name), getattr(bp, name)
        if a is not None and b is not None and a != b:
            result.not_comparable.append(
                f"provenance.{name}: {_short(a)!r} vs baseline {_short(b)!r}"
            )
    for name in _PROVENANCE_WATCH:
        a, b = getattr(sp, name), getattr(bp, name)
        if a is not None and b is not None and a != b:
            result.warnings.append(f"provenance.{name}: {a!r} vs baseline {b!r}")
    if _minor(sp.python_version) != _minor(bp.python_version):
        result.warnings.append(
            f"provenance.python_version: {sp.python_version} vs baseline {bp.python_version}"
        )
    if sp.git_dirty:
        result.warnings.append("summary was recorded from a dirty working tree")
    if bp.git_dirty:
        result.warnings.append("baseline was recorded from a dirty working tree")


def compare(summary: Summary, baseline: Baseline) -> Comparison:
    result = Comparison()
    for name in _SETUP_FIELDS:
        a, b = getattr(summary, name), getattr(baseline, name)
        if name == "schema_version":
            same = _SCHEMA_FAMILY.get(a, a) == _SCHEMA_FAMILY.get(b, b)
        else:
            same = a == b
        if not same:
            result.not_comparable.append(f"{name}: {a!r} vs baseline {b!r}")
    _compare_provenance(summary, baseline, result)
    if result.not_comparable:
        return result

    tol = baseline.tolerances
    cur, base = summary.aggregate, baseline.aggregate
    reg = result.regressions
    if baseline.n_truth != 0:  # on a clean target "reported nothing" is the goal; fp is gated below
        _lower("precision", _mean(cur.precision), _mean(base.precision), tol.precision, reg)
    _lower("recall", _mean(cur.recall), _mean(base.recall), tol.recall, reg)

    cur_clean, base_clean = _clean_rate(summary), _clean_rate(baseline)
    if cur_clean < base_clean - _EPS:
        reg.append(
            f"clean_runs: {cur.clean_runs}/{summary.n_runs} below baseline "
            f"{base.clean_runs}/{baseline.n_runs}"
        )

    if baseline.n_truth == 0:
        base_fp, cur_fp = _mean(base.fp), _mean(cur.fp)
        if base_fp is not None and cur_fp is not None and cur_fp > base_fp + _EPS:
            reg.append(f"fp: mean {cur_fp:.2f} per run > baseline {base_fp:.2f}")
        if summary.aggregate.fp_free_runs / max(summary.n_runs, 1) < (
            baseline.aggregate.fp_free_runs / max(baseline.n_runs, 1) - _EPS
        ):
            reg.append(
                f"fp_free_runs: {cur.fp_free_runs}/{summary.n_runs} below baseline "
                f"{base.fp_free_runs}/{baseline.n_runs}"
            )

    _higher("cost_usd", _mean(cur.cost_usd), _mean(base.cost_usd), tol.cost_rel, reg)
    _higher(
        "cost_per_finding_usd",
        _mean(cur.cost_per_finding_usd),
        _mean(base.cost_per_finding_usd),
        tol.cost_per_finding_rel,
        reg,
    )

    for cls, b in sorted(base.recall_by_class.items()):
        c = cur.recall_by_class.get(cls)
        _lower(
            f"recall[{cls}]",
            None if c is None else c.mean,
            b.mean,
            tol.recall_by_class,
            result.warnings,
        )
    return result
