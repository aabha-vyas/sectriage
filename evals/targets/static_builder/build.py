"""Builds the site: renders content pages, runs asset tools and publishes the output."""

import hmac
import os
import re
import sqlite3
import subprocess
from pathlib import Path

from . import cache, config, plugins, render

_HOST_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")


def output_path(relative: str) -> Path:
    base = config.OUTPUT_DIR.resolve()
    target = (base / relative).resolve()
    if not target.is_relative_to(base):
        raise ValueError(f"{relative!r} escapes the output directory")
    return target


def run_user_command(name: str, args: list[str]) -> int:
    """Run one of the configured asset tools on files inside the output directory."""
    tool = config.TOOLS.get(name)
    if tool is None:
        raise KeyError(name)
    files = [str(output_path(arg)) for arg in args]
    return subprocess.run([tool, *files], shell=False, check=False).returncode


def find_page(slug: str) -> str | None:
    with sqlite3.connect(config.DB_PATH) as conn:
        row = conn.execute("SELECT body FROM pages WHERE slug = ?", (slug,)).fetchone()
    return row[0] if row else None


def count_pages(conn: sqlite3.Connection) -> int:
    return conn.execute(f"SELECT COUNT(*) FROM {config.PAGES_TABLE}").fetchone()[0]


def build_page(slug: str, title: str, transform: str | None = None) -> Path:
    body = find_page(slug) or ""
    if transform:
        body = plugins.apply(transform, body)
    target = output_path(f"{slug}.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(render.page(title, body), encoding="utf-8")
    cache.store(slug, body)
    return target


def check_token(candidate: str) -> bool:
    expected = config.DEPLOY_TOKEN
    if not expected:
        return False
    return hmac.compare_digest(candidate.encode(), expected.encode())


def deploy_password() -> str:
    password = config.DEPLOY_PASSWORD
    if not password:
        raise RuntimeError("BUILDER_DEPLOY_PASSWORD is not set")
    return password


def publish(host: str) -> int:
    if not _HOST_RE.fullmatch(host):
        raise ValueError(f"invalid host {host!r}")
    source = f"{config.OUTPUT_DIR}/"
    argv = ["rsync", "-a", "--", source, f"deploy@{host}:/srv/site/"]
    env = {"RSYNC_PASSWORD": deploy_password(), "PATH": os.environ.get("PATH", "")}
    return subprocess.run(argv, shell=False, check=False, env=env).returncode
