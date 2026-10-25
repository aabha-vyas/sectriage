"""HTML rendering for pages."""

import html


def page(title: str, body: str) -> str:
    paragraphs = "".join(
        f"<p>{html.escape(chunk, quote=True)}</p>" for chunk in body.split("\n\n") if chunk
    )
    return f"<!doctype html><title>{html.escape(title, quote=True)}</title>{paragraphs}"
