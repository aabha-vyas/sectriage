"""Settings for the ledger service."""

import os
from pathlib import Path

BASE_DIR = Path(__file__).parent
ATTACHMENTS_DIR = BASE_DIR / "attachments"
ARCHIVE_DIR = BASE_DIR / "archives"

SESSION_KEY = os.environ.get("LEDGER_SESSION_KEY")
WEBHOOK_SIGNING_KEY = "wh_Qm4Zr7xT2vBn9LkD5sHc8YfJ3aWe6UpG"
