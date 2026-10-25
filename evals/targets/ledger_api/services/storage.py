"""Locations of files that belong to invoices and archives."""

from pathlib import Path

from .. import config


def attachment_path(invoice_id: int, name: str) -> Path:
    return config.ATTACHMENTS_DIR / str(invoice_id) / name


def export_path(name: str) -> Path:
    base = config.ARCHIVE_DIR.resolve()
    target = (base / name).resolve()
    if not target.is_relative_to(base):
        raise ValueError("path is outside the archive directory")
    return target
