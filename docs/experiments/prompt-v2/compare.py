"""Prints the prompt v1 vs v2 step-budget comparison from runs.json, then the criteria verdicts.

Usage: python docs/experiments/prompt-v2/compare.py [runs.json]

Reads only the JSON next to it (no logs, no API). This is a side-by-side report, not a gate result:
v1 and v2 summaries are not comparable through evals.baseline.
"""

import json
import statistics as st
import sys
from pathlib import Path

CLEAN_STOPS = {"finish", "max_steps"}
BUDGETS = (6, 10, 15, 30)
TARGETS = ("ledger_api", "notes_portal")


def mean(xs):
    xs = [x for x in xs if x is not None]
    return st.fmean(xs) if xs else None


def fmt(x, d=2):
    return "n/a" if x is None else f"{x:.{d}f}"


def column(runs):
    n = len(runs)
    firsts = [r["first_report_step"] for r in runs]
    got = [x for x in firsts if x is not None]
    classes = sorted({c for r in runs for c in r["recall_by_class"]})
    stops = {}
    for r in runs:
        stops[r["stop_reason"]] = stops.get(r["stop_reason"], 0) + 1
    return {
        "n": n,
        "recall": mean(r["recall"] for r in runs),
        "per_run_recall": [round(r["recall"], 2) for r in runs],
        "precision": mean(r["precision"] for r in runs),
        "fp": mean(float(r["fp"]) for r in runs),
        "fp_free": sum(r["fp"] == 0 for r in runs),
        "clean": sum(r["stop_reason"] in CLEAN_STOPS for r in runs),
        "cost": mean(r["cost_usd"] for r in runs),
        "cost_per_finding": mean(r["cost_usd"] / r["tp"] if r["tp"] else None for r in runs),
        "steps": mean(float(r["steps"]) for r in runs),
        "stops": stops,
        "first": st.median(got) if got else None,
        "per_run_first": firsts,
        "no_findings": sum(r["tp"] + r["fp"] == 0 for r in runs),
        "steps_unused": sum(r["steps"] < r["max_steps"] for r in runs),
        "by_class": {c: mean(r["recall_by_class"].get(c) for r in runs) for c in classes},
    }


def table(target, budget, a, b):
    rows = [
        ("mean recall", fmt(a["recall"]), fmt(b["recall"])),
        ("per-run recall", str(a["per_run_recall"]), str(b["per_run_recall"])),
        ("precision (mean)", fmt(a["precision"]), fmt(b["precision"])),
        ("fp / run", fmt(a["fp"]), fmt(b["fp"])),
        ("fp-free runs", f"{a['fp_free']}/{a['n']}", f"{b['fp_free']}/{b['n']}"),
        ("clean runs", f"{a['clean']}/{a['n']}", f"{b['clean']}/{b['n']}"),
        ("mean cost usd", fmt(a["cost"], 4), fmt(b["cost"], 4)),
        ("cost / finding usd", fmt(a["cost_per_finding"], 4), fmt(b["cost_per_finding"], 4)),
        ("mean steps used", fmt(a["steps"], 1), fmt(b["steps"], 1)),
        ("stop reasons", str(a["stops"]), str(b["stops"])),
        ("median 1st report", fmt(a["first"], 1), fmt(b["first"], 1)),
        ("per-run 1st report", str(a["per_run_first"]), str(b["per_run_first"])),
        ("runs w/o findings", str(a["no_findings"]), str(b["no_findings"])),
        ("runs w/ steps unused", str(a["steps_unused"]), str(b["steps_unused"])),
    ]
    for c in sorted(set(a["by_class"]) | set(b["by_class"])):
        rows.append((f"recall[{c}]", fmt(a["by_class"].get(c)), fmt(b["by_class"].get(c))))
    print(f"\n== {target}, max_steps {budget} ==")
    print(f"{'':27}{'v1':46}v2")
    for name, x, y in rows:
        print(f"{name:27}{x:46}{y}")


def criteria(cols):
    """The five pre-registered criteria, in the order they were written down."""
    v1 = {b: cols[b][0] for b in BUDGETS}
    v2 = {b: cols[b][1] for b in BUDGETS}
    first = {b: (v1[b]["first"], v2[b]["first"]) for b in BUDGETS}
    c30a, c30b = v1[30], v2[30]
    return [
        (
            "median first report is earlier at every budget",
            all(y < x for x, y in first.values()),
            "v1->v2 " + ", ".join(f"{b}: {fmt(x, 0)}->{fmt(y, 0)}" for b, (x, y) in first.items()),
        ),
        (
            "mean recall >= v1 at 6, 10, 15",
            all(v2[b]["recall"] >= v1[b]["recall"] - 1e-9 for b in (6, 10, 15)),
            ", ".join(
                f"{b}: {fmt(v1[b]['recall'])} vs {fmt(v2[b]['recall'])}" for b in (6, 10, 15)
            ),
        ),
        (
            "mean recall >= v1 + 0.2 at 6, 10",
            all(v2[b]["recall"] >= v1[b]["recall"] + 0.2 - 1e-9 for b in (6, 10)),
            ", ".join(f"{b}: {v2[b]['recall'] - v1[b]['recall']:+.2f}" for b in (6, 10)),
        ),
        (
            "at 30: recall >= 0.85, precision >= 0.90, cost <= 1.25 x v1, all runs clean",
            c30b["recall"] >= 0.85
            and c30b["precision"] >= 0.90
            and c30b["cost"] <= 1.25 * c30a["cost"]
            and c30b["clean"] == c30b["n"],
            f"recall {fmt(c30b['recall'])}, precision {fmt(c30b['precision'])}, "
            f"cost ratio {c30b['cost'] / c30a['cost']:.2f}, clean {c30b['clean']}/{c30b['n']}",
        ),
        (
            "fp-free runs stay at 5/5 at every budget",
            all(v2[b]["fp_free"] == v2[b]["n"] for b in BUDGETS),
            ", ".join(f"{b}: {v2[b]['fp_free']}/{v2[b]['n']}" for b in BUDGETS),
        ),
    ]


def main(path):
    data = json.loads(Path(path).read_text())
    for target in TARGETS:
        cols = {}
        for budget in BUDGETS:
            pair = []
            for arm in ("v1", "v2"):
                runs = [
                    r
                    for r in data["runs"]
                    if (r["target"], r["prompt_version"], r["max_steps"]) == (target, arm, budget)
                ]
                pair.append(column(runs))
            cols[budget] = tuple(pair)
            table(target, budget, *pair)
        print(f"\n== {target}: pre-registered criteria ==")
        results = criteria(cols)
        for i, (text, ok, numbers) in enumerate(results, 1):
            print(f"{i}. {'MET    ' if ok else 'NOT MET'}  {text}  [{numbers}]")
        print(f"   met {sum(ok for _, ok, _ in results)} of {len(results)}")


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else Path(__file__).with_name("runs.json"))
