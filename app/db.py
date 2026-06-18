"""Optional Supabase logging. If Supabase isn't configured, all calls are no-ops."""
from __future__ import annotations

import json

from . import config

_client = None


def _get_client():
    global _client
    if not config.SUPABASE_ENABLED:
        return None
    if _client is None:
        from supabase import create_client
        _client = create_client(config.SUPABASE_URL, config.SUPABASE_ANON_KEY)
    return _client


def log_event(event_type: str, payload: dict) -> None:
    """Insert a row into the trade_log table. Never raises — logging must not break trading."""
    client = _get_client()
    if client is None:
        return
    try:
        client.table("trade_log").insert({
            "event_type": event_type,
            "payload": json.loads(json.dumps(payload, default=str)),
        }).execute()
    except Exception:  # noqa: BLE001 - logging is best-effort
        pass


def recent_events(limit: int = 25) -> list[dict]:
    client = _get_client()
    if client is None:
        return []
    try:
        res = (
            client.table("trade_log")
            .select("*")
            .order("created_at", desc=True)
            .limit(limit)
            .execute()
        )
        return res.data or []
    except Exception:  # noqa: BLE001
        return []
