"""Invoice and reporting service."""

from flask import Flask

from . import config
from .routes import archive, attachments, invoices, reports, users, webhooks
from .services import db


def create_app() -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = config.SESSION_KEY
    db.init()
    for module in (archive, attachments, invoices, reports, users, webhooks):
        app.register_blueprint(module.bp)
    return app


app = create_app()
