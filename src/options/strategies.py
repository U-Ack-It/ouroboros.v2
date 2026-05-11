"""
Options strategy builders — Covered Calls and Protective Puts.

Each builder:
  1. Fetches the real options chain for the ticker
  2. Scans strikes around the target delta
  3. Returns a structured strategy idea with full P&L profile
"""

from dataclasses import dataclass
from typing import Optional

import yfinance as yf

from src.options.chain import load_chain, nearest_expiry, _get_spot, _dte, RISK_FREE_RATE
from src.options.pricing import greeks


# ---------------------------------------------------------------------------
# Data structures
# ---------------------------------------------------------------------------

@dataclass
class CoveredCallIdea:
    ticker: str
    underlying_price: float
    strike: float
    expiry: str
    dte: int
    bid: float
    ask: float
    mid: float
    bs_delta: float
    bs_iv: float
    # P&L profile (per 100-share lot / 1 contract)
    premium_received: float    # mid × 100
    premium_yield_pct: float   # premium / (underlying × 100) × 100
    max_profit: float          # premium + (strike - underlying) × 100 if called away
    max_profit_pct: float      # max_profit / (underlying × 100) × 100
    breakeven: float           # underlying - mid
    called_away_price: float   # = strike
    annualised_yield_pct: float


@dataclass
class ProtectivePutIdea:
    ticker: str
    underlying_price: float
    strike: float
    expiry: str
    dte: int
    bid: float
    ask: float
    mid: float
    bs_delta: float
    bs_iv: float
    # P&L profile (per 100-share lot / 1 contract)
    cost: float              # mid × 100
    cost_pct: float          # cost / (underlying × 100) × 100
    protected_below: float   # = strike
    max_loss: float          # (underlying - strike) × 100 + cost
    max_loss_pct: float
    breakeven_up: float      # underlying + mid (stock must rise to recover put cost)


# ---------------------------------------------------------------------------
# Covered Call builder
# ---------------------------------------------------------------------------

def build_covered_call(
    ticker: str,
    target_delta: float = 0.30,
    target_dte: int = 45,
    expiry: Optional[str] = None,
) -> Optional[CoveredCallIdea]:
    """
    Find the best OTM call to sell (closest to target_delta, above current price).
    Default: ~30 delta, ~45 DTE — the classic "wheel" sweet spot.
    """
    spot = _get_spot(ticker)
    if not spot:
        return None

    exp = expiry or nearest_expiry(ticker, target_dte)
    if not exp:
        return None

    try:
        calls, _ = load_chain(ticker, exp, spot)
    except Exception:
        return None

    # Only OTM calls (strike > spot), filter near-zero liquidity
    otm = calls[(calls["strike"] > spot) & (calls["bid"] > 0.01)].copy()
    if otm.empty:
        return None

    # Pick strike with bs_delta closest to target_delta (from below — OTM calls have delta < 0.5)
    otm["delta_dist"] = (otm["bs_delta"].abs() - target_delta).abs()
    row = otm.loc[otm["delta_dist"].idxmin()]

    K   = float(row["strike"])
    mid = float(row["mid"])
    dte = int(row["dte"])

    premium_received   = round(mid * 100, 2)
    underlying_cost    = round(spot * 100, 2)
    premium_yield_pct  = round(premium_received / underlying_cost * 100, 4)
    upside_captured    = round((K - spot) * 100, 2)
    max_profit         = round(premium_received + upside_captured, 2)
    max_profit_pct     = round(max_profit / underlying_cost * 100, 4)
    breakeven          = round(spot - mid, 4)
    ann_factor         = 365 / dte if dte > 0 else 0
    annualised_yield   = round(premium_yield_pct * ann_factor, 2)

    return CoveredCallIdea(
        ticker=ticker,
        underlying_price=round(spot, 4),
        strike=K,
        expiry=exp,
        dte=dte,
        bid=float(row["bid"]),
        ask=float(row["ask"]),
        mid=round(mid, 4),
        bs_delta=round(float(row["bs_delta"]), 4),
        bs_iv=round(float(row["bs_iv"]), 4),
        premium_received=premium_received,
        premium_yield_pct=premium_yield_pct,
        max_profit=max_profit,
        max_profit_pct=max_profit_pct,
        breakeven=breakeven,
        called_away_price=K,
        annualised_yield_pct=annualised_yield,
    )


# ---------------------------------------------------------------------------
# Protective Put builder
# ---------------------------------------------------------------------------

def build_protective_put(
    ticker: str,
    target_delta: float = 0.30,
    target_dte: int = 45,
    expiry: Optional[str] = None,
) -> Optional[ProtectivePutIdea]:
    """
    Find the best OTM put to buy for protection (closest to target_delta, below current price).
    Default: ~30 delta (|delta|), ~45 DTE.
    """
    spot = _get_spot(ticker)
    if not spot:
        return None

    exp = expiry or nearest_expiry(ticker, target_dte)
    if not exp:
        return None

    try:
        _, puts = load_chain(ticker, exp, spot)
    except Exception:
        return None

    # OTM puts (strike < spot), filter no-liquidity
    otm = puts[(puts["strike"] < spot) & (puts["ask"] > 0.01)].copy()
    if otm.empty:
        return None

    # For puts: bs_delta is negative — target abs(delta) ≈ target_delta
    otm["delta_dist"] = (otm["bs_delta"].abs() - target_delta).abs()
    row = otm.loc[otm["delta_dist"].idxmin()]

    K   = float(row["strike"])
    mid = float(row["mid"])
    dte = int(row["dte"])

    cost            = round(mid * 100, 2)
    underlying_cost = round(spot * 100, 2)
    cost_pct        = round(cost / underlying_cost * 100, 4)
    max_loss        = round((spot - K) * 100 + cost, 2)
    max_loss_pct    = round(max_loss / underlying_cost * 100, 4)
    breakeven_up    = round(spot + mid, 4)

    return ProtectivePutIdea(
        ticker=ticker,
        underlying_price=round(spot, 4),
        strike=K,
        expiry=exp,
        dte=dte,
        bid=float(row["bid"]),
        ask=float(row["ask"]),
        mid=round(mid, 4),
        bs_delta=round(float(row["bs_delta"]), 4),
        bs_iv=round(float(row["bs_iv"]), 4),
        cost=cost,
        cost_pct=cost_pct,
        protected_below=K,
        max_loss=max_loss,
        max_loss_pct=max_loss_pct,
        breakeven_up=breakeven_up,
    )


# ---------------------------------------------------------------------------
# Batch screener
# ---------------------------------------------------------------------------

def screen_covered_calls(
    tickers: list[str],
    target_delta: float = 0.30,
    target_dte: int = 45,
    min_annualised_yield: float = 5.0,
) -> list[CoveredCallIdea]:
    ideas = []
    for t in tickers:
        idea = build_covered_call(t, target_delta=target_delta, target_dte=target_dte)
        if idea and idea.annualised_yield_pct >= min_annualised_yield:
            ideas.append(idea)
    return sorted(ideas, key=lambda x: -x.annualised_yield_pct)


def screen_protective_puts(
    tickers: list[str],
    target_delta: float = 0.30,
    target_dte: int = 45,
    max_cost_pct: float = 3.0,
) -> list[ProtectivePutIdea]:
    ideas = []
    for t in tickers:
        idea = build_protective_put(t, target_delta=target_delta, target_dte=target_dte)
        if idea and idea.cost_pct <= max_cost_pct:
            ideas.append(idea)
    return sorted(ideas, key=lambda x: x.cost_pct)
