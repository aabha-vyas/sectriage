"""Per-customer reporting."""

from flask import Blueprint, abort, jsonify

from ..services import db

bp = Blueprint("reports", __name__)


@bp.get("/reports/customer/<int:user_id>")
def customer_report(user_id):
    user = db.get_user(user_id)
    if user is None:
        abort(404)
    sql = f"SELECT id, amount_cents, status FROM invoices WHERE customer = '{user['username']}'"
    rows = db.query_all(sql)
    total = sum(row["amount_cents"] for row in rows)
    return jsonify(customer=user["username"], total_cents=total, invoices=rows)
