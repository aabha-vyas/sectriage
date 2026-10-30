"""Failure-injection harness: unit tests (default suite) and real-spawn tests (`-m chaos`).

The unit tests run no worker process. The `chaos` tests spawn workers, SIGKILL one and take about
35 s together; they are skipped unless selected with `-m chaos`. Running one by node id with no
`-m` skips it silently, by design.
"""

import re
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest

from src.agent.tools.registry import ToolRegistry
from src.swarm import chaos
from src.swarm.chaos import (
    HEARTBEAT,
    PRE_ACK,
    RANDOM_TIME,
    TASK_ID,
    TOOL_CALL,
    TrialPlan,
)
from src.swarm.sqlite_queue import SqliteQueue
from tests.swarm.fakes import make_finding, make_result, make_task

CHAOS_TOKEN = re.compile(r"(?<!not )\bchaos\b")


def chaos_selected(markexpr: str) -> bool:
    """True when `-m` asks for the chaos tests. `not chaos` must not count."""
    return bool(CHAOS_TOKEN.search(markexpr))


@pytest.fixture(autouse=True)
def _skip_chaos_unless_selected(request: pytest.FixtureRequest) -> None:
    if request.node.get_closest_marker("chaos") and not chaos_selected(
        request.config.getoption("markexpr") or ""
    ):
        pytest.skip("real-spawn chaos test: run with -m chaos")


# statistics, plan, canonical bytes ----------------------------------------------------------


def test_wilson_matches_known_values() -> None:
    lo, hi = chaos.wilson(20, 20)
    assert (round(lo, 3), hi) == (0.839, 1.0)
    lo, hi = chaos.wilson(10, 20)
    assert (round(lo, 3), round(hi, 3)) == (0.299, 0.701)
    assert round(chaos.wilson(4, 4)[0], 3) == 0.51
    assert chaos.wilson(0, 20)[0] == 0.0
    assert chaos.wilson(0, 0) == (0.0, 1.0)


def test_plan_is_seeded_and_stratified() -> None:
    plan = chaos.Planner(20, 1).drawn
    assert plan == chaos.Planner(20, 1).drawn
    assert plan != chaos.Planner(20, 2).drawn
    assert Counter(p.window for p in plan) == {
        RANDOM_TIME: 8,
        TOOL_CALL: 4,
        HEARTBEAT: 4,
        PRE_ACK: 4,
    }
    assert [p.index for p in plan] == list(range(20))
    for p in plan:
        assert p.sub in chaos.SUBPOINTS[p.window]
        assert (p.k in chaos.HEARTBEAT_INDEXES) == (p.window == HEARTBEAT)
        assert (p.frac is not None and 0 <= p.frac < 1) == (p.window == RANDOM_TIME)


def test_redraw_continues_the_same_stream() -> None:
    a, b = chaos.Planner(20, 1), chaos.Planner(20, 1)
    first, second = a.redraw(HEARTBEAT), b.redraw(HEARTBEAT)
    assert first == second
    assert first.index == 20


def test_planner_rejects_zero_trials() -> None:
    with pytest.raises(ValueError):
        chaos.Planner(0, 1)


def test_canonical_findings_ignore_order_and_see_changes() -> None:
    a, b = make_finding(3), make_finding(9)
    assert chaos.canonical_findings([a, b]) == chaos.canonical_findings([b, a])
    changed = b.model_copy(update={"title": "other"})
    assert chaos.canonical_findings([a, b]) != chaos.canonical_findings([a, changed])


# invariant checker and runner ---------------------------------------------------------------


def good_record(window: str = HEARTBEAT, index: int = 0) -> dict[str, Any]:
    """A counted trial that meets every invariant."""
    return {
        "plan": {"index": index, "window": window, "sub": "in_tx", "k": 1, "frac": None},
        "window": window,
        "workdir": "/nonexistent",
        "fired": True,
        "kill": {"point": "heartbeat_in_tx", "t_kill": 100.0},
        "completed": True,
        "state": "done",
        "stop_reason": "finish",
        "attempts": 2,
        "last_error": "lease expired",
        "winning_attempt": 2,
        "result_rows": 1,
        "result_rows_total": 1,
        "log_name": f"{TASK_ID}.2.jsonl",
        "kill_to_result_s": 2.0,
        "exit_codes": [-signal.SIGKILL, 0],
        "takeover": {
            "observed": True,
            "claim_at": 99.0,
            "last_lease_before": 101.0,
            "takeover_at": 101.2,
            "gap_s": 0.2,
            "stayed_running": True,
        },
        "killed_log": {"events": 3, "has_terminal": False, "unchanged_by_replay": True},
        "findings_sha256": "abc",
        "findings_equal_control": True,
        "replay": {"error": None, "identical": True, "stop_equal": True},
        "cost_usd": 0.0,
        "violations": [],
    }


def good_control() -> dict[str, Any]:
    return {
        "window": "control",
        "completed": True,
        "state": "done",
        "stop_reason": "finish",
        "attempts": 1,
        "last_error": None,
        "result_rows": 1,
        "exit_codes": [0, 0],
        "cost_usd": 0.0,
        "duration_s": 1.1,
        "violations": [],
    }


def test_a_good_record_has_no_violations() -> None:
    assert chaos.check_trial(good_record()) == []
    assert chaos.classify(good_record()) == "counted"


@pytest.mark.parametrize(
    ("change", "invariant", "kind"),
    [
        ({"completed": False, "state": "running"}, "completed", "bug"),
        ({"attempts": 3}, "attempts", "bug"),
        ({"result_rows": 2}, "one_result_row", "bug"),
        ({"result_rows_total": 2}, "one_result_row", "bug"),
        ({"exit_codes": [-9, -9]}, "exit_codes", "bug"),
        ({"exit_codes": [-9, None]}, "exit_codes", "bug"),
        ({"findings_equal_control": False}, "findings_match_control", "bug"),
        (
            {"replay": {"error": None, "identical": False, "stop_equal": True}},
            "replay_exact",
            "bug",
        ),
        (
            {"replay": {"error": "no log", "identical": None, "stop_equal": None}},
            "replay_exact",
            "bug",
        ),
        ({"log_name": f"{TASK_ID}.1.jsonl"}, "attempt_logs", "bug"),
        ({"cost_usd": 0.01}, "no_spend", "bug"),
        ({"last_error": "boom"}, "lease_takeover", "bug"),
    ],
)
def test_checker_flags_each_violation(change: dict[str, Any], invariant: str, kind: str) -> None:
    record = {**good_record(), **change}
    found = chaos.check_trial(record)
    assert [(v["invariant"], v["kind"]) for v in found][:1] == [(invariant, kind)]


def test_checker_flags_takeover_problems() -> None:
    early = good_record()
    early["takeover"] = {**early["takeover"], "takeover_at": 100.5}
    assert chaos.check_trial(early)[0]["kind"] == "bug"

    slow = good_record()
    slow["takeover"] = {**slow["takeover"], "takeover_at": 102.5, "gap_s": 1.5}
    found = chaos.check_trial(slow)
    assert [(v["invariant"], v["kind"]) for v in found] == [("lease_takeover", "timing")]

    nacked = good_record()
    nacked["takeover"] = {**nacked["takeover"], "stayed_running": False}
    assert chaos.check_trial(nacked)[0]["invariant"] == "lease_takeover"

    unseen = good_record()
    unseen["takeover"] = {"observed": False, "claim_at": 99.0}
    assert chaos.check_trial(unseen)[0]["kind"] == "timing"


def test_checker_flags_a_terminal_event_in_a_log_that_should_be_cut_short() -> None:
    record = good_record(HEARTBEAT)
    record["killed_log"] = {**record["killed_log"], "has_terminal": True}
    assert chaos.check_trial(record)[0]["invariant"] == "attempt_logs"
    ack = good_record(PRE_ACK)
    ack["killed_log"] = {**ack["killed_log"], "has_terminal": True}
    assert chaos.check_trial(ack) == []  # a pre_ack kill happens after the terminal event


def test_classify_separates_late_and_unfired_kills() -> None:
    late = {**good_record(), "winning_attempt": 1}
    unfired = {**good_record(), "fired": False, "winning_attempt": 2}
    harness_late = {**good_record(RANDOM_TIME), "fired": False, "winning_attempt": 1}
    assert chaos.classify(late) == "late_kill"
    assert chaos.classify(unfired) == "not_fired"
    assert chaos.classify(harness_late) == "late_kill"
    assert chaos.classify({**good_record(), "window": "control"}) == "control"


def test_control_check_wants_one_clean_attempt() -> None:
    assert chaos.check_control(good_control()) == []
    assert chaos.check_control({**good_control(), "attempts": 2})
    assert chaos.check_control({**good_control(), "exit_codes": [0, -9]})
    assert chaos.check_control({**good_control(), "last_error": "lease expired"})


def run(
    trial: Callable[[TrialPlan], dict[str, Any]],
    *,
    trials: int = 20,
    control: Callable[[], tuple[dict[str, Any], bytes]] = lambda: (good_control(), b"x"),
    **kwargs: Any,
) -> dict[str, Any]:
    return chaos.run_experiment(
        trials,
        1,
        control_fn=control,
        trial_fn=lambda plan, control_s, control_bytes: trial(plan),
        **kwargs,
    )


def test_runner_passes_when_every_trial_holds() -> None:
    seen: list[TrialPlan] = []

    def trial(plan: TrialPlan) -> dict[str, Any]:
        seen.append(plan)
        return good_record(plan.window, plan.index)

    report = run(trial)
    assert (report["verdict"], report["exit_code"], report["counted"]) == ("pass", 0, 20)
    assert len(seen) == 20
    overall = report["aggregates"]["overall"]
    assert overall["completed"]["ok"] == 20
    assert round(overall["completed"]["wilson_lo"], 2) == 0.84


def test_runner_stops_at_the_first_violation_and_does_not_retry() -> None:
    calls: list[int] = []

    def trial(plan: TrialPlan) -> dict[str, Any]:
        calls.append(plan.index)
        record = good_record(plan.window, plan.index)
        if len(calls) == 3:
            record["result_rows"] = 2
        return record

    report = run(trial)
    assert (report["verdict"], report["exit_code"]) == ("fail", chaos.EXIT_VIOLATION)
    assert len(calls) == 3
    assert report["first_violation"]["invariant"] == "one_result_row"
    assert report["first_violation"]["trial"] == calls[2]
    assert report["counted"] == 2


def test_runner_redraws_unfired_kills_without_counting_them() -> None:
    calls: list[int] = []

    def trial(plan: TrialPlan) -> dict[str, Any]:
        calls.append(plan.index)
        record = good_record(plan.window, plan.index)
        if len(calls) in (2, 5):
            record["winning_attempt"] = 1
        return record

    report = run(trial)
    assert report["verdict"] == "pass"
    assert report["excluded"] == {"not_fired": 0, "late_kill": 2}
    assert len(calls) == 22 and report["counted"] == 20


def test_runner_stops_when_too_many_kills_do_not_test_recovery() -> None:
    def trial(plan: TrialPlan) -> dict[str, Any]:
        return {**good_record(plan.window, plan.index), "fired": False}

    report = run(trial)
    assert (report["verdict"], report["exit_code"]) == ("fail", chaos.EXIT_EXCLUDED)
    assert report["excluded"]["not_fired"] == 6  # the sixth is the first past 25% of 20
    assert report["counted"] == 0


def test_runner_threshold_allows_exactly_a_quarter() -> None:
    n = {"calls": 0}

    def trial(plan: TrialPlan) -> dict[str, Any]:
        n["calls"] += 1
        record = good_record(plan.window, plan.index)
        if n["calls"] <= 5:
            record["winning_attempt"] = 1
        return record

    report = run(trial)
    assert (report["verdict"], report["counted"]) == ("pass", 20)
    assert report["excluded"]["late_kill"] == 5


def test_runner_aborts_when_controls_disagree_or_are_dirty() -> None:
    flip = iter([b"a", b"b"])
    report = run(
        lambda p: good_record(p.window, p.index),
        control=lambda: (good_control(), next(flip)),
    )
    assert (report["verdict"], report["exit_code"]) == ("abort", chaos.EXIT_ABORT)
    assert "disagree" in report["reason"]

    dirty = run(
        lambda p: good_record(p.window, p.index),
        control=lambda: ({**good_control(), "attempts": 2}, b"x"),
    )
    assert dirty["exit_code"] == chaos.EXIT_ABORT


def test_runner_aborts_at_the_runtime_cap() -> None:
    ticks = iter(range(0, 10_000, 100))
    report = run(
        lambda p: good_record(p.window, p.index),
        max_runtime_s=250,
        clock=lambda: float(next(ticks)),
    )
    assert (report["verdict"], report["exit_code"]) == ("abort", chaos.EXIT_ABORT)
    assert "runtime cap" in report["reason"]


def test_aggregate_reports_per_window_intervals() -> None:
    bad = good_record(HEARTBEAT)
    bad["violations"] = [{"invariant": "attempts", "kind": "bug", "detail": ""}]
    agg = chaos.aggregate([good_record(TOOL_CALL), good_record(HEARTBEAT), bad])
    assert agg["overall"]["attempts"]["ok"] == 2
    assert agg["by_window"][HEARTBEAT]["attempts"] == {
        "ok": 1,
        "n": 2,
        "wilson_lo": pytest.approx(0.0945, abs=1e-3),
        "wilson_hi": pytest.approx(0.9055, abs=1e-3),
    }
    assert agg["by_window"][PRE_ACK]["n"] == 0


# takeover analysis --------------------------------------------------------------------------


def test_takeover_analysis_reads_the_polled_rows() -> None:
    s = chaos.Sample
    samples = [
        s(0.0, 1, "running", 11.0, 10.0, 7),
        s(0.3, 1, "running", 11.3, 10.3, 7),
        s(1.5, 2, "running", 12.7, 11.5, 8),
    ]
    got = chaos.analyse_takeover(samples)
    assert got["observed"] and got["stayed_running"]
    assert got["last_lease_before"] == 11.3 and got["takeover_at"] == 11.5
    assert got["gap_s"] == pytest.approx(0.2)
    assert chaos.analyse_takeover(samples[:2])["observed"] is False
    assert chaos.analyse_takeover([])["observed"] is False


# output -------------------------------------------------------------------------------------


def test_splice_is_idempotent_and_leaves_the_prose_alone() -> None:
    doc = "before\n\n<!-- results:start -->\nold\n<!-- results:end -->\n\nafter\n"
    once = chaos.splice_results(doc, "new table")
    assert once.startswith("before\n\n") and once.endswith("\n\nafter\n")
    assert "old" not in once and "new table" in once
    assert chaos.splice_results(once, "new table") == once
    with pytest.raises(ValueError):
        chaos.splice_results("no markers", "x")


def test_splice_fills_only_the_named_section() -> None:
    doc = "<!-- a:start -->\nA\n<!-- a:end -->\n<!-- b:start -->\nB\n<!-- b:end -->\n"
    out = chaos.splice_results(doc, "new", "b")
    assert "\nA\n" in out and "\nB\n" not in out and "new" in out
    with pytest.raises(ValueError):
        chaos.splice_results(doc, "x", "c")


# --windows ----------------------------------------------------------------------------------


def test_parse_windows() -> None:
    assert chaos.parse_windows("pre_ack:in_tx") == {PRE_ACK: "in_tx"}
    assert chaos.parse_windows("heartbeat, tool_call:entry") == {
        HEARTBEAT: None,
        TOOL_CALL: "entry",
    }
    for bad in ("nope", "pre_ack:nope", "pre_ack,pre_ack:in_tx", "random_time:x"):
        with pytest.raises(ValueError):
            chaos.parse_windows(bad)


def test_windows_option_pins_every_kill_inside_the_ack_transaction() -> None:
    planner = chaos.Planner(20, 4, chaos.parse_windows("pre_ack:in_tx"))
    assert {(p.window, p.sub, p.k, p.frac) for p in planner.drawn} == {
        (PRE_ACK, "in_tx", None, None)
    }
    assert len(planner.drawn) == 20
    again = planner.redraw(PRE_ACK)  # a redraw keeps the pinned point
    assert (again.window, again.sub, again.index) == (PRE_ACK, "in_tx", 20)


def test_windows_option_splits_trials_evenly_and_keeps_free_points_seeded() -> None:
    planner = chaos.Planner(7, 3, chaos.parse_windows("heartbeat,tool_call:entry"))
    assert Counter(p.window for p in planner.drawn) == {HEARTBEAT: 4, TOOL_CALL: 3}
    assert {p.sub for p in planner.drawn if p.window == TOOL_CALL} == {"entry"}
    assert {p.sub for p in planner.drawn if p.window == HEARTBEAT} <= set(
        chaos.SUBPOINTS[HEARTBEAT]
    )
    assert (
        planner.drawn == chaos.Planner(7, 3, chaos.parse_windows("heartbeat,tool_call:entry")).drawn
    )


def test_default_plan_is_unchanged_by_the_windows_option() -> None:
    """Seed 1's plan is what the exploratory run used; adding --windows must not move it."""
    plan = chaos.Planner(20, 1).drawn
    assert [(p.window, p.sub) for p in plan[:3]] == [
        (TOOL_CALL, "after_tool"),
        (RANDOM_TIME, None),
        (PRE_ACK, "before_ack"),
    ]
    assert plan == chaos.Planner(20, 1, None).drawn


def test_runner_records_the_windows_it_was_given() -> None:
    windows = chaos.parse_windows("pre_ack:in_tx")
    report = chaos.run_experiment(
        20,
        4,
        control_fn=lambda: (good_control(), b"x"),
        trial_fn=lambda plan, s, b: good_record(plan.window, plan.index),
        windows=windows,
    )
    assert report["windows"] == windows
    assert {p["window"] for p in report["plan_drawn"]} == {PRE_ACK}
    assert report["counted"] == 20
    report["git_sha"] = "0123456789abcdef"
    header = chaos.render_results(report, "cmd").split("| invariant |")[1].split("\n")[0]
    assert "pre_ack" in header and "heartbeat" not in header  # windows with no trials are omitted


def test_render_and_compact_on_a_passing_report() -> None:
    report = run(lambda p: good_record(p.window, p.index))
    report["git_sha"] = "0123456789abcdef"
    text = chaos.render_results(report, "cmd")
    assert "**Verdict: pass**" in text and "`one_result_row`" in text
    assert "20/20 (0.84 to 1.00)" in text
    assert "| `one_result_row` | 20/20 (0.84 to 1.00) | 8/8" in text
    slim = chaos.compact(
        deepcopy({**report, "trials": [{**t, "samples": [1]} for t in report["trials"]]})
    )
    assert all("samples" not in t and "workdir" not in t for t in slim["trials"])


# environment hygiene ------------------------------------------------------------------------


def test_spawn_env_removes_the_api_key_and_points_at_the_plan(tmp_path: Path) -> None:
    env = chaos.spawn_env(tmp_path / "plan.json", 0.15)
    assert env["ANTHROPIC_API_KEY"] is None
    assert env[chaos.PLAN_ENV] == str(tmp_path / "plan.json")
    assert env["SWARM_DEMO_STEP_DELAY_S"] == "0.15"
    assert env["PYTHONPATH"]


def test_environ_context_restores_even_after_an_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.delenv("SWARM_CHAOS_PLAN", raising=False)
    monkeypatch.setenv("PYTHONPATH", "keep")
    before = dict(__import__("os").environ)
    with pytest.raises(RuntimeError):
        with chaos._environ(
            {"ANTHROPIC_API_KEY": None, "SWARM_CHAOS_PLAN": "x", "PYTHONPATH": "y"}
        ):
            env = __import__("os").environ
            assert "ANTHROPIC_API_KEY" not in env and env["SWARM_CHAOS_PLAN"] == "x"
            raise RuntimeError
    assert dict(__import__("os").environ) == before


def test_importing_in_the_test_process_patches_nothing(monkeypatch: pytest.MonkeyPatch) -> None:
    """A plan variable alone must not patch a process that is not a spawned child."""
    monkeypatch.setenv(chaos.PLAN_ENV, "/nonexistent/plan.json")
    connect, execute = sqlite3.connect, ToolRegistry.execute
    chaos._install_from_env()  # would raise on the missing plan file if it tried to install
    assert sqlite3.connect is connect and ToolRegistry.execute is execute
    assert not getattr(sqlite3.connect, "_chaos_hook", False)


# hook markers (in-process; does not prove delivery to child processes, the chaos tests do) --


def _stub_execute(self: object, call_id: str, name: str, raw_args: str, *, timeout_s: float) -> str:
    return "ok"


@pytest.mark.parametrize(
    ("window", "sub", "k", "point"),
    [
        (TOOL_CALL, "entry", None, "tool_entry"),
        (TOOL_CALL, "after_tool", None, "tool_done"),
        (HEARTBEAT, "before_tx", 1, "heartbeat_before_tx"),
        (HEARTBEAT, "in_tx", 2, "heartbeat_in_tx"),
        (PRE_ACK, "before_ack", None, "before_ack"),
        (PRE_ACK, "in_tx", None, "ack_in_tx"),
    ],
)
def test_hooks_fire_at_their_marker_once_and_are_removed(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    window: str,
    sub: str,
    k: int | None,
    point: str,
) -> None:
    """Drives a real SqliteQueue through claim, heartbeat x2 and ack with the kill swapped for a
    recorder. The in-transaction markers must fire with the transaction still uncommitted."""
    db = tmp_path / "queue.db"
    observed: list[dict[str, Any]] = []
    real_connect = sqlite3.connect  # taken before install: the observer must not be traced

    def record_instead_of_dying() -> None:
        with closing(real_connect(db)) as c:
            state, lease = c.execute("SELECT state, lease_expires_at FROM tasks").fetchone()
            rows = c.execute("SELECT COUNT(*) FROM results").fetchone()[0]
        observed.append({"state": state, "lease": lease, "rows": rows})

    monkeypatch.setattr(chaos, "_kill_self", record_instead_of_dying)
    monkeypatch.setattr(ToolRegistry, "execute", _stub_execute)
    originals = (sqlite3.connect, ToolRegistry.execute, SqliteQueue.heartbeat, SqliteQueue.ack)
    cfg = {"armed": True, "window": window, "sub": sub, "k": k, "marker": str(tmp_path / "k.json")}

    uninstall = chaos.install_child_hooks(cfg)
    try:
        queue = SqliteQueue(db)
        queue.enqueue(make_task(TASK_ID))
        claim = queue.claim(1234, 5.0)
        assert claim is not None
        ToolRegistry.execute(object(), "c1", "read_file", "{}", timeout_s=1.0)
        queue.heartbeat(TASK_ID, claim.token, 5.0)
        queue.heartbeat(TASK_ID, claim.token, 5.0)
        queue.ack(TASK_ID, claim.token, make_result(log_path=f"events/{TASK_ID}.1.jsonl"))
        queue.close()
    finally:
        uninstall()

    marker = chaos.json.loads((tmp_path / "k.json").read_text())
    assert marker["point"] == point and marker["window"] == window
    assert len(observed) == 1  # one kill per trial, however many points are passed afterwards
    if point == "ack_in_tx":
        assert (observed[0]["state"], observed[0]["rows"]) == ("running", 0)
    assert (sqlite3.connect, ToolRegistry.execute, SqliteQueue.heartbeat, SqliteQueue.ack) == (
        originals[0],
        _stub_execute,
        originals[2],
        originals[3],
    )


def test_a_disarmed_plan_passes_everything_through(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    fired: list[str] = []
    monkeypatch.setattr(chaos, "_kill_self", lambda: fired.append("died"))
    cfg = {
        "armed": False,
        "window": PRE_ACK,
        "sub": "in_tx",
        "k": None,
        "marker": str(tmp_path / "k"),
    }
    uninstall = chaos.install_child_hooks(cfg)
    try:
        queue = SqliteQueue(tmp_path / "queue.db")
        queue.enqueue(make_task(TASK_ID))
        claim = queue.claim(1, 5.0)
        assert claim is not None
        assert queue.ack(TASK_ID, claim.token, make_result())
        queue.close()
    finally:
        uninstall()
    assert fired == [] and not (tmp_path / "k").exists()


def test_hooks_leave_no_patch_or_env_behind() -> None:
    import os

    env = dict(os.environ)
    assert not getattr(sqlite3.connect, "_chaos_hook", False)
    assert "_chaos_hook" not in vars(SqliteQueue.heartbeat) and "_chaos_hook" not in vars(
        SqliteQueue.ack
    )
    assert dict(os.environ) == env


def test_chaos_selection_ignores_negated_expressions() -> None:
    assert chaos_selected("chaos")
    assert chaos_selected("chaos and not docker")
    assert not chaos_selected("not chaos")
    assert not chaos_selected("")
    assert not chaos_selected("not docker")


# real-spawn tests (about 35 s together); `uv run pytest -m chaos tests/swarm/test_chaos.py` -----

_CONTROL: dict[str, Any] = {}


@pytest.fixture
def cfg(tmp_path: Path) -> chaos.Config:
    return chaos.Config(tmp_path)


@pytest.fixture
def control(cfg: chaos.Config, tmp_path_factory: pytest.TempPathFactory) -> tuple[float, bytes]:
    """Two uninterrupted runs, once per session: they must be clean and agree."""
    if not _CONTROL:
        root = tmp_path_factory.mktemp("control")
        first, first_bytes = chaos.run_trial(None, chaos.Config(root))
        second, second_bytes = chaos.run_trial(None, chaos.Config(root))
        _CONTROL.update(
            first=first, second=second, bytes=first_bytes, same=first_bytes == second_bytes
        )
    return _CONTROL["first"]["duration_s"], _CONTROL["bytes"]


@pytest.mark.chaos
def test_control_runs_are_clean_and_agree(control: tuple[float, bytes]) -> None:
    assert chaos.check_control(_CONTROL["first"]) == []
    assert chaos.check_control(_CONTROL["second"]) == []
    assert _CONTROL["same"]
    assert control[0] > 0.5  # 7 steps at 0.15 s each


def _run_fixed(plan: TrialPlan, cfg: chaos.Config, control: tuple[float, bytes]) -> dict[str, Any]:
    start = time.monotonic()
    record, _ = chaos.run_trial(plan, cfg, control_s=control[0], control_bytes=control[1])
    assert time.monotonic() - start < cfg.trial_timeout_s
    assert chaos.classify(record) == "counted", record["kill"]
    assert chaos.check_trial(record) == []
    return record


@pytest.mark.chaos
def test_kill_during_a_tool_call(cfg: chaos.Config, control: tuple[float, bytes]) -> None:
    record = _run_fixed(TrialPlan(0, TOOL_CALL, "after_tool"), cfg, control)
    assert record["kill"]["point"] == "tool_done"


@pytest.mark.chaos
def test_kill_inside_a_heartbeat_transaction(
    cfg: chaos.Config, control: tuple[float, bytes]
) -> None:
    record = _run_fixed(TrialPlan(0, HEARTBEAT, "in_tx", k=1), cfg, control)
    assert record["kill"]["point"] == "heartbeat_in_tx"


@pytest.mark.chaos
def test_kill_inside_the_ack_transaction(cfg: chaos.Config, control: tuple[float, bytes]) -> None:
    """The atomicity case: `done` written, `results` not yet, then the process dies."""
    record = _run_fixed(TrialPlan(0, PRE_ACK, "in_tx"), cfg, control)
    assert record["kill"]["point"] == "ack_in_tx"
    assert record["killed_log"]["has_terminal"]  # the work was finished; only the ack was lost


@pytest.mark.chaos
def test_kill_at_a_random_time(cfg: chaos.Config, control: tuple[float, bytes]) -> None:
    record = _run_fixed(TrialPlan(0, RANDOM_TIME, frac=0.5), cfg, control)
    assert record["kill"]["point"] == "harness"


def test_a_failed_trial_is_published_in_the_table_like_the_others() -> None:
    def trial(plan: TrialPlan) -> dict[str, Any]:
        record = good_record(plan.window, plan.index)
        if plan.index == 1:
            record.update(result_rows=2, kill_to_result_s=None)
        return record

    report = run(trial)
    report["git_sha"], report["git_dirty"] = "0123456789abcdef", False
    text = chaos.render_results(report, "cmd")
    assert "**Verdict: fail**" in text and "First violation: trial 1, `one_result_row`" in text
    assert "FAIL one_result_row (bug)" in text
    assert "clean tree" in text
    assert "`one_result_row` | 1/2" in text  # the failed trial is in the denominator


def test_from_json_republishes_without_running(tmp_path: Path) -> None:
    report = run(lambda p: good_record(p.window, p.index))
    report.update(git_sha="0123456789abcdef", git_dirty=False, command="cmd --x")
    src, doc, runs = tmp_path / "r.json", tmp_path / "doc.md", tmp_path / "runs.json"
    src.write_text(chaos.json.dumps(report))
    doc.write_text("t\n<!-- run2:start -->\n<!-- run2:end -->\nend\n")
    code = chaos.main(
        [
            "--from-json",
            str(src),
            "--markdown",
            str(doc),
            "--results-id",
            "run2",
            "--runs-json",
            str(runs),
        ]
    )
    assert code == 0
    assert "Command: `cmd --x`" in doc.read_text() and doc.read_text().endswith("end\n")
    assert "samples" not in runs.read_text()
