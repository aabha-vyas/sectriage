"""Invoice listing and search."""

from flask import Blueprint, jsonify, request

from ..services import db, queries

bp = Blueprint("invoices", __name__)


@bp.get("/invoices")
def list_all():
    sort = request.args.get("sort", "id")
    status = request.args.get("status")
    return jsonify(invoices=db.list_invoices(sort, status))


@bp.get("/invoices/search")
def search():
    term = request.args.get("q", "")
    return jsonify(invoices=db.search_invoices(term))


@bp.get("/customers/<name>/invoices")
def for_customer(name):
    try:
        clause = queries.checked_order_clause(request.args.get("sort", "id"))
    except ValueError:
        return jsonify(error="unsupported sort column"), 400
    return jsonify(invoices=db.list_for_customer(name, clause))
