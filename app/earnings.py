"""Earnings straddle strategy: calendar, timing logic, straddle builder, execution.

Idea: buy a (long) straddle/strangle just before a company's earnings to capture the
post-announcement move. We enter as LATE as possible on the last trading session
before the move:
  - earnings AFTER market close (AMC) on day D  -> buy near the close of D
  - earnings BEFORE market open (BMO) on day D   -> buy near the close of the prior trading day
"""
from __future__ import annotations

import re
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

from alpaca.trading.enums import OrderClass, OrderSide, TimeInForce
from alpaca.trading.requests import (
    GetOptionContractsRequest,
    LimitOrderRequest,
    OptionLegRequest,
)

from . import alpaca_client as ac

EASTERN = ZoneInfo("America/New_York")
_OCC = re.compile(r"^([A-Z]+)\d{6}[CP]\d{8}$")


def underlying_of(option_symbol: str) -> str:
    m = _OCC.match(option_symbol)
    return m.group(1) if m else option_symbol


def prev_trading_day(d: date) -> date:
    d = d - timedelta(days=1)
    while d.weekday() >= 5:  # skip Sat/Sun (holidays not handled yet)
        d -= timedelta(days=1)
    return d


def entry_day_for(earnings_date: date, timing: str) -> date:
    """The trading day we buy on. BMO -> prior trading day; AMC -> the earnings day."""
    if str(timing).upper() == "BMO":
        return prev_trading_day(earnings_date)
    return earnings_date


def _contracts(ticker: str, min_exp: date, spot: float):
    """Active option contracts for a ticker, near the money, expiring on/after min_exp."""
    client = ac._trading()
    lo = round(spot * 0.85, 0)
    hi = round(spot * 1.15, 0)
    req = GetOptionContractsRequest(
        underlying_symbols=[ticker.upper()],
        expiration_date_gte=min_exp,
        strike_price_gte=str(lo),
        strike_price_lte=str(hi),
        status="active",
        limit=500,
    )
    return client.get_option_contracts(req).option_contracts


def build_straddle(ticker: str, preset: dict, earnings_date: date) -> dict:
    """Pick the expiration + strike(s) and return the 2-leg long straddle/strangle."""
    ticker = ticker.upper()
    spot = ac.get_spot_price(ticker)
    min_exp = earnings_date + timedelta(days=int(preset.get("min_dte_after_earnings", 0) or 0))
    contracts = _contracts(ticker, min_exp, spot)
    if not contracts:
        raise ValueError(f"No option contracts for {ticker} on/after {min_exp}.")

    exps = sorted({c.expiration_date for c in contracts})
    exp = exps[0]
    on_exp = [c for c in contracts if c.expiration_date == exp]
    calls = {float(c.strike_price): c.symbol for c in on_exp if "call" in str(c.type).lower()}
    puts = {float(c.strike_price): c.symbol for c in on_exp if "put" in str(c.type).lower()}
    if not calls or not puts:
        raise ValueError(f"{ticker} {exp} missing call/put strikes.")

    stype = preset.get("strategy_type", "straddle")
    if stype == "strangle":
        off = float(preset.get("strangle_offset", 5.0))
        kc = min(calls, key=lambda s: abs(s - (spot + off)))
        kp = min(puts, key=lambda s: abs(s - (spot - off)))
    else:  # straddle: same strike for both
        target = float(preset["custom_strike"]) if (preset.get("strike_mode") == "custom" and preset.get("custom_strike")) else spot
        common = sorted(set(calls) & set(puts))
        k = min(common, key=lambda s: abs(s - target))
        kc = kp = k

    legs = [
        {"symbol": calls[kc], "side": "buy", "right": "C", "strike": kc},
        {"symbol": puts[kp], "side": "buy", "right": "P", "strike": kp},
    ]
    return {
        "ticker": ticker, "spot": round(spot, 2), "expiration": exp.isoformat(),
        "strategy_type": stype, "legs": legs,
    }


def preview_straddle(ticker: str, preset: dict, earnings_date: date) -> dict:
    built = build_straddle(ticker, preset, earnings_date)
    mids = ac.get_mids([l["symbol"] for l in built["legs"]])
    qty = int(preset.get("quantity", 1))
    debit = 0.0
    ok = True
    for l in built["legs"]:
        m = mids.get(l["symbol"])
        l["mid"] = m
        if m is None:
            ok = False
        else:
            debit += m  # both legs are bought
    built["net_debit"] = round(debit, 2) if ok else None
    built["cost"] = round(debit * 100 * qty, 2) if ok else None
    built["quantity"] = qty
    # Long straddle: max loss = debit paid; profit is open-ended on a big move.
    built["max_loss"] = round(debit * 100 * qty, 2) if ok else None
    return built


def open_straddle(ticker: str, preset: dict, earnings_date: date) -> dict:
    """Submit a long straddle/strangle as a 2-leg MLEG debit at the net mid."""
    built = build_straddle(ticker, preset, earnings_date)
    mids = ac.get_mids([l["symbol"] for l in built["legs"]])
    qty = int(preset.get("quantity", 1))
    shade = float(preset.get("limit_shade", 0.0))

    debit = 0.0
    for l in built["legs"]:
        m = mids.get(l["symbol"])
        if m is None:
            raise ValueError(f"No two-sided quote for {l['symbol']}; not submitting.")
        debit += m
    # Buying -> to fill, may pay a touch MORE than mid (shade adds to the debit).
    limit = round(debit + shade, 2)

    legs = [OptionLegRequest(symbol=l["symbol"], side=OrderSide.BUY, ratio_qty=1) for l in built["legs"]]
    order = LimitOrderRequest(
        qty=qty, limit_price=limit, order_class=OrderClass.MLEG,
        time_in_force=TimeInForce.DAY, legs=legs,
    )
    o = ac._trading().submit_order(order)
    return {
        "ticker": ticker, "ok": True, "order_id": str(o.id), "status": str(o.status),
        "expiration": built["expiration"], "strikes": [l["strike"] for l in built["legs"]],
        "limit_price": float(o.limit_price or 0), "qty": qty,
    }


def earnings_pnl(calendar: list[dict]) -> dict:
    """Open option positions whose underlying is on the earnings calendar, with totals."""
    tickers = {c["ticker"].upper() for c in calendar}
    positions = ac.get_option_positions()
    rows, tot_pl, tot_mv = [], 0.0, 0.0
    for p in positions:
        und = underlying_of(p["symbol"])
        if und in tickers:
            rows.append({**p, "underlying": und})
            tot_pl += p["unrealized_pl"]
            tot_mv += p["market_value"]
    return {"positions": rows, "total_unrealized": round(tot_pl, 2), "total_market_value": round(tot_mv, 2)}
