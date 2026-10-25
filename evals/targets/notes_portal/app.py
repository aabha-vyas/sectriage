"""Shared notes portal."""

from flask import Flask

from .views import comments, errors, files, profile


def create_app() -> Flask:
    app = Flask(__name__)
    for module in (comments, files, profile):
        app.register_blueprint(module.bp)
    errors.register(app)
    return app


app = create_app()
