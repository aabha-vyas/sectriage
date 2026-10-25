"""Outgoing mail through the system mailer."""

import os
import re
import subprocess

from .. import settings

_DOMAIN_RE = re.compile(r"[a-z0-9][a-z0-9.-]{0,252}")


def domain_resolves(domain: str) -> bool:
    if not _DOMAIN_RE.fullmatch(domain):
        raise ValueError("invalid domain")
    result = subprocess.run(["nslookup", domain], shell=False, capture_output=True, check=False)
    return result.returncode == 0


def send_test_message(recipient: str) -> None:
    cmd = f"mail -s 'Portal notification' {recipient} < {settings.MESSAGE_TEMPLATE}"
    env = {"MAIL_API_KEY": settings.MAIL_API_KEY, "PATH": os.environ.get("PATH", "")}
    subprocess.run(cmd, shell=True, check=False, env=env)
