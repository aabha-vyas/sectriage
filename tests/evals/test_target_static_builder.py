"""Behavior of static_builder: it has no ground-truth findings; every risky-looking pattern
is shown safe here. Decoy ids (see evals/ground_truth/static_builder.decoys.json) appear in test
names as `dec_*`."""

import ast
import sqlite3
import subprocess
from pathlib import Path

import pytest

from evals.targets.static_builder import build, cache, config, plugins, render

QUOTE = "x' OR '1'='1"
SCRIPT = "<script>alert(1)</script>"


@pytest.fixture
def site(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "OUTPUT_DIR", tmp_path / "public")
    monkeypatch.setattr(config, "CACHE_DIR", tmp_path / "cache")
    db = tmp_path / "pages.db"
    conn = sqlite3.connect(db)
    conn.execute("CREATE TABLE pages (slug TEXT, body TEXT)")
    conn.execute("INSERT INTO pages VALUES ('home', 'hello\n\nworld')")
    conn.execute("INSERT INTO pages VALUES ('x', 'other page')")
    conn.execute("CREATE TABLE builds (slug TEXT, digest TEXT)")
    conn.commit()
    conn.close()
    monkeypatch.setattr(config, "DB_PATH", str(db))
    return tmp_path


@pytest.fixture
def calls(monkeypatch):
    seen: list[tuple] = []

    def fake_run(argv, **kwargs):
        seen.append((argv, kwargs.get("shell"), kwargs.get("env")))
        return subprocess.CompletedProcess(argv, 0)

    monkeypatch.setattr(subprocess, "run", fake_run)
    return seen


def test_build_page_writes_escaped_html(site):
    path = build.build_page("home", "Home <1>", transform="upper")
    assert path == (site / "public" / "home.html").resolve()
    assert (
        path.read_text() == "<!doctype html><title>Home &lt;1&gt;</title><p>HELLO</p><p>WORLD</p>"
    )


def test_dec_output_path_stays_inside_output_dir(site):
    for rel in ("../escape.html", "a/../../escape.html", str(site / "escape.html")):
        with pytest.raises(ValueError):
            build.output_path(rel)
        with pytest.raises(ValueError):
            build.build_page(rel, "t")
    assert build.output_path("ok/page.html").is_relative_to(config.OUTPUT_DIR.resolve())
    assert not (site / "escape.html").exists()


def test_dec_asset_tool_is_allowlisted_and_runs_without_a_shell(site, calls):
    build.run_user_command("minify", ["a.css", "sub/b.css"])
    [(argv, shell, _)] = calls
    out = config.OUTPUT_DIR.resolve()
    assert shell is False
    assert argv == [config.TOOLS["minify"], str(out / "a.css"), str(out / "sub/b.css")]


def test_dec_asset_tool_rejects_unknown_names_and_escaping_args(site, calls):
    with pytest.raises(KeyError):
        build.run_user_command("minify; id", [])
    with pytest.raises(ValueError):
        build.run_user_command("minify", ["../../etc/passwd"])
    with pytest.raises(ValueError):
        build.run_user_command("minify", ["--output=/tmp/x", "/etc/passwd"])
    assert calls == []


def test_dec_page_lookup_binds_the_slug(site):
    assert build.find_page(QUOTE) is None
    assert build.find_page("x") == "other page"


def test_dec_page_count_interpolates_a_constant_table_name(site):
    tree = ast.parse(Path(config.__file__).read_text())
    assign = next(
        n for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == "PAGES_TABLE"
    )
    assert isinstance(assign.value, ast.Constant) and assign.value.value == "pages"
    fn = next(
        n
        for n in ast.parse(Path(build.__file__).read_text()).body
        if getattr(n, "name", "") == "count_pages"
    )
    assert [a.arg for a in fn.args.args] == ["conn"]  # nothing else flows in
    with sqlite3.connect(config.DB_PATH) as conn:
        assert build.count_pages(conn) == 2


def test_dec_render_escapes_title_and_body():
    out = render.page(SCRIPT, f'{SCRIPT}\n\n"quoted" & more')
    assert SCRIPT not in out
    assert "&lt;script&gt;" in out and "&quot;quoted&quot; &amp; more" in out


def test_dec_deploy_token_comes_from_the_environment_and_unset_denies(monkeypatch):
    tree = ast.parse(Path(config.__file__).read_text())
    for name in ("DEPLOY_TOKEN", "DEPLOY_PASSWORD"):
        call = next(
            n.value for n in tree.body if isinstance(n, ast.Assign) and n.targets[0].id == name
        )
        assert isinstance(call, ast.Call) and len(call.args) == 1 and not call.keywords
    monkeypatch.setattr(config, "DEPLOY_TOKEN", None)
    assert build.check_token("") is False and build.check_token("anything") is False
    monkeypatch.setattr(config, "DEPLOY_TOKEN", "t0ken")
    assert build.check_token("t0ken") is True and build.check_token("T0KEN") is False


def test_dec_deploy_password_must_be_configured(monkeypatch):
    monkeypatch.setattr(config, "DEPLOY_PASSWORD", None)
    with pytest.raises(RuntimeError, match="not set"):
        build.deploy_password()
    monkeypatch.setattr(config, "DEPLOY_PASSWORD", "pw")
    assert build.deploy_password() == "pw"


@pytest.mark.parametrize("host", ["-oProxyCommand=id", "a;b.test", "a b", "", "h.test\nid", "h:22"])
def test_dec_publish_rejects_odd_hosts_before_running_anything(host, calls, monkeypatch):
    monkeypatch.setattr(config, "DEPLOY_PASSWORD", "pw")
    with pytest.raises(ValueError):
        build.publish(host)
    assert calls == []


def test_dec_publish_runs_rsync_with_an_argument_vector(calls, monkeypatch):
    monkeypatch.setattr(config, "DEPLOY_PASSWORD", "pw")
    build.publish("host.example.test")
    [(argv, shell, env)] = calls
    assert shell is False
    assert argv[:4] == ["rsync", "-a", "--", f"{config.OUTPUT_DIR}/"]
    assert argv[4] == "deploy@host.example.test:/srv/site/"
    assert env["RSYNC_PASSWORD"] == "pw"


def test_dec_plugin_names_are_dictionary_lookups():
    assert plugins.apply("upper", "ab") == "AB"
    for name in ("__import__('os').system('id')", "open", "eval", ""):
        with pytest.raises(KeyError):
            plugins.apply(name, "x")


def test_dec_cache_keys_are_hashed_so_they_cannot_name_paths(site):
    for key in ("../../etc/passwd", "/etc/passwd", "a/b", QUOTE):
        path = cache.cache_file(key)
        assert path.parent == config.CACHE_DIR
        assert path.name.isalnum() and len(path.name) == 64
    cache.store("../../etc/passwd", "body")
    assert cache.load("../../etc/passwd") == "body"
    assert cache.load("missing") is None


def test_dec_build_record_binds_values(site):
    with sqlite3.connect(config.DB_PATH) as conn:
        cache.record_build(conn, QUOTE, "d'); DROP TABLE builds; --")
        rows = conn.execute("SELECT slug, digest FROM builds").fetchall()
    assert rows == [(QUOTE, "d'); DROP TABLE builds; --")]
