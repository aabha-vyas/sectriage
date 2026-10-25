"""Note comments."""

from flask import Blueprint, abort, redirect, render_template, request, url_for

from .. import storage

bp = Blueprint("comments", __name__)


@bp.get("/notes/<int:note_id>/comments")
def show(note_id):
    note = storage.NOTES.get(note_id)
    if note is None:
        abort(404)
    comments = storage.comments_for(note_id)
    return render_template("comments.html", note=note, comments=comments)


@bp.post("/notes/<int:note_id>/comments")
def add(note_id):
    author = request.form.get("author", "anonymous")
    body = request.form.get("body", "")
    storage.add_comment(note_id, author, body)
    return redirect(url_for("comments.show", note_id=note_id))
