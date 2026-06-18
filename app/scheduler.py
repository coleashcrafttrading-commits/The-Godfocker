"""Background scheduler: opens the automation ladder at the preset time (Central)."""
from __future__ import annotations

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

from . import alpaca_client, config, db

CT = ZoneInfo("America/Chicago")
CHECK_SECONDS = 15
FIRE_WINDOW_SECONDS = 90  # fire if we're within this many seconds past the target


def _parse_time(s: str) -> tuple[int, int]:
    hh, mm = s.split(":")
    return int(hh), int(mm)


def _fmt12(hh: int, mm: int) -> str:
    ap = "AM" if hh < 12 else "PM"
    return f"{hh % 12 or 12}:{mm:02d} {ap}"


def next_fire_iso(auto: dict) -> str | None:
    """Human description of the next scheduled fire, for the dashboard."""
    if not auto.get("enabled"):
        return None
    now = datetime.now(CT)
    hh, mm = _parse_time(auto.get("fire_time_ct", "14:25"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    label = _fmt12(hh, mm)
    fired_today = auto.get("last_fired") == now.date().isoformat()
    if now >= target or fired_today or now.weekday() >= 5:
        return f"next trading day at {label} CT"
    return f"today at {label} CT"


async def scheduler_loop():
    """Runs for the app's lifetime; checks every CHECK_SECONDS whether to fire."""
    while True:
        try:
            await _tick()
        except Exception as e:  # noqa: BLE001 - never let the loop die
            db.log_event("error", {"action": "scheduler", "error": str(e)})
        await asyncio.sleep(CHECK_SECONDS)


async def _tick():
    auto = config.load_automation()
    if not auto.get("enabled"):
        return
    if not config.keys_configured():
        return
    now = datetime.now(CT)
    if now.weekday() >= 5:  # weekend
        return
    today = now.date().isoformat()
    if auto.get("last_fired") == today:
        return

    hh, mm = _parse_time(auto.get("fire_time_ct", "14:25"))
    target = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
    delta = (now - target).total_seconds()
    if not (0 <= delta < FIRE_WINDOW_SECONDS):
        return

    # Mark fired BEFORE submitting so a slow open can't double-fire on the next tick.
    auto["last_fired"] = today
    config.save_automation(auto)
    db.log_event("auto_trigger", {"time_ct": now.isoformat(), "preset": auto["preset"]})

    try:
        result = await asyncio.to_thread(alpaca_client.open_ladder, auto["preset"])
        result["fired_at_ct"] = now.strftime("%Y-%m-%d %H:%M CT")
        config.update_automation({"last_result": result})
        db.log_event("auto_open", result)
    except Exception as e:  # noqa: BLE001
        err = {"ok": False, "error": str(e), "fired_at_ct": now.strftime("%Y-%m-%d %H:%M CT")}
        config.update_automation({"last_result": err})
        db.log_event("error", {"action": "auto_open", "error": str(e)})
