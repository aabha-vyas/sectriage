"""In-memory notes and comments, plus resolution of uploaded and exported files."""

from pathlib import Path

from . import settings

NOTES = {1: {"id": 1, "title": "Quarterly planning", "owner": "ana"}}
COMMENTS: dict[int, list[dict]] = {1: []}


def add_comment(note_id: int, author: str, body: str) -> None:
    COMMENTS.setdefault(note_id, []).append({"author": author, "body": body})


def comments_for(note_id: int) -> list[dict]:
    return list(COMMENTS.get(note_id, []))


def resolve_upload(name: str) -> Path:
    base = settings.UPLOAD_DIR.resolve()
    target = (base / name).resolve()
    if not str(target).startswith(str(base)):
        raise PermissionError(name)
    return target


def resolve_export(name: str) -> Path:
    base = settings.EXPORT_DIR.resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base):
        raise PermissionError(name)
    return target
