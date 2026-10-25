"""Settings for the static site builder."""

import os
from pathlib import Path

ROOT = Path(__file__).parent
CONTENT_DIR = ROOT / "content"
OUTPUT_DIR = ROOT / "public"
CACHE_DIR = ROOT / ".cache"

DB_PATH = ":memory:"
PAGES_TABLE = "pages"
TOOLS = {"minify": "/usr/bin/true", "optimize": "/usr/bin/true"}

DEPLOY_TOKEN = os.environ.get("BUILDER_DEPLOY_TOKEN")
DEPLOY_PASSWORD = os.environ.get("BUILDER_DEPLOY_PASSWORD")
