"""Behavior of ledger_api: each ground-truth bug is real, each decoy is not.

Decoy ids (see evals/ground_truth/ledger_api.decoys.json) appear in test names as `dec_*`.
"""

import ast
import hashlib
import hmac
import importlib
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from evals.targets.ledger_api import config  # noqa: E402
from evals.targets.ledger_api.app import create_app  # noqa: E402
from evals.targets.ledger_api.services import db  # noqa: E402

PAYLOAD_ORDER = "CASE WHEN customer = 'globex' THEN 0 ELSE 1 END"
PAYLOAD_QUOTE = "x' OR '1'='1"


@pytest.fixture
def client():
    return create_app().test_client()


@pytest.fixture
def sql_log(monkeypatch):
    seen: list[str] = []
    original = db.query_all

    def spy(sql, params=()):
        seen.append(sql)
        return original(sql, params)

    monkeypatch.setattr(db, "query_all", spy)
    return seen


def ids(response):
    return [row["id"] for row in response.json["invoices"]]


# --- bugs -------------------------------------------------------------------------------------


def test_bug_sqli_order_clause_takes_an_expression(client, sql_log):
    assert ids(client.get("/invoices")) == [1, 2, 3, 4]
    r = client.get("/invoices", query_string={"sort": PAYLOAD_ORDER})
    assert ids(r) == [3, 1, 2, 4]  # the supplied expression decided the order
    assert any(PAYLOAD_ORDER in sql for sql in sql_log)


def test_bug_sqli_second_order_stored_name_changes_a_later_query(client):
    before = client.get("/reports/customer/1").json
    assert [i["id"] for i in before["invoices"]] == [1, 2]
    r = client.post("/users", json={"username": PAYLOAD_QUOTE, "email": "e@example.test"})
    assert r.status_code == 201  # the insert itself is parameterized and stores it verbatim
    assert db.get_user(r.json["id"])["username"] == PAYLOAD_QUOTE
    after = client.get(f"/reports/customer/{r.json['id']}").json
    assert len(after["invoices"]) == 4  # every row came back


def test_bug_command_line_is_built_from_the_name_and_run_in_a_shell(client, monkeypatch):
    seen = {}

    def fake_run(cmd, **kwargs):
        seen["cmd"], seen["shell"] = cmd, kwargs.get("shell")
        return subprocess.CompletedProcess(cmd, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    client.post("/archives", json={"name": "x; id"})
    assert seen["shell"] is True and isinstance(seen["cmd"], str) and "x; id" in seen["cmd"]


@pytest.mark.parametrize("name", ["../../outside", "/tmp/outside", "a/b/outside", "../outside"])
def test_archive_name_is_reduced_to_its_last_component(name, monkeypatch):
    from evals.targets.ledger_api.services import shell

    monkeypatch.setattr(subprocess, "run", lambda *a, **k: subprocess.CompletedProcess(a, 0))
    target = shell.make_archive(name)
    assert target == config.ARCHIVE_DIR / "outside.tgz"
    assert target.resolve().is_relative_to(config.ARCHIVE_DIR.resolve())


def test_bug_attachment_name_can_leave_the_attachments_directory(client, tmp_path, monkeypatch):
    (tmp_path / "attachments" / "1").mkdir(parents=True)
    (tmp_path / "outside.txt").write_text("outside")
    monkeypatch.setattr(config, "ATTACHMENTS_DIR", tmp_path / "attachments")
    r = client.get("/invoices/1/attachment", query_string={"name": "../../outside.txt"})
    assert r.status_code == 200 and r.get_data(as_text=True) == "outside"
    r.close()


def test_bug_signing_key_is_a_source_literal_that_forges_requests(client):
    tree = ast.parse(Path(config.__file__).read_text())
    literals = {
        n.targets[0].id: n.value.value
        for n in tree.body
        if isinstance(n, ast.Assign) and isinstance(n.value, ast.Constant)
    }
    body = b'{"event": "paid"}'
    signature = hmac.new(literals["WEBHOOK_SIGNING_KEY"].encode(), body, hashlib.sha256).hexdigest()
    r = client.post("/webhooks/payments", data=body, headers={"X-Signature": signature})
    assert r.status_code == 200
    assert client.post("/webhooks/payments", data=body).status_code == 401


# --- decoys: each is safe ----------------------------------------------------------------------


def test_dec_like_search_binds_the_term(client, sql_log):
    r = client.get("/invoices/search", query_string={"q": PAYLOAD_QUOTE})
    assert ids(r) == []
    assert all(PAYLOAD_QUOTE not in sql for sql in sql_log)
    assert ids(client.get("/invoices/search", query_string={"q": "acme"})) == [1, 2]


def test_dec_status_filter_binds_the_value(client, sql_log):
    r = client.get("/invoices", query_string={"status": PAYLOAD_QUOTE})
    assert ids(r) == []
    assert all(PAYLOAD_QUOTE not in sql for sql in sql_log)
    assert ids(client.get("/invoices", query_string={"status": "paid"})) == [1, 4]


def test_dec_checked_sort_rejects_an_expression(client, sql_log):
    r = client.get("/customers/acme/invoices", query_string={"sort": PAYLOAD_ORDER})
    assert r.status_code == 400
    assert all(PAYLOAD_ORDER not in sql for sql in sql_log)
    r = client.get("/customers/acme/invoices", query_string={"sort": "amount_cents"})
    assert r.status_code == 200 and ids(r) == [2, 1]


def test_dec_clause_concat_is_safe_only_because_the_caller_checks(client):
    root = Path(db.__file__).parents[1]
    calls = []
    for path in root.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text())):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "list_for_customer"
            ):
                calls.append(path.name)
    assert calls == ["invoices.py"]  # one caller, and it validates (next assertion)
    r = client.get("/customers/acme/invoices", query_string={"sort": PAYLOAD_ORDER})
    assert r.status_code == 400
    # the helper itself does no checking: this is why the caller's check matters
    rows = db.list_for_customer("acme", f" ORDER BY {PAYLOAD_ORDER}")
    assert [row["id"] for row in rows] == [1, 2]


def test_dec_customer_name_in_path_is_bound(client, sql_log):
    r = client.get("/customers/" + PAYLOAD_QUOTE.replace("/", "") + "/invoices")
    assert ids(r) == []


def test_dec_user_insert_binds_values(client):
    hostile = "x'); DROP TABLE users; --"
    r = client.post("/users", json={"username": hostile, "email": "a@example.test"})
    assert r.status_code == 201
    assert db.get_user(r.json["id"])["username"] == hostile
    assert len(db.query_all("SELECT id FROM users")) == 3


def test_dec_checksum_runs_without_a_shell_and_after_option_terminator(monkeypatch):
    from evals.targets.ledger_api.services import shell

    seen = {}

    def fake_run(argv, **kwargs):
        seen["argv"], seen["shell"] = argv, kwargs.get("shell")
        return subprocess.CompletedProcess(argv, 0, stdout="abc  file\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert shell.checksum(Path("; id")) == "abc"
    assert seen == {"argv": ["sha256sum", "--", "; id"], "shell": False}


def test_dec_export_path_stays_inside_archive_dir(client, tmp_path, monkeypatch):
    archive = tmp_path / "archives"
    archive.mkdir()
    (archive / "ok.txt").write_text("fine")
    (tmp_path / "archives_extra").mkdir()
    (tmp_path / "archives_extra" / "other.txt").write_text("other")
    (tmp_path / "outside.txt").write_text("outside")
    monkeypatch.setattr(config, "ARCHIVE_DIR", archive)
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kw: subprocess.CompletedProcess(argv, 0, stdout="abc  f\n"),
    )
    for name in ("../outside.txt", "../archives_extra/other.txt", str(tmp_path / "outside.txt")):
        assert client.get("/archives/download", query_string={"name": name}).status_code == 400
    r = client.get("/archives/download", query_string={"name": "ok.txt"})
    assert r.status_code == 200 and r.headers["X-Checksum"] == "abc"
    r.close()


def test_dec_session_key_comes_from_the_environment(monkeypatch):
    tree = ast.parse(Path(config.__file__).read_text())
    assign = next(
        n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "SESSION_KEY"
    )
    assert not isinstance(assign.value, ast.Constant)
    monkeypatch.delenv("LEDGER_SESSION_KEY", raising=False)
    try:
        assert importlib.reload(config).SESSION_KEY is None
        monkeypatch.setenv("LEDGER_SESSION_KEY", "from-env")
        assert importlib.reload(config).SESSION_KEY == "from-env"
    finally:
        monkeypatch.undo()
        importlib.reload(config)
