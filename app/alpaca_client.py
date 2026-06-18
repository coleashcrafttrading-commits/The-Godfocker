"""Thin wrapper around alpaca-py for spot price, option quotes, and order submission."""
from __future__ import annotations

from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.historical.option import OptionHistoricalDataClient
from alpaca.data.requests import OptionLatestQuoteRequest, StockLatestTradeRequest
from alpaca.trading.client import TradingClient
from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import LimitOrderRequest, OptionLegRequest

from . import config
from .strategy import Rung, all_symbols, build_ladder, net_credit, payoff_summary

EASTERN = ZoneInfo("America/New_York")


def _trading() -> TradingClient:
    return TradingClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET, paper=config.IS_PAPER)


def _stock_data() -> StockHistoricalDataClient:
    return StockHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)


def _option_data() -> OptionHistoricalDataClient:
    return OptionHistoricalDataClient(config.ALPACA_API_KEY, config.ALPACA_API_SECRET)


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


def get_spot_price(underlying: str) -> float:
    req = StockLatestTradeRequest(symbol_or_symbols=underlying)
    res = _stock_data().get_stock_latest_trade(req)
    return float(res[underlying].price)


def get_mids(symbols: list[str]) -> dict[str, float]:
    """Latest mid price per option symbol. Falls back to bid or ask if one side is missing."""
    req = OptionLatestQuoteRequest(symbol_or_symbols=symbols)
    quotes = _option_data().get_option_latest_quote(req)
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
    spot = get_spot_price(underlying)
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

    risk = payoff_summary(
        centers=[rv["center"] for rv in rung_views],
        credits=[rv["credit"] for rv in rung_views],
        wing=float(preset["wing_width"]),
        qty=int(preset["quantity"]),
    )

    return {
        "underlying": underlying,
        "spot": spot,
        "expiration": exp.isoformat(),
        "quantity": int(preset["quantity"]),
        "rungs": rung_views,
        "total_credit": round(total_credit, 2),
        "est_credit_dollars": round(total_credit * 100, 2),
        "risk": risk,
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
