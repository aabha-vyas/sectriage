"""Shared error pages."""

from flask import Flask, Response, request
from markupsafe import escape


def render_error(status: int, message: str) -> Response:
    page = f"<!doctype html><title>Error {status}</title><h1>Error {status}</h1><p>{message}</p>"
    return Response(page, status=status, mimetype="text/html")


def register(app: Flask) -> None:
    @app.errorhandler(404)
    def not_found(_exc):
        return render_error(404, f"No page at {escape(request.path)}")
