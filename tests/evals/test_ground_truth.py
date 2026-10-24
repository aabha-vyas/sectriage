import subprocess
from pathlib import Path

import pytest

from evals.scorer import load_ground_truth

ROOT = Path(__file__).resolve().parents[2]
TRUTH = load_ground_truth("vuln_app")


def test_five_entries_with_unique_ids_and_classes():
    assert len(TRUTH.findings) == 5
    assert len({t.id for t in TRUTH.findings}) == 5
    assert len({t.vuln_class for t in TRUTH.findings}) == 5


def test_snippets_sit_inside_their_ranges():
    for t in TRUTH.findings:
        lines = (ROOT / t.file).read_text().splitlines()
        block = "\n".join(lines[t.line_start - 1 : t.line_end])
        assert t.snippet in block, t.id


def test_ranges_do_not_overlap():
    spans = sorted((t.line_start, t.line_end) for t in TRUTH.findings)
    for (_, prev_end), (next_start, _) in zip(spans, spans[1:], strict=False):
        assert prev_end < next_start


@pytest.fixture
def client():
    pytest.importorskip("flask")
    from evals.targets.vuln_app.app import app

    return app.test_client()


def test_sqli_returns_every_row(client):
    assert len(client.get("/users", query_string={"name": "x' OR '1'='1"}).json["users"]) == 2
    assert len(client.get("/users/by-id", query_string={"id": "1 OR 1=1"}).json["users"]) == 0


def test_xss_reflects_raw_but_safe_route_escapes(client):
    payload = "<script>alert(1)</script>"
    assert payload in client.get("/greet", query_string={"name": payload}).get_data(as_text=True)
    safe = client.get("/greet-safe", query_string={"name": payload}).get_data(as_text=True)
    assert payload not in safe


def test_path_traversal_escapes_docs_dir(client):
    from evals.targets.vuln_app import app as module

    assert module.DOCS_DIR.name == "docs"
    r = client.get("/docs/download", query_string={"file": "../app.py"})
    assert r.status_code == 200
    r.close()


def test_command_injection_builds_shell_string(client, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["shell"] = cmd, kwargs.get("shell")
        return subprocess.CompletedProcess(cmd, 0, stdout="", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    client.get("/ping", query_string={"host": "x; id"})
    assert seen == {"cmd": "ping -c 1 x; id", "shell": True}
