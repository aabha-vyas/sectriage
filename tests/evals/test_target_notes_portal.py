"""Behavior of notes_portal: each ground-truth bug is real, each decoy is not.

Decoy ids (see evals/ground_truth/notes_portal.decoys.json) appear in test names as `dec_*`.
"""

import ast
import importlib
import subprocess
from pathlib import Path

import pytest

pytest.importorskip("flask")

from evals.targets.notes_portal import settings, storage  # noqa: E402
from evals.targets.notes_portal.app import create_app  # noqa: E402
from evals.targets.notes_portal.integrations import mailer  # noqa: E402

IMG = "<img src=x onerror=alert(1)>"
SCRIPT = "<script>alert(1)</script>"


@pytest.fixture
def client():
    storage.COMMENTS[1] = []
    return create_app().test_client()


@pytest.fixture
def dirs(tmp_path, monkeypatch):
    """uploads/ and exports/ each with a sibling whose name starts with the same text."""
    for name in ("uploads", "uploads_extra", "exports", "exports_extra"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "f.txt").write_text(name)
    (tmp_path / "outside.txt").write_text("outside")
    monkeypatch.setattr(settings, "UPLOAD_DIR", tmp_path / "uploads")
    monkeypatch.setattr(settings, "EXPORT_DIR", tmp_path / "exports")
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []

    def fake_run(cmd, **kwargs):
        seen.append((cmd, kwargs.get("shell")))
        return subprocess.CompletedProcess(cmd, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def text(response) -> str:
    out = response.get_data(as_text=True)
    response.close()
    return out


# --- bugs -------------------------------------------------------------------------------------


def test_bug_stored_comment_body_renders_unescaped(client):
    client.post("/notes/1/comments", data={"author": "<b>ann</b>", "body": SCRIPT})
    page = text(client.get("/notes/1/comments"))
    assert SCRIPT in page  # body is emitted as markup
    assert "<b>ann</b>" not in page and "&lt;b&gt;ann&lt;/b&gt;" in page  # author is escaped


def test_bug_prefix_check_admits_a_sibling_directory(client, dirs):
    r = client.get("/uploads/download", query_string={"name": "../uploads_extra/f.txt"})
    assert r.status_code == 200 and text(r) == "uploads_extra"
    r = client.get("/uploads/download", query_string={"name": "../outside.txt"})
    assert r.status_code == 403  # a path with no shared prefix is refused


def test_bug_mail_key_falls_back_to_a_literal(monkeypatch):
    tree = ast.parse(Path(settings.__file__).read_text())
    call = next(
        n.value
        for n in tree.body
        if isinstance(n, ast.Assign) and n.targets[0].id == "MAIL_API_KEY"
    )
    literal = call.args[1].value
    monkeypatch.delenv("MAIL_API_KEY", raising=False)
    try:
        assert importlib.reload(settings).MAIL_API_KEY == literal
    finally:
        monkeypatch.undo()
        importlib.reload(settings)


def test_bug_recipient_local_part_reaches_a_shell_command(client, calls):
    r = client.post("/profile/notify", data={"email": "a;id@example.test"})
    assert r.status_code == 200
    assert ["nslookup", "example.test"] in [c for c, _ in calls]  # domain check passed
    shell_calls = [(c, s) for c, s in calls if s is True]
    assert len(shell_calls) == 1 and "a;id@example.test" in shell_calls[0][0]


def test_bug_error_page_reflects_the_requested_name_raw(client, dirs):
    r = client.get("/uploads/download", query_string={"name": IMG})
    assert r.status_code == 404 and IMG in text(r)
    r = client.get("/uploads/download", query_string={"name": "../" + IMG})
    assert r.status_code == 403 and IMG in text(r)


# --- decoys: each is safe ----------------------------------------------------------------------


def test_dec_profile_template_escapes_name_and_bio(client):
    page = text(client.get("/profile/" + IMG, query_string={"bio": SCRIPT}))
    assert IMG not in page and SCRIPT not in page
    assert "&lt;script&gt;" in page and "&lt;img" in page


def test_dec_greeting_escapes_the_name(client):
    page = text(client.get("/greeting", query_string={"name": SCRIPT}))
    assert SCRIPT not in page and "&lt;script&gt;" in page


def test_dec_not_found_page_escapes_the_path(client):
    r = client.get("/" + IMG)
    assert r.status_code == 404
    page = text(r)
    assert IMG not in page and "&lt;img" in page


def test_dec_export_path_check_refuses_siblings_and_escapes(client, dirs):
    for name in ("../exports_extra/f.txt", "../outside.txt", str(dirs / "outside.txt")):
        assert client.get("/exports/download", query_string={"name": name}).status_code == 403
    r = client.get("/exports/download", query_string={"name": "f.txt"})
    assert r.status_code == 200 and text(r) == "exports"


def test_dec_export_error_pages_carry_no_request_data(client, dirs):
    r = client.get("/exports/download", query_string={"name": IMG})
    assert r.status_code == 404 and IMG not in text(r)
    r = client.get("/exports/download", query_string={"name": "../" + IMG})
    assert r.status_code == 403 and IMG not in text(r)


def test_dec_admin_token_comes_from_the_environment_and_unset_denies(client, dirs, monkeypatch):
    tree = ast.parse(Path(settings.__file__).read_text())
    call = next(
        n.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "ADMIN_TOKEN"
    )
    assert len(call.args) == 1 and not call.keywords  # no fallback value
    monkeypatch.setattr(settings, "ADMIN_TOKEN", None)
    assert client.get("/uploads").status_code == 403
    assert client.get("/uploads", headers={"X-Admin-Token": ""}).status_code == 403
    monkeypatch.setattr(settings, "ADMIN_TOKEN", "correct-token")
    assert client.get("/uploads", headers={"X-Admin-Token": "wrong"}).status_code == 403
    r = client.get("/uploads", headers={"X-Admin-Token": "correct-token"})
    assert r.status_code == 200 and "f.txt" in r.json["files"]


@pytest.mark.parametrize("domain", ["-x.test", "a;b.test", "a b.test", "", "ex.test\nid", "A.TEST"])
def test_dec_domain_check_rejects_odd_domains_before_running_anything(domain, calls):
    with pytest.raises(ValueError):
        mailer.domain_resolves(domain)
    assert calls == []


def test_dec_domain_check_runs_without_a_shell(calls):
    assert mailer.domain_resolves("example.test") is True
    assert calls == [(["nslookup", "example.test"], False)]
