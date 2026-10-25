"""Uploaded files and exports."""

import hmac

from flask import Blueprint, abort, jsonify, request, send_file

from .. import settings, storage
from . import errors

bp = Blueprint("files", __name__)


@bp.get("/uploads/download")
def download_upload():
    name = request.args.get("name", "")
    try:
        path = storage.resolve_upload(name)
    except PermissionError:
        return errors.render_error(403, f"Access to {name} is not allowed")
    if not path.is_file():
        return errors.render_error(404, f"No file named {name}")
    return send_file(path)


@bp.get("/exports/download")
def download_export():
    name = request.args.get("name", "")
    try:
        path = storage.resolve_export(name)
    except PermissionError:
        return errors.render_error(403, "Access denied")
    if not path.is_file():
        return errors.render_error(404, "Export not found")
    return send_file(path)


@bp.get("/uploads")
def list_uploads():
    token = request.headers.get("X-Admin-Token", "")
    expected = settings.ADMIN_TOKEN
    if not expected or not hmac.compare_digest(token.encode(), expected.encode()):
        abort(403)
    return jsonify(files=sorted(p.name for p in settings.UPLOAD_DIR.iterdir()))
