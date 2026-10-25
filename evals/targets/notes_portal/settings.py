"""Portal settings."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
UPLOAD_DIR = BASE_DIR / "uploads"
EXPORT_DIR = BASE_DIR / "exports"
MESSAGE_TEMPLATE = BASE_DIR / "templates" / "test_message.txt"

SENDER = "portal@example.test"
MAIL_API_KEY = os.environ.get("MAIL_API_KEY", "mp_Zr8Qk1VnT5yXb3LdG7sHw2CfJ9aEu4Po")
ADMIN_TOKEN = os.environ.get("PORTAL_ADMIN_TOKEN")
