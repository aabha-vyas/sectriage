"""Helpers that call external programs."""

import subprocess
from pathlib import Path

from .. import config


def make_archive(name: str) -> Path:
    target = config.ARCHIVE_DIR / f"{name}.tgz"
    cmd = f"tar czf {target} -C {config.ATTACHMENTS_DIR} ."
    subprocess.run(cmd, shell=True, check=True)
    return target


def checksum(path: Path) -> str:
    result = subprocess.run(
        ["sha256sum", "--", str(path)],
        shell=False,
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.split()[0]
