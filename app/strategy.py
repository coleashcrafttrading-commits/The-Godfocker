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
    """Combined expiration P/L for the whole ladder.

    Each rung's P/L at underlying price S is:  credit_i - min(|S - center_i|, wing)
    (an Iron Butterfly: max profit = credit at the center, loss capped at the wing).
    We scan S across the full range to find the combined max profit / max loss, and
    derive collateral (the broker buying-power hold = the position's max loss).
    """
    pairs = [(c, cr) for c, cr in zip(centers, credits) if cr is not None]
    if not pairs:
        return None

    mult = 100 * int(qty)
    lo = min(c for c, _ in pairs) - wing - 5
    hi = max(c for c, _ in pairs) + wing + 5

    best = float("-inf")
    best_price = None
    worst = float("inf")
    steps = int(round((hi - lo) / 0.05))
    for i in range(steps + 1):
        s = lo + i * 0.05
        pl = sum(cr - min(abs(s - c), wing) for c, cr in pairs)
        if pl > best:
            best, best_price = pl, s
        if pl < worst:
            worst = pl

    # Defined-risk: max loss happens in the tails where every wing is in the money.
    collateral = sum(wing - cr for _, cr in pairs) * mult
    total_credit = sum(cr for _, cr in pairs) * mult
    return {
        "max_profit": round(best * mult, 2),
        "max_profit_price": round(best_price, 2),
        "max_loss": round(worst * mult, 2),          # negative number
        "collateral": round(collateral, 2),           # buying-power hold
        "credit_collected": round(total_credit, 2),
        "rungs_priced": len(pairs),
    }
