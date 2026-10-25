"""Account registration."""

from flask import Blueprint, jsonify, request

from ..services import db

bp = Blueprint("users", __name__)


@bp.post("/users")
def register():
    payload = request.get_json(force=True)
    user_id = db.add_user(payload["username"], payload["email"])
    return jsonify(id=user_id), 201
