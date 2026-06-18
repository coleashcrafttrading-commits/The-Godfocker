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


# --- Scheduled automation ---
AUTOMATION_PATH = BASE_DIR / "automation.json"
PRESET_FIELDS = {
    "underlying", "dte", "num_rungs", "strike_increment", "center_override",
    "center_spacing", "wing_width", "quantity", "limit_shade",
}


def load_automation() -> dict:
    """Automation config: its own copy of the preset + an on/off switch + fire time."""
    if not AUTOMATION_PATH.exists():
        default = {
            "enabled": False,
            "fire_time_ct": "14:25",   # 2:25 PM Central
            "last_fired": "",
            "last_result": None,
            "preset": active_preset(),
        }
        with open(AUTOMATION_PATH, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(AUTOMATION_PATH) as f:
        return json.load(f)


def save_automation(data: dict) -> dict:
    with open(AUTOMATION_PATH, "w") as f:
        json.dump(data, f, indent=2)
    return data


def update_automation(updates: dict) -> dict:
    data = load_automation()
    for k in ("enabled", "fire_time_ct", "last_fired", "last_result"):
        if k in updates:
            data[k] = updates[k]
    if "preset" in updates and isinstance(updates["preset"], dict):
        patch = {k: v for k, v in updates["preset"].items() if k in PRESET_FIELDS}
        data["preset"].update(patch)
    return save_automation(data)


# --- Earnings straddle strategy ---
EARNINGS_PATH = BASE_DIR / "earnings.json"
EARNINGS_PRESET_FIELDS = {
    "strategy_type", "strike_mode", "custom_strike", "strangle_offset",
    "quantity", "min_dte_after_earnings", "limit_shade",
}


def load_earnings() -> dict:
    if not EARNINGS_PATH.exists():
        default = {
            "automation_enabled": False,
            "entry_minutes_before_close": 5,
            "preset": {
                "strategy_type": "straddle",
                "strike_mode": "atm",
                "custom_strike": 0,
                "strangle_offset": 5.0,
                "quantity": 1,
                "min_dte_after_earnings": 0,
                "limit_shade": 0.0,
            },
            "calendar": [],
            "log": [],
        }
        with open(EARNINGS_PATH, "w") as f:
            json.dump(default, f, indent=2)
        return default
    with open(EARNINGS_PATH) as f:
        return json.load(f)


def save_earnings(data: dict) -> dict:
    with open(EARNINGS_PATH, "w") as f:
        json.dump(data, f, indent=2)
    return data

