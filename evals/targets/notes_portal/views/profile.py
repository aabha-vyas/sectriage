"""Profile pages and notification settings."""

from flask import Blueprint, jsonify, render_template, request
from markupsafe import escape

from ..integrations import mailer

bp = Blueprint("profile", __name__)


@bp.get("/profile/<name>")
def show(name):
    return render_template("profile.html", name=name, bio=request.args.get("bio", ""))


@bp.get("/greeting")
def greeting():
    name = request.args.get("name", "guest")
    return f"<p>Hello {escape(name)}</p>"


@bp.post("/profile/notify")
def notify():
    email = request.form.get("email", "")
    domain = email.rpartition("@")[2]
    if not mailer.domain_resolves(domain):
        return jsonify(sent=False, reason="unknown domain"), 422
    mailer.send_test_message(email)
    return jsonify(sent=True)
