"""Thin wrapper around alpaca-py for spot price, option quotes, and order submission."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.data.enums import DataFeed, OptionsFeed
from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import (
    OptionLatestQuoteRequest,
    StockLatestQuoteRequest,
    StockLatestTradeRequest,
)
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, QueryOrderStatus, TimeInForce
from alpaca.trading.requests import GetOrdersRequest, LimitOrderRequest, OptionLegRequest

from . import config
from .strategy import (
    Rung,
    all_symbols,
    build_ladder,
    implied_vol,
    net_credit,
    payoff_curve,
    payoff_summary,
)

# Risk-free rate used by the simulator's Black-Scholes pricing (approx, short-dated).
SIM_RATE = 0.04

EASTERN = ZoneInfo("America/New_York")


def _trading() -> TradingClient:
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=config.IS_PAPER)


def _stock_data() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)


def _option_data() -> OptionHistoricalDataClient:
    return OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)


def _option_quotes(symbols: list[str]):
    """Latest option quotes, preferring the paid real-time OPRA feed.

    Falls back to the free 'indicative' feed (and then the client default) if the
    OPRA subscription isn't available, so the dashboard still works either way.
    """
    client = _option_data()
    for feed in (OptionsFeed.OPRA, OptionsFeed.INDICATIVE):
        try:
            return client.get_option_latest_quote(
                OptionLatestQuoteRequest(symbol_or_symbols=symbols, feed=feed)
            )
        except Exception:  # noqa: BLE001 - try the next feed
            continue
    return client.get_option_latest_quote(OptionLatestQuoteRequest(symbol_or_symbols=symbols))


def expiration_for(dte: int) -> date:
    """Expiration date that is `dte` days from today (Eastern). dte=0 -> today."""
    return (datetime.now(EASTERN) + timedelta(days=int(dte))).date()


def get_account() -> dict:
    a = _trading().get_account()
    return {
        "account_number": a.account_number,
        "status": str(a.status),
        "buying_power": float(a.buying_power),
        "cash": float(a.cash),
        "portfolio_value": float(a.portfolio_value),
        "options_level": getattr(a, "options_trading_level", None),
        "paper": config.IS_PAPER,
    }


def get_spot_detail(underlying: str) -> dict:
    """Current underlying price plus its source and timestamp.

    Prefer the SIP NBBO midpoint (tracks the real market); after hours the 'last
    trade' can be a stale/anomalous block print, so it's only a fallback. NOTE:
    Alpaca's feed stops at 8 PM ET and does not carry the overnight (Blue Ocean)
    session, so the timestamp can lag a live overnight quote on other platforms.
    """
    client = _stock_data()
    try:
        q = client.get_stock_latest_quote(
            StockLatestQuoteRequest(symbol_or_symbols=underlying, feed=DataFeed.SIP)
        )[underlying]
        bid, ask = float(q.bid_price or 0), float(q.ask_price or 0)
        if bid > 0 and ask > 0 and ask >= bid:
            ts = q.timestamp.astimezone(EASTERN).strftime("%b %d %I:%M %p ET")
            return {"price": round((bid + ask) / 2, 2), "source": "SIP NBBO mid", "time": ts}
    except Exception:  # noqa: BLE001 - fall back to last trade below
        pass
    t = client.get_stock_latest_trade(
        StockLatestTradeRequest(symbol_or_symbols=underlying, feed=DataFeed.SIP)
    )[underlying]
    ts = t.timestamp.astimezone(EASTERN).strftime("%b %d %I:%M %p ET")
    return {"price": float(t.price), "source": "SIP last trade", "time": ts}


def get_spot_price(underlying: str) -> float:
    return get_spot_detail(underlying)["price"]


def get_mids(symbols: list[str]) -> dict[str, float]:
    """Latest mid price per option symbol. Falls back to bid or ask if one side is missing."""
    quotes = _option_quotes(symbols)
    mids: dict[str, float] = {}
    for sym, q in quotes.items():
        bid = float(q.bid_price or 0)
        ask = float(q.ask_price or 0)
        if bid > 0 and ask > 0:
            mids[sym] = round((bid + ask) / 2, 2)
        elif ask > 0:
            mids[sym] = round(ask, 2)
        elif bid > 0:
            mids[sym] = round(bid, 2)
    return mids


def preview(preset: dict) -> dict:
    """Build the ladder and price each rung WITHOUT submitting anything."""
    underlying = preset["underlying"]
    exp = expiration_for(preset["dte"])
    spot_detail = get_spot_detail(underlying)
    spot = spot_detail["price"]
    rungs = build_ladder(spot, preset, exp)
    mids = get_mids(all_symbols(rungs))

    rung_views = []
    total_credit = 0.0
    for r in rungs:
        try:
            credit = net_credit(r, mids)
        except ValueError:
            credit = None
        if credit is not None:
            total_credit += credit * int(preset["quantity"])
        rung_views.append({
            "center": r.center,
            "credit": credit,
            "legs": [
                {"symbol": l.symbol, "side": l.side, "right": l.right,
                 "strike": l.strike, "mid": mids.get(l.symbol)}
                for l in r.legs
            ],
        })

    # --- Quote sanity / staleness checks (mostly relevant after hours) ---
    warnings: list[str] = []
    wing = float(preset["wing_width"])

    missing = sum(1 for rv in rung_views for l in rv["legs"] if l["mid"] is None)
    if missing:
        warnings.append(
            f"{missing} option leg(s) have no quote — those strikes/expiration may not be "
            f"listed for {underlying}. Try a valid expiration date or adjust the strikes/wing width."
        )

    # Put-call parity from the middle rung: short call/put share a strike.
    mid_rung = rung_views[len(rung_views) // 2]
    cmid = next((l["mid"] for l in mid_rung["legs"] if l["side"] == "sell" and l["right"] == "C"), None)
    pmid = next((l["mid"] for l in mid_rung["legs"] if l["side"] == "sell" and l["right"] == "P"), None)
    implied_spot = None
    if cmid is not None and pmid is not None:
        implied_spot = round(cmid - pmid + mid_rung["center"], 2)  # ~ ignores tiny carry
        if abs(implied_spot - spot) > 1.0:
            warnings.append(
                f"Option quotes look stale: they imply {underlying} ~${implied_spot:.2f}, but the "
                f"stock feed says ${spot:.2f}. The options market is likely closed — figures "
                f"below may be unreliable until it reopens."
            )

    # A defined-risk fly can't collect more credit than its wing width (max loss >= 0).
    for rv in rung_views:
        if rv["credit"] is not None and rv["credit"] >= wing:
            warnings.append(
                f"Rung @ {rv['center']:.0f}: quoted credit ${rv['credit']:.2f} ≥ wing width "
                f"${wing:.2f}, which is impossible with live quotes — that leg's quote is stale or crossed."
            )
            break

    risk = payoff_summary(rung_views, int(preset["quantity"]))
    curve = payoff_curve(rung_views, int(preset["quantity"]), spot)

    # Build the interactive-simulator payload: per-leg implied vol + time to expiry,
    # so the dashboard can re-price the whole position at any underlying/time client-side.
    sim = None
    if curve:
        now_et = datetime.now(EASTERN)
        exp_dt = datetime(exp.year, exp.month, exp.day, 16, 0, tzinfo=EASTERN)
        t_years = max((exp_dt - now_et).total_seconds(), 60) / (365.25 * 24 * 3600)
        sim_legs = []
        all_quoted = True
        for rv in rung_views:
            for leg in rv["legs"]:
                entry = leg["mid"]
                if entry is None:
                    all_quoted = False
                    break
                is_call = leg["right"] == "C"
                iv = implied_vol(entry, spot, leg["strike"], t_years, SIM_RATE, is_call)
                sim_legs.append({
                    "strike": leg["strike"],
                    "type": leg["right"],
                    "side": "short" if leg["side"] == "sell" else "long",
                    "entry": entry,
                    "iv": iv if iv > 0 else 0.20,  # fallback if IV can't be solved
                })
            if not all_quoted:
                break
        if all_quoted and sim_legs:
            sim = {
                "spot": spot,
                "t_years": round(t_years, 6),
                "dte_days": round(t_years * 365.25, 2),
                "r": SIM_RATE,
                "qty": int(preset["quantity"]),
                "lo": curve["lo"],
                "hi": curve["hi"],
                "legs": sim_legs,
            }

    return {
        "underlying": underlying,
        "spot": spot,
        "spot_source": spot_detail["source"],
        "spot_time": spot_detail["time"],
        "expiration": exp.isoformat(),
        "quantity": int(preset["quantity"]),
        "rungs": rung_views,
        "total_credit": round(total_credit, 2),
        "est_credit_dollars": round(total_credit * 100, 2),
        "risk": risk,
        "curve": curve,
        "sim": sim,
        "warnings": warnings,
        "implied_spot": implied_spot,
    }


def _submit_rung(client: TradingClient, rung: Rung, qty: int, limit_price: float, shade: float):
    legs = [
        OptionLegRequest(
            symbol=l.symbol,
            side=OrderSide.SELL if l.side == "sell" else OrderSide.BUY,
            ratio_qty=1,
        )
        for l in rung.legs
    ]
    # Shade a credit *down* slightly to improve fill odds (collect a bit less).
    price = max(round(limit_price - shade, 2), 0.01)
    order = LimitOrderRequest(
        qty=qty,
        limit_price=price,
        order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY,
        legs=legs,
    )
    return client.submit_order(order)


def open_ladder(preset: dict) -> dict:
    """Submit one net-credit limit combo order per rung. Returns a per-rung result."""
    underlying = preset["underlying"]
    qty = int(preset["quantity"])
    shade = float(preset.get("limit_shade", 0.0))
    exp = expiration_for(preset["dte"])

    spot = get_spot_price(underlying)
    rungs = build_ladder(spot, preset, exp)
    mids = get_mids(all_symbols(rungs))

    client = _trading()
    results = []
    for r in rungs:
        try:
            credit = net_credit(r, mids)
            if credit <= 0:
                raise ValueError(f"Computed net credit {credit} is not positive; skipping.")
            o = _submit_rung(client, r, qty, credit, shade)
            results.append({
                "center": r.center, "ok": True, "order_id": str(o.id),
                "status": str(o.status), "limit_price": float(o.limit_price or 0),
            })
        except Exception as e:  # noqa: BLE001 - surface per-rung failures to the UI
            results.append({"center": r.center, "ok": False, "error": str(e)})

    return {
        "underlying": underlying, "spot": spot, "expiration": exp.isoformat(),
        "quantity": qty, "results": results,
        "submitted": sum(1 for r in results if r.get("ok")),
        "failed": sum(1 for r in results if not r.get("ok")),
    }


def get_open_orders() -> list[dict]:
    """Currently working/queued orders (e.g. unfilled limit combos)."""
    orders = _trading().get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=50))
    out = []
    for o in orders:
        legs = getattr(o, "legs", None) or []
        out.append({
            "id": str(o.id),
            "status": str(o.status).split(".")[-1],
            "order_class": (str(o.order_class).split(".")[-1] if o.order_class else ""),
            "qty": float(o.qty) if o.qty else None,
            "limit_price": float(o.limit_price) if o.limit_price else None,
            "legs": [
                {"side": str(l.side).split(".")[-1], "symbol": l.symbol,
                 "ratio_qty": float(l.ratio_qty)}
                for l in legs
            ],
        })
    return out


def cancel_all_orders() -> dict:
    """Cancel every open order (clears queued/working orders)."""
    resp = _trading().cancel_orders()
    return {"requested": len(resp)}


def get_option_positions() -> list[dict]:
    positions = _trading().get_all_positions()
    out = []
    for p in positions:
        if str(getattr(p, "asset_class", "")).endswith("option") or "option" in str(getattr(p, "asset_class", "")).lower():
            out.append({
                "symbol": p.symbol,
                "qty": float(p.qty),
                "side": str(p.side),
                "avg_entry_price": float(p.avg_entry_price),
                "market_value": float(p.market_value or 0),
                "unrealized_pl": float(p.unrealized_pl or 0),
            })
    return out


def close_all_options() -> dict:
    """Market-close every open option position (gets you flat with one click)."""
    client = _trading()
    positions = get_option_positions()
    results = []
    for p in positions:
        try:
            client.close_position(p["symbol"])
            results.append({"symbol": p["symbol"], "ok": True})
        except Exception as e:  # noqa: BLE001
            results.append({"symbol": p["symbol"], "ok": False, "error": str(e)})
    return {
        "closed": sum(1 for r in results if r["ok"]),
        "failed": sum(1 for r in results if not r["ok"]),
        "results": results,
    }
