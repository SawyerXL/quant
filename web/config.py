"""Web app settings — extends config/settings.py."""
import os, secrets
from pathlib import Path
from config.settings import ROOT, LOG_DIR

# Security
# Persistent secret key — store in DB or file to survive restarts
def _load_or_create_secret():
    key_file = ROOT / "data_store" / ".web_secret"
    if key_file.exists():
        return key_file.read_text().strip()
    key = secrets.token_hex(32)
    key_file.parent.mkdir(parents=True, exist_ok=True)
    key_file.write_text(key)
    return key

SECRET_KEY = os.getenv("WEB_SECRET_KEY") or _load_or_create_secret()
SESSION_DAYS = 7

# Database
DB_PATH = ROOT / "data_store" / "web.db"

# Snapshots
SNAPSHOT_DIR = ROOT / "data_store" / "web_cache"
SNAPSHOT_DIR.mkdir(parents=True, exist_ok=True)

# Rate limiting
RATE_LIMIT = "60/minute"
REALTIME_RATE_LIMIT = "4/minute"

# Admin — first registered user becomes admin
INVITE_CODE_LENGTH = 8
INVITE_CODE_EXPIRY_DAYS = 7
