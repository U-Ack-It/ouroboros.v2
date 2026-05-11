"""
Options chain loader — thin wrapper around yfinance.

Returns clean DataFrames with mid-price and BS-computed delta
added as additional columns.
"""

from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

from src.options.pricing import greeks, implied_vol

RISK_FREE_RATE = 0.045   # 4.5% — approximate 3-month T-bill rate


# ---------------------------------------------------------------------------
# Expiry helpers
# ---------------------------------------------------------------------------

def available_expiries(ticker: str) -> list[str]:
    try:
        return list(yf.Ticker(ticker).options)
    except Exception:
        return []


def nearest_expiry(ticker: str, target_dte: int = 45) -> Optional[str]:
    """
    Returns the available expiry date closest to target_dte calendar days out.
    Prefers >= target_dte to avoid very near-term gamma risk.
    """
    dates = available_expiries(ticker)
    if not dates:
        return None

    today = date.today()
    target = today + timedelta(days=target_dte)

    # Pick the closest date on-or-after the target; fall back to closest overall
    candidates = [d for d in dates if date.fromisoformat(d) >= today]
    if not candidates:
        return None

    return min(candidates, key=lambda d: abs((date.fromisoformat(d) - target).days))


# ---------------------------------------------------------------------------
# Chain loader
# ---------------------------------------------------------------------------

def load_chain(
    ticker: str,
    expiry: str,
    underlying_price: Optional[float] = None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """
    Returns (calls_df, puts_df) with extra columns:
      mid, dte, bs_delta, bs_iv (re-derived from mid price)

    bs_iv is the implied vol backed out from the mid price using Black-Scholes.
    """
    raw = yf.Ticker(ticker).option_chain(expiry)
    spot = underlying_price or _get_spot(ticker)
    T    = _dte(expiry) / 365.0

    def enrich(df: pd.DataFrame, flag: str) -> pd.DataFrame:
        df = df.copy()
        df["mid"] = ((df["bid"] + df["ask"]) / 2.0).round(4)
        df["dte"] = _dte(expiry)

        bs_deltas, bs_ivs = [], []
        for _, row in df.iterrows():
            K     = float(row["strike"])
            mid   = float(row["mid"])
            yf_iv = float(row.get("impliedVolatility", 0.0) or 0.0)
            sigma = yf_iv if yf_iv > 0 else 0.3

            try:
                g = greeks(spot, K, T, RISK_FREE_RATE, sigma, flag=flag)
                bs_deltas.append(g["delta"])
            except Exception:
                bs_deltas.append(0.0)

            iv = implied_vol(mid, spot, K, T, RISK_FREE_RATE, flag=flag) if mid > 0 else 0.0
            bs_ivs.append(iv)

        df["bs_delta"] = bs_deltas
        df["bs_iv"]    = bs_ivs
        return df

    calls = enrich(raw.calls, "call")
    puts  = enrich(raw.puts,  "put")
    return calls, puts


def _get_spot(ticker: str) -> float:
    try:
        return float(yf.Ticker(ticker).fast_info.last_price or 0.0)
    except Exception:
        return 0.0


def _dte(expiry_str: str) -> int:
    return max(0, (date.fromisoformat(expiry_str) - date.today()).days)
