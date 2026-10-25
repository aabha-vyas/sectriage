"""On-disk cache of rendered fragments, keyed by content hash."""

import hashlib
import sqlite3
from pathlib import Path

from . import config


def cache_file(key: str) -> Path:
    return config.CACHE_DIR / hashlib.sha256(key.encode()).hexdigest()


def store(key: str, text: str) -> Path:
    path = cache_file(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def load(key: str) -> str | None:
    path = cache_file(key)
    return path.read_text(encoding="utf-8") if path.is_file() else None


def record_build(conn: sqlite3.Connection, slug: str, digest: str) -> None:
    conn.execute("INSERT INTO builds (slug, digest) VALUES (?, ?)", (slug, digest))
