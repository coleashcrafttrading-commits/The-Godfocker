"""Pure strategy math: turn a spot price + preset into the exact option legs.

The structure (default preset) is a 3-rung *interlocking* Iron Butterfly ladder:
  - The middle rung is centered at the at-the-money (ATM) strike.
  - One rung is centered one `center_spacing` below, one rung one above.
  - Each rung SELLS a call + put at its own center, and BUYS a call
    `wing_width` above and a put `wing_width` below that same center.

Example (ATM=751, center_spacing=1, wing_width=3):
  Rung 750 -> SELL 750C, SELL 750P, BUY 753C, BUY 747P
  Rung 751 -> SELL 751C, SELL 751P, BUY 754C, BUY 748P
  Rung 752 -> SELL 752C, SELL 752P, BUY 755C, BUY 749P
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field
from datetime import date


def occ_symbol(underlying: str, exp: date, right: str, strike: float) -> str:
    """Build an OCC/OSI option symbol, e.g. SPY260617C00750000."""
    yymmdd = exp.strftime("%y%m%d")
    strike_milli = int(round(strike * 1000))
    return f"{underlying.upper()}{yymmdd}{right.upper()}{strike_milli:08d}"


def nearest_strike(price: float, increment: float) -> float:
    return round(round(price / increment) * increment, 2)


@dataclass
class Leg:
    symbol: str
    side: str          # "buy" or "sell"
    right: str         # "C" or "P"
    strike: float


@dataclass
class Rung:
    center: float
    legs: list[Leg] = field(default_factory=list)


def _nearest(strikes: list[float], target: float) -> float:
    return min(strikes, key=lambda s: abs(s - target))


def _nearest_above(strikes: list[float], target: float, floor: float) -> float | None:
    cands = [s for s in strikes if s > floor]
    return min(cands, key=lambda s: abs(s - target)) if cands else None


def _nearest_below(strikes: list[float], target: float, ceil: float) -> float | None:
    cands = [s for s in strikes if s < ceil]
    return min(cands, key=lambda s: abs(s - target)) if cands else None


def build_ladder(spot: float, preset: dict, exp: date,
                 available_strikes: list[float] | None = None) -> list[Rung]:
    """Return the list of rungs (each a 4-leg Iron Butterfly) for the ladder.

    When `available_strikes` (the ticker's actually-listed strikes for this expiration)
    is given, every center and wing snaps to a REAL strike — so it works on tickers
    whose grid isn't $1 (NVDA, etc.). Without it, falls back to the increment grid.
    """
    underlying = preset["underlying"]
    n = int(preset["num_rungs"])
    spacing = float(preset["center_spacing"])
    wing = float(preset["wing_width"])
    increment = float(preset["strike_increment"])
    strikes = sorted(available_strikes) if available_strikes else None

    override = preset.get("center_override")
    base = float(override) if override else spot
    center = _nearest(strikes, base) if strikes else nearest_strike(base, increment)

    half = (n - 1) // 2
    centers: list[float] = []
    seen: set[float] = set()
    for i in range(n):
        raw = center + (i - half) * spacing
        c = _nearest(strikes, raw) if strikes else nearest_strike(raw, increment)
        if c not in seen:
            seen.add(c)
            centers.append(c)

    rungs: list[Rung] = []
    for c in centers:
        if strikes:
            up = _nearest_above(strikes, c + wing, c) or nearest_strike(c + wing, increment)
            dn = _nearest_below(strikes, c - wing, c) or nearest_strike(c - wing, increment)
        else:
            up = nearest_strike(c + wing, increment)
            dn = nearest_strike(c - wing, increment)
        legs = [
            Leg(occ_symbol(underlying, exp, "C", c), "sell", "C", c),
            Leg(occ_symbol(underlying, exp, "P", c), "sell", "P", c),
            Leg(occ_symbol(underlying, exp, "C", up), "buy", "C", up),
            Leg(occ_symbol(underlying, exp, "P", dn), "buy", "P", dn),
        ]
        rungs.append(Rung(center=c, legs=legs))
    return rungs


def all_symbols(rungs: list[Rung]) -> list[str]:
    return [leg.symbol for r in rungs for leg in r.legs]


def net_credit(rung: Rung, mids: dict[str, float]) -> float:
    """Net credit per spread = sum(sell mids) - sum(buy mids). Positive = credit."""
    total = 0.0
    for leg in rung.legs:
        mid = mids.get(leg.symbol)
        if mid is None:
            raise ValueError(f"No quote for {leg.symbol}")
        total += mid if leg.side == "sell" else -mid
    return round(total, 2)


def _position_pl(rung_views: list[dict], qty: int):
    """Build the exact combined P/L function straight from the raw legs.

    Returns (net_credit, multiplier, strikes, pl_at) or None if any leg is unquoted.
    pl_at(S) = (net_credit - close_liability(S)) * 100 * qty, where the liability is
    the net intrinsic of all legs (short legs add, long legs subtract). Fully general:
    any strikes, wing widths, rung counts, symmetric or not.
    """
    legs = [l for rv in rung_views for l in rv["legs"]]
    if not legs or any(l.get("mid") is None for l in legs):
        return None
    mult = 100 * int(qty)
    nc = sum((1 if l["side"] == "sell" else -1) * l["mid"] for l in legs)
    strikes = [l["strike"] for l in legs]

    def pl_at(s: float) -> float:
        liab = 0.0
        for l in legs:
            intr = (s - l["strike"]) if l["right"] == "C" else (l["strike"] - s)
            if intr > 0:
                liab += (1 if l["side"] == "sell" else -1) * intr
        return (nc - liab) * mult

    return nc, mult, strikes, pl_at


def _rung_collateral(rung_views: list[dict], qty: int) -> float:
    """Sum of each rung's risk width x 100 (the wider of its call/put wing)."""
    total = 0.0
    for rv in rung_views:
        c = rv["center"]
        ups = [l["strike"] for l in rv["legs"] if l["side"] == "buy" and l["right"] == "C"]
        dns = [l["strike"] for l in rv["legs"] if l["side"] == "buy" and l["right"] == "P"]
        wc = (ups[0] - c) if ups else 0.0
        wp = (c - dns[0]) if dns else 0.0
        total += max(wc, wp) * 100 * int(qty)
    return total


def payoff_summary(rung_views: list[dict], qty: int) -> dict | None:
    """Mathematically calculated combined max profit / max loss + collateral.

    Scans the true combined payoff (built from the raw legs) for its peak and trough,
    so overlapping rungs of any size are handled exactly. Collateral = strike width
    x 100 per rung; broker_margin (= max loss) is Alpaca's defined-risk hold.
    """
    res = _position_pl(rung_views, qty)
    if res is None:
        return None
    nc, mult, strikes, pl_at = res
    lo, hi = min(strikes) - 2, max(strikes) + 2
    pts = 800
    vals = [pl_at(lo + i * (hi - lo) / pts) for i in range(pts + 1)]
    return {
        "max_profit": round(max(vals), 2),     # true peak of the combined payoff
        "max_loss": round(min(vals), 2),        # true trough (negative)
        "collateral": round(_rung_collateral(rung_views, qty), 2),  # strike width x 100/rung
        "broker_margin": round(-min(vals), 2),  # defined-risk margin = max loss
        "credit_collected": round(nc * mult, 2),
        "rungs_priced": len(rung_views),
    }


def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2))


def black_scholes(S: float, K: float, t: float, r: float, sigma: float, is_call: bool) -> float:
    """European Black-Scholes price (no dividends). At/after expiry returns intrinsic."""
    if t <= 0 or sigma <= 0:
        return max((S - K) if is_call else (K - S), 0.0)
    sqrt_t = math.sqrt(t)
    d1 = (math.log(S / K) + (r + sigma * sigma / 2) * t) / (sigma * sqrt_t)
    d2 = d1 - sigma * sqrt_t
    if is_call:
        return S * _norm_cdf(d1) - K * math.exp(-r * t) * _norm_cdf(d2)
    return K * math.exp(-r * t) * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def implied_vol(price: float, S: float, K: float, t: float, r: float, is_call: bool) -> float:
    """Back out implied volatility from a market price via bisection. 0.0 if unsolvable."""
    intrinsic = max((S - K) if is_call else (K - S), 0.0)
    if t <= 0 or price <= intrinsic + 1e-6:
        return 0.0
    lo, hi = 1e-4, 5.0
    if black_scholes(S, K, t, r, hi, is_call) < price:
        return 0.0  # price above model max — give up, caller uses a default
    for _ in range(64):
        mid = (lo + hi) / 2
        if black_scholes(S, K, t, r, mid, is_call) > price:
            hi = mid
        else:
            lo = mid
    return round((lo + hi) / 2, 4)


def payoff_curve(rung_views: list[dict], qty: int, spot: float, points: int = 160) -> dict | None:
    """Combined at-expiration P/L across a price range, for the simulator graph."""
    res = _position_pl(rung_views, qty)
    if res is None:
        return None
    nc, mult, strikes, pl_at = res
    lo, hi = min(strikes) - 2, max(strikes) + 2
    lo = min(lo, spot - 2)
    hi = max(hi, spot + 2)

    curve: list[list[float]] = []
    breakevens: list[float] = []
    step = (hi - lo) / points
    prev = None
    for i in range(points + 1):
        s = lo + i * step
        pl = pl_at(s)
        curve.append([round(s, 2), round(pl, 2)])
        if prev is not None:
            s0, pl0 = prev
            if (pl0 <= 0 < pl) or (pl0 >= 0 > pl):
                t = pl0 / (pl0 - pl) if (pl0 - pl) != 0 else 0.0
                breakevens.append(round(s0 + t * (s - s0), 2))
        prev = (s, pl)

    pls = [p for _, p in curve]
    return {
        "curve": curve,
        "breakevens": breakevens,
        "max_pl": round(max(pls), 2),
        "min_pl": round(min(pls), 2),
        "lo": round(lo, 2),
        "hi": round(hi, 2),
        "centers": [rv["center"] for rv in rung_views],
    }
