"""Payment provider callbacks."""

import hashlib
import hmac

from flask import Blueprint, abort, jsonify, request

from .. import config

bp = Blueprint("webhooks", __name__)


@bp.post("/webhooks/payments")
def payments():
    signature = request.headers.get("X-Signature", "")
    key = config.WEBHOOK_SIGNING_KEY.encode()
    expected = hmac.new(key, request.get_data(), hashlib.sha256).hexdigest()
    if not hmac.compare_digest(signature, expected):
        abort(401)
    return jsonify(accepted=True)
