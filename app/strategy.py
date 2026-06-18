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


def build_ladder(spot: float, preset: dict, exp: date) -> list[Rung]:
    """Return the list of rungs (each a 4-leg Iron Butterfly) for the ladder."""
    underlying = preset["underlying"]
    n = int(preset["num_rungs"])
    spacing = float(preset["center_spacing"])
    wing = float(preset["wing_width"])
    increment = float(preset["strike_increment"])

    atm = nearest_strike(spot, increment)
    # Centers symmetric around ATM: e.g. n=3 -> [-1, 0, +1] * spacing.
    centers = [round(atm + (i - (n - 1) / 2.0) * spacing, 2) for i in range(n)]

    rungs: list[Rung] = []
    for c in centers:
        legs = [
            Leg(occ_symbol(underlying, exp, "C", c), "sell", "C", c),
            Leg(occ_symbol(underlying, exp, "P", c), "sell", "P", c),
            Leg(occ_symbol(underlying, exp, "C", c + wing), "buy", "C", round(c + wing, 2)),
            Leg(occ_symbol(underlying, exp, "P", c - wing), "buy", "P", round(c - wing, 2)),
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


def payoff_summary(centers: list[float], credits: list[float | None],
                   wing: float, qty: int) -> dict | None:
    """Defined-risk figures for the ladder, summed across its Iron Butterfly rungs.

    Standard short Iron Butterfly math (Macroption / Fidelity):
      - Max Profit  = net credit collected
      - Max Loss    = strike width - net credit
      - Strike Width (= the wing distance) is the gross collateral per contract.
    Therefore  Max Profit + Max Loss == Strike Width, i.e. profit and loss both
    add up to the collateral.  Per rung we multiply by 100 (option multiplier) and
    by quantity, then sum across rungs.

    Alpaca's actual buying-power hold for a defined-risk fly equals the max loss
    (its "universal spread rule"), which we expose as `broker_margin`.
    """
    pairs = [(c, cr) for c, cr in zip(centers, credits) if cr is not None]
    if not pairs:
        return None

    mult = 100 * int(qty)
    n = len(pairs)

    # Mathematically calculated max/min: scan the true combined payoff and take its
    # actual peak and trough (matches the simulator graph). For the interlocking
    # ladder the peak is below the sum of credits, because SPY settles at one price.
    lo = min(c for c, _ in pairs) - wing - 4
    hi = max(c for c, _ in pairs) + wing + 4
    pts = 600

    def pl_at(s: float) -> float:
        return sum(cr - min(abs(s - c), wing) for c, cr in pairs) * mult

    vals = [pl_at(lo + i * (hi - lo) / pts) for i in range(pts + 1)]
    max_profit = max(vals)
    max_loss = min(vals)                 # negative
    collateral = wing * n * mult         # strike width x 100 x qty, per rung
    return {
        "max_profit": round(max_profit, 2),   # true peak of the combined payoff
        "max_loss": round(max_loss, 2),        # true trough (negative)
        "collateral": round(collateral, 2),    # strike width x 100
        "broker_margin": round(-max_loss, 2),  # defined-risk margin = max loss
        "credit_collected": round(sum(cr for _, cr in pairs) * mult, 2),
        "rungs_priced": n,
    }


def payoff_curve(centers: list[float], credits: list[float | None],
                 wing: float, qty: int, spot: float, points: int = 160) -> dict | None:
    """Combined expiration P/L of the whole ladder across a range of SPY prices.

    This is the true at-expiration payoff (sum of every rung's P/L at each price),
    suitable for plotting a Robinhood-style simulator curve. Returns the sampled
    curve, the price range, the breakeven crossings, and the P/L extremes.
    """
    pairs = [(c, cr) for c, cr in zip(centers, credits) if cr is not None]
    if not pairs:
        return None
    mult = 100 * int(qty)

    lo = min(c for c, _ in pairs) - wing - 4
    hi = max(c for c, _ in pairs) + wing + 4
    lo = min(lo, spot - 2)
    hi = max(hi, spot + 2)

    def pl_at(s: float) -> float:
        return sum(cr - min(abs(s - c), wing) for c, cr in pairs) * mult

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
        "centers": [c for c, _ in pairs],
    }
