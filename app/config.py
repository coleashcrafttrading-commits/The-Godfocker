"""Configuration: loads secrets from .env and strategy presets from presets.json."""
import json
import os
from pathlib import Path

from dotenv import load_dotenv

BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR.parent / ".env")

# --- Alpaca ---
ALPACA_API_KEY = os.getenv("ALPACA_API_KEY", "")
ALPACA_API_SECRET = os.getenv("ALPACA_API_SECRET", "")
ALPACA_BASE_URL = os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets")
# Anything that isn't the live host is treated as paper.
IS_PAPER = "paper" in ALPACA_BASE_URL.lower()

# --- Supabase (optional) ---
SUPABASE_URL = os.getenv("SUPABASE_URL", "")
SUPABASE_ANON_KEY = os.getenv("SUPABASE_ANON_KEY", "")
SUPABASE_ENABLED = bool(SUPABASE_URL and SUPABASE_ANON_KEY)

# --- Dashboard ---
DASHBOARD_PASSWORD = os.getenv("DASHBOARD_PASSWORD", "changeme")
PASSWORD_REQUIRED = DASHBOARD_PASSWORD not in ("", "changeme")

PRESETS_PATH = BASE_DIR / "presets.json"


def load_presets() -> dict:
    with open(PRESETS_PATH) as f:
        return json.load(f)


def active_preset() -> dict:
    data = load_presets()
    return data["presets"][data["active"]]


def save_active_preset(updates: dict) -> dict:
    """Patch the currently active preset on disk and return it."""
    data = load_presets()
    key = data["active"]
    data["presets"][key].update(updates)
    with open(PRESETS_PATH, "w") as f:
        json.dump(data, f, indent=2)
    return data["presets"][key]


def keys_configured() -> bool:
    return bool(ALPACA_API_KEY and ALPACA_API_SECRET)
