"""FastAPI app: serves the dashboard and the trade endpoints."""
from __future__ import annotations

import asyncio
import base64
import secrets
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import alpaca_client, config, db, earnings, scheduler


@asynccontextmanager
async def lifespan(app: FastAPI):
    task = asyncio.create_task(scheduler.scheduler_loop())
    try:
        yield
    finally:
        task.cancel()


app = FastAPI(title="Alpaca Butterfly Bot", lifespan=lifespan)
templates = Jinja2Templates(directory=str(config.BASE_DIR / "templates"))
app.mount("/static", StaticFiles(directory=str(config.BASE_DIR / "static")), name="static")


@app.middleware("http")
async def require_login(request: Request, call_next):
    """When a dashboard password is set, gate the WHOLE app with HTTP Basic Auth so a
    public tunnel is safe. With no password set, the app is open (local use only)."""
    if config.PASSWORD_REQUIRED:
        auth = request.headers.get("Authorization", "")
        ok = False
        if auth.startswith("Basic "):
            try:
                pw = base64.b64decode(auth[6:]).decode("utf-8").split(":", 1)[1]
                ok = secrets.compare_digest(pw, config.DASHBOARD_PASSWORD)
            except Exception:  # noqa: BLE001
                ok = False
        if not ok:
            return Response(status_code=401, content="Login required",
                            headers={"WWW-Authenticate": 'Basic realm="Butterfly Bot"'})
    return await call_next(request)


def _check_password(supplied: str | None) -> None:
    # App-wide HTTP Basic Auth (require_login middleware) gates every request now,
    # so per-endpoint password checks are redundant.
    return


def _require_keys() -> None:
    if not config.keys_configured():
        raise HTTPException(
            status_code=400,
            detail="Alpaca API keys are not set. Add them to the .env file and restart.",
        )


@app.get("/", response_class=HTMLResponse)
def index(request: Request):
    return templates.TemplateResponse("index.html", {
        "request": request,
        "paper": config.IS_PAPER,
        "password_required": False,
    })


@app.get("/automation", response_class=HTMLResponse)
def automation_page(request: Request):
    return templates.TemplateResponse("automation.html", {
        "request": request,
        "paper": config.IS_PAPER,
        "password_required": False,
    })


@app.get("/api/automation")
def automation_status():
    auto = config.load_automation()
    return {
        "enabled": auto.get("enabled", False),
        "fire_time_ct": auto.get("fire_time_ct", "14:25"),
        "last_fired": auto.get("last_fired", ""),
        "last_result": auto.get("last_result"),
        "next_fire": scheduler.next_fire_iso(auto),
        "preset": auto.get("preset"),
        "keys_configured": config.keys_configured(),
        "paper": config.IS_PAPER,
    }


@app.post("/api/automation/toggle")
async def automation_toggle(request: Request, x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    body = await request.json()
    auto = config.update_automation({"enabled": bool(body.get("enabled"))})
    db.log_event("automation_toggle", {"enabled": auto["enabled"]})
    return {"enabled": auto["enabled"], "next_fire": scheduler.next_fire_iso(auto)}


@app.post("/api/automation/preset")
async def automation_update(request: Request, x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    body = await request.json()
    updates = {}
    if "fire_time_ct" in body:
        updates["fire_time_ct"] = body["fire_time_ct"]
    if "preset" in body:
        updates["preset"] = body["preset"]
    auto = config.update_automation(updates)
    return {"ok": True, "preset": auto["preset"], "fire_time_ct": auto["fire_time_ct"]}


@app.get("/api/automation/preview")
def automation_preview():
    _require_keys()
    auto = config.load_automation()
    try:
        return alpaca_client.preview(auto["preset"])
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Preview failed: {e}")


@app.post("/api/automation/run")
def automation_run(x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    _require_keys()
    auto = config.load_automation()
    try:
        result = alpaca_client.open_ladder(auto["preset"])
        db.log_event("auto_open_manual", result)
        return result
    except Exception as e:  # noqa: BLE001
        db.log_event("error", {"action": "auto_run", "error": str(e)})
        raise HTTPException(status_code=502, detail=f"Run failed: {e}")


@app.get("/earnings", response_class=HTMLResponse)
def earnings_page(request: Request):
    return templates.TemplateResponse("earnings.html", {
        "request": request,
        "paper": config.IS_PAPER,
        "password_required": False,
    })


def _calendar_view(e: dict) -> list[dict]:
    from datetime import date as _date
    out = []
    for i, c in enumerate(e.get("calendar", [])):
        row = {"index": i, **c}
        try:
            ed = _date.fromisoformat(c["earnings_date"])
            row["entry_day"] = earnings.entry_day_for(ed, c.get("timing", "AMC")).isoformat()
        except Exception:  # noqa: BLE001
            row["entry_day"] = None
        out.append(row)
    return out


@app.get("/api/earnings")
def earnings_status():
    e = config.load_earnings()
    out = {
        "automation_enabled": e.get("automation_enabled", False),
        "entry_minutes_before_close": e.get("entry_minutes_before_close", 5),
        "preset": e.get("preset"),
        "calendar": _calendar_view(e),
        "log": e.get("log", [])[:25],
        "keys_configured": config.keys_configured(),
        "paper": config.IS_PAPER,
    }
    if config.keys_configured():
        try:
            out["pnl"] = earnings.earnings_pnl(e.get("calendar", []))
        except Exception as ex:  # noqa: BLE001
            out["pnl_error"] = str(ex)
    return out


@app.post("/api/earnings/preset")
async def earnings_update(request: Request, x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    body = await request.json()
    e = config.load_earnings()
    if "entry_minutes_before_close" in body:
        e["entry_minutes_before_close"] = int(body["entry_minutes_before_close"])
    if isinstance(body.get("preset"), dict):
        patch = {k: v for k, v in body["preset"].items() if k in config.EARNINGS_PRESET_FIELDS}
        e["preset"].update(patch)
    config.save_earnings(e)
    return {"ok": True, "preset": e["preset"], "entry_minutes_before_close": e["entry_minutes_before_close"]}


@app.post("/api/earnings/toggle")
async def earnings_toggle(request: Request, x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    body = await request.json()
    e = config.load_earnings()
    e["automation_enabled"] = bool(body.get("enabled"))
    config.save_earnings(e)
    db.log_event("earnings_toggle", {"enabled": e["automation_enabled"]})
    return {"enabled": e["automation_enabled"]}


@app.post("/api/earnings/calendar")
async def earnings_calendar_edit(request: Request, x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    body = await request.json()
    e = config.load_earnings()
    action = body.get("action")
    if action == "delete":
        idx = int(body.get("index", -1))
        if 0 <= idx < len(e["calendar"]):
            e["calendar"].pop(idx)
    elif action == "clear":
        e["calendar"] = []
    else:  # add / import (list of {ticker, earnings_date, timing})
        for row in body.get("rows", []):
            t = str(row.get("ticker", "")).upper().strip()
            d = str(row.get("earnings_date", "")).strip()
            tm = str(row.get("timing", "AMC")).upper().strip()
            if not t or not d:
                continue
            try:
                from datetime import date as _date
                _date.fromisoformat(d)
            except Exception:  # noqa: BLE001
                continue
            tm = "BMO" if tm.startswith("B") else "AMC"
            e["calendar"].append({"ticker": t, "earnings_date": d, "timing": tm, "last_fired": "", "last_result": None})
    config.save_earnings(e)
    return {"ok": True, "calendar": _calendar_view(e)}


@app.get("/api/earnings/preview")
def earnings_preview(ticker: str, earnings_date: str):
    _require_keys()
    from datetime import date as _date
    e = config.load_earnings()
    try:
        return earnings.preview_straddle(ticker, e["preset"], _date.fromisoformat(earnings_date))
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Preview failed: {ex}")


@app.post("/api/earnings/run")
async def earnings_run(request: Request, x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    _require_keys()
    from datetime import date as _date
    body = await request.json()
    e = config.load_earnings()
    try:
        res = earnings.open_straddle(body["ticker"], e["preset"], _date.fromisoformat(body["earnings_date"]))
        e.setdefault("log", []).insert(0, {"ticker": body["ticker"], "when_et": "manual", "result": res})
        config.save_earnings(e)
        db.log_event("earnings_open_manual", res)
        return res
    except Exception as ex:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Run failed: {ex}")


@app.get("/api/status")
def status():
    out = {
        "keys_configured": config.keys_configured(),
        "paper": config.IS_PAPER,
        "supabase": config.SUPABASE_ENABLED,
        "preset": config.active_preset(),
    }
    if config.keys_configured():
        try:
            out["account"] = alpaca_client.get_account()
        except Exception as e:  # noqa: BLE001
            out["account_error"] = str(e)
    return out


@app.get("/api/preset")
def get_preset():
    return config.active_preset()


@app.post("/api/preset")
async def update_preset(request: Request, x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    updates = await request.json()
    allowed = {
        "underlying", "dte", "expiration", "num_rungs", "strike_increment", "center_override",
        "center_spacing", "wing_width", "quantity", "limit_shade",
    }
    patch = {k: v for k, v in updates.items() if k in allowed}
    return config.save_active_preset(patch)


@app.get("/api/expirations")
def expirations(underlying: str):
    _require_keys()
    try:
        return {"underlying": underlying.upper(), "expirations": alpaca_client.list_expirations(underlying)}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Could not load expirations: {e}")


@app.get("/api/preview")
def preview():
    _require_keys()
    try:
        return alpaca_client.preview(config.active_preset())
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=f"Preview failed: {e}")


@app.post("/api/open")
def open_position(x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    _require_keys()
    try:
        result = alpaca_client.open_ladder(config.active_preset())
        db.log_event("open", result)
        return result
    except Exception as e:  # noqa: BLE001
        db.log_event("error", {"action": "open", "error": str(e)})
        raise HTTPException(status_code=502, detail=f"Open failed: {e}")


@app.post("/api/close")
def close_positions(x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    _require_keys()
    try:
        result = alpaca_client.close_all_options()
        db.log_event("close", result)
        return result
    except Exception as e:  # noqa: BLE001
        db.log_event("error", {"action": "close", "error": str(e)})
        raise HTTPException(status_code=502, detail=f"Close failed: {e}")


@app.get("/api/orders")
def orders():
    _require_keys()
    try:
        return {"orders": alpaca_client.get_open_orders()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.post("/api/cancel")
def cancel_orders(x_dashboard_password: str | None = Header(default=None)):
    _check_password(x_dashboard_password)
    _require_keys()
    try:
        result = alpaca_client.cancel_all_orders()
        db.log_event("cancel", result)
        return result
    except Exception as e:  # noqa: BLE001
        db.log_event("error", {"action": "cancel", "error": str(e)})
        raise HTTPException(status_code=502, detail=f"Cancel failed: {e}")


@app.get("/api/positions")
def positions():
    _require_keys()
    try:
        return {"positions": alpaca_client.get_option_positions()}
    except Exception as e:  # noqa: BLE001
        raise HTTPException(status_code=502, detail=str(e))


@app.get("/api/log")
def log():
    return {"events": db.recent_events()}


@app.exception_handler(HTTPException)
async def http_exc_handler(request: Request, exc: HTTPException):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})
