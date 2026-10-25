"""Archive creation and download."""

from flask import Blueprint, abort, jsonify, request, send_file

from ..services import shell, storage

bp = Blueprint("archive", __name__)


@bp.post("/archives")
def create():
    name = request.get_json(force=True)["name"]
    archive = shell.make_archive(name)
    return jsonify(archive=archive.name), 201


@bp.get("/archives/download")
def fetch():
    try:
        path = storage.export_path(request.args.get("name", ""))
    except ValueError:
        abort(400)
    if not path.is_file():
        abort(404)
    response = send_file(path)
    response.headers["X-Checksum"] = shell.checksum(path)
    return response
