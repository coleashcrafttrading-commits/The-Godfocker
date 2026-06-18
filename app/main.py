"""FastAPI app: serves the dashboard and the trade endpoints."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

from . import alpaca_client, config, db, scheduler


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


def _check_password(supplied: str | None) -> None:
    if config.PASSWORD_REQUIRED and supplied != config.DASHBOARD_PASSWORD:
        raise HTTPException(status_code=401, detail="Wrong or missing dashboard password.")


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
        "password_required": config.PASSWORD_REQUIRED,
    })


@app.get("/automation", response_class=HTMLResponse)
def automation_page(request: Request):
    return templates.TemplateResponse("automation.html", {
        "request": request,
        "paper": config.IS_PAPER,
        "password_required": config.PASSWORD_REQUIRED,
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
        "underlying", "dte", "num_rungs", "strike_increment", "center_override",
        "center_spacing", "wing_width", "quantity", "limit_shade",
    }
    patch = {k: v for k, v in updates.items() if k in allowed}
    return config.save_active_preset(patch)


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
