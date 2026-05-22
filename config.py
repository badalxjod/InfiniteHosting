"""
config.py — Configuration loader.

Reads all settings from environment variables only.
Crashes immediately with a clear error if required vars are missing.
"""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

# Load .env file if present (dev convenience; production should set env vars directly)
load_dotenv()

# ── Required ────────────────────────────────────────────────
BOT_TOKEN = os.environ.get("BOT_TOKEN")
MONGO_URI = os.environ.get("MONGO_URI")

if not BOT_TOKEN:
    sys.exit("FATAL: BOT_TOKEN environment variable is not set. Aborting.")
if not MONGO_URI:
    sys.exit("FATAL: MONGO_URI environment variable is not set. Aborting.")

# ── Database ────────────────────────────────────────────────
DB_NAME = "hosting_bot"

# ── File System ─────────────────────────────────────────────
BASE_DIR = Path.home() / "hosting_projects"
BASE_DIR.mkdir(parents=True, exist_ok=True)

# ── Admin ───────────────────────────────────────────────────
# Comma-separated list of owner Telegram user IDs (e.g. "123456789,987654321")
_owner_ids_raw = os.environ.get("OWNER_IDS", "")
OWNER_IDS: list[int] = [
    int(x.strip()) for x in _owner_ids_raw.split(",") if x.strip().isdigit()
]

# ── Optional ────────────────────────────────────────────────
_backup_raw = os.environ.get("BACKUP_CHANNEL_ID", "")
BACKUP_CHANNEL_ID: int | None = int(_backup_raw) if _backup_raw.lstrip("-").isdigit() else None

DEVELOPER_LINK = os.environ.get("DEVELOPER_LINK", "t.me/developer")
CHANNEL_LINK = os.environ.get("CHANNEL_LINK", "t.me/channel")

FOOTER = f"\n\n👨‍💻 Developer: {DEVELOPER_LINK} | 📢 Updates: {CHANNEL_LINK}"
