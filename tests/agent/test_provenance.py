import json
from pathlib import Path

import pytest

from src.agent import pricing
from src.agent import run as cli
from src.agent.events import EventLog, header_line, is_header, read_events, read_header
from src.agent.loop import offered_tools
from src.agent.prompts import PROMPT_VERSION, SYSTEM_PROMPT
from src.agent.provenance import (
    Provenance,
    collect,
    git_state,
    lock_hash,
    sha256_json,
    tree_hashes,
)
from src.agent.sandbox_protocol import UnavailableSandbox
from src.agent.tools.registry import ToolRegistry
from tests.agent.fakes import FakeModel, tool_call
from tests.evals.builders import make_provenance

VULN_APP = cli.TARGETS_DIR / "vuln_app"


def git_with(commit: str | None, status: str | None):
    def git(args: list[str], cwd: Path) -> str | None:
        return commit if args[0] == "rev-parse" else status

    return git


def tools():
    return offered_tools(ToolRegistry(VULN_APP, UnavailableSandbox()))


def collect_for(target: Path = VULN_APP, **kw):
    fields = {
        "model": FakeModel([]),
        "system_prompt": SYSTEM_PROMPT,
        "tools": tools(),
        "sandbox_mode": "none",
        "max_steps": 30,
        "target": target,
        "repo_root": cli.REPO_ROOT,
        "git": git_with("a" * 40 + "\n", ""),
    }
    fields.update(kw)
    return collect(**fields)


# --- git -------------------------------------------------------------------------------------


def test_git_state_clean_dirty_and_unavailable():
    root = Path(".")
    assert git_state(root, git_with("abc\n", "")) == ("abc", False)
    assert git_state(root, git_with("abc\n", " M src/x.py\n")) == ("abc", True)
    assert git_state(root, git_with(None, None)) == (None, None)  # unknown is not "clean"


def test_dirty_status_excludes_saved_results_and_baselines():
    seen: list[list[str]] = []

    def git(args: list[str], cwd: Path) -> str | None:
        seen.append(args)
        return ""

    git_state(Path("."), git)
    status = next(a for a in seen if a[0] == "status")
    assert ":(exclude)evals/results" in status and ":(exclude)evals/baselines" in status
    assert "--untracked-files=all" in status


# --- hashes ----------------------------------------------------------------------------------


def test_hashes_follow_the_prompt_and_the_tools_not_key_order():
    base = collect_for()
    assert collect_for().system_prompt_sha256 == base.system_prompt_sha256
    assert collect_for(system_prompt=SYSTEM_PROMPT + " ").system_prompt_sha256 != (
        base.system_prompt_sha256
    )
    reordered = [dict(reversed(list(t.items()))) for t in tools()]
    assert collect_for(tools=reordered).tool_schemas_sha256 == base.tool_schemas_sha256
    changed = tools()
    changed[0]["description"] += "!"
    assert collect_for(tools=changed).tool_schemas_sha256 != base.tool_schemas_sha256


def test_tool_names_are_the_offered_tools_not_the_usable_ones():
    p = collect_for()
    assert p.tool_names == [
        "read_file",
        "search_code",
        "run_command",
        "semgrep",
        "git",
        "report_finding",
        "finish",
    ]


def test_hashes_match_what_the_model_actually_receives(tmp_path: Path):
    model = FakeModel([tool_call("finish", {})])
    assert (
        cli.main(["--target", str(VULN_APP), "--out", str(tmp_path / "f.json")], model=model) == 0
    )
    sent = model.calls[0]
    p = Provenance.model_validate(json.loads((tmp_path / "f.meta.json").read_text())["provenance"])
    assert p.tool_schemas_sha256 == sha256_json(sent["tools"])
    assert p.system_prompt_sha256 == collect_for(system_prompt=sent["system"]).system_prompt_sha256


def test_request_settings_are_recorded_when_the_client_has_them():
    class Client(FakeModel):
        effort = "medium"
        max_tokens = 123
        caching = True

    p = collect_for(model=Client([]))
    assert (p.effort, p.max_tokens, p.caching) == ("medium", 123, True)
    p = collect_for()  # a test double without them
    assert (p.effort, p.max_tokens, p.caching) == (None, None, None)


# --- target tree -----------------------------------------------------------------------------


def test_tree_hash_ignores_pycache_and_pyc_but_not_content(tmp_path: Path):
    target = tmp_path / "evals" / "targets" / "t"
    (target / "pkg").mkdir(parents=True)
    (target / "app.py").write_text("x = 1\n")
    (target / "pkg" / "m.py").write_text("y = 2\n")
    before = sha256_json(tree_hashes(target, tmp_path))

    (target / "pkg" / "__pycache__").mkdir()
    (target / "pkg" / "__pycache__" / "m.cpython-312.pyc").write_bytes(b"\x00junk")
    (target / "stray.pyc").write_bytes(b"\x01")
    assert sha256_json(tree_hashes(target, tmp_path)) == before

    (target / "app.py").write_text("x = 2\n")
    assert sha256_json(tree_hashes(target, tmp_path)) != before
    (target / "app.py").write_text("x = 1\n")
    (target / "new.txt").write_text("")
    assert sha256_json(tree_hashes(target, tmp_path)) != before


def test_unfrozen_target_hashes_the_live_tree_and_has_no_lock_hash():
    p = collect_for(VULN_APP)
    assert p.target_lock_sha256 is None
    assert len(p.target_sha256) == 64


@pytest.mark.parametrize("name", ["ledger_api", "notes_portal", "static_builder"])
def test_frozen_target_live_hash_equals_its_lock_hash(name: str):
    target = cli.TARGETS_DIR / name
    live = sha256_json(tree_hashes(target, cli.REPO_ROOT))
    assert lock_hash(target, cli.REPO_ROOT) == live


# --- price table version ---------------------------------------------------------------------


def test_price_version_tracks_rates_not_the_verification_date(monkeypatch: pytest.MonkeyPatch):
    model = "claude-haiku-5-5"
    before = pricing.price_table_version(model)
    monkeypatch.setattr(pricing, "VERIFIED_ON", "2030-01-01")
    assert pricing.price_table_version(model) == before
    price = pricing.PRICES[model]
    from dataclasses import replace

    cheaper = replace(price, base=replace(price.base, output=price.base.output + 1))
    monkeypatch.setitem(pricing.PRICES, model, cheaper)
    assert pricing.price_table_version(model) != before
    assert pricing.price_table_version("unpriced-model") is None


# --- the CLI ---------------------------------------------------------------------------------


def run_cli(tmp_path: Path, *extra: str, **kw) -> int:
    argv = ["--target", str(VULN_APP), "--out", str(tmp_path / "f.json"), *extra]
    return cli.main(argv, model=FakeModel([tool_call("finish", {})]), **kw)


def test_meta_records_provenance_and_the_log_starts_with_the_same_header(tmp_path: Path):
    assert run_cli(tmp_path, "--max-steps", "12") == 0
    meta = json.loads((tmp_path / "f.meta.json").read_text())
    p = Provenance.model_validate(meta["provenance"])
    assert p.model == "claude-haiku-5-5" and p.prompt_version == PROMPT_VERSION
    assert p.max_steps == 12 and p.sandbox_mode == "none"
    assert p.git_commit == "c" * 40 and p.git_dirty is False
    assert p.started_at and p.finished_at and p.finished_at >= p.started_at
    assert p.python_version and p.price_verified_on == meta["price_verified_on"]

    log = tmp_path / "f.events.jsonl"
    assert is_header(log.read_text().splitlines()[0])
    header = read_header(log)
    assert header is not None and header.finished_at is None  # unknown when the log opens
    assert header.model_copy(update={"finished_at": p.finished_at}) == p
    assert [e.step for e in read_events(log)] == [1, 2]  # header is not an event


def test_dirty_tree_warns_records_the_flag_and_still_runs(tmp_path: Path, capsys):
    code = run_cli(tmp_path, git=git_with("d" * 40 + "\n", " M src/agent/loop.py\n"))
    assert code == 0
    assert "dirty" in capsys.readouterr().err
    meta = json.loads((tmp_path / "f.meta.json").read_text())
    assert meta["provenance"]["git_dirty"] is True
    assert meta["provenance"]["git_commit"] == "d" * 40


def test_clean_tree_does_not_warn_and_missing_git_records_unknown(tmp_path: Path, capsys):
    assert run_cli(tmp_path) == 0
    assert "dirty" not in capsys.readouterr().err
    assert run_cli(tmp_path, git=git_with(None, None)) == 0
    p = json.loads((tmp_path / "f.meta.json").read_text())["provenance"]
    assert p["git_commit"] is None and p["git_dirty"] is None


# --- event-log header ------------------------------------------------------------------------


def test_header_round_trip_and_headerless_logs_still_read(tmp_path: Path):
    p = make_provenance()
    log = tmp_path / "a.jsonl"
    with EventLog(log, header=p) as events:
        assert header_line(p) in log.read_text()
        assert events.path == log
    assert read_header(log) == p and read_events(log) == []

    legacy = tmp_path / "legacy.jsonl"
    with EventLog(legacy):
        pass
    assert read_header(legacy) is None and read_events(legacy) == []


def test_a_newer_header_version_is_refused(tmp_path: Path):
    obj = json.loads(header_line(make_provenance()))
    obj["header_version"] = 2
    log = tmp_path / "a.jsonl"
    log.write_text(json.dumps(obj) + "\n")
    with pytest.raises(ValueError, match="header version"):
        read_header(log)


def test_an_event_that_mentions_run_header_is_not_a_header():
    assert not is_header('{"schema_version":1,"result_summary":"saw run_header in a file"}')
    assert not is_header("run_header but not json")
