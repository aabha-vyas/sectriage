"""Invoice attachment downloads."""

from flask import Blueprint, abort, request, send_file

from ..services import storage

bp = Blueprint("attachments", __name__)


@bp.get("/invoices/<int:invoice_id>/attachment")
def download(invoice_id):
    name = request.args.get("name", "")
    path = storage.attachment_path(invoice_id, name)
    if not path.is_file():
        abort(404)
    return send_file(path)
