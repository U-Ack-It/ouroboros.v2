"""
Crypto FVG Scanner

Applies the same 3-candle SMC Fair Value Gap logic as scanner.py but
sources data from Binance and uses 24/7-aware session labelling.

Crypto sessions (UTC):
  asia     00:00 – 07:59   (Tokyo/Singapore active)
  europe   08:00 – 15:59   (London open, EU liquidity)
  americas 16:00 – 23:59   (NY open, US retail peak)
"""

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Optional

from src.crypto.feed import get_ohlcv, get_last_price


# ---------------------------------------------------------------------------
# Session
# ---------------------------------------------------------------------------

CRYPTO_SESSIONS = {
    "asia":     (0,  7),
    "europe":   (8,  15),
    "americas": (16, 23),
}


def crypto_session(dt: Optional[datetime] = None) -> str:
    hour = (dt or datetime.now(timezone.utc)).hour
    for name, (start, end) in CRYPTO_SESSIONS.items():
        if start <= hour <= end:
            return name
    return "americas"


# ---------------------------------------------------------------------------
# FVG detection (mirrors equities logic exactly)
# ---------------------------------------------------------------------------

@dataclass
class CryptoFVG:
    symbol: str
    fvg_type: str         # BULL_FVG | BEAR_FVG
    direction: str        # LONG | SHORT
    gap_size: float       # absolute price gap
    gap_pct: float        # gap as % of entry price
    entry_price: float    # fill level (c2 midpoint)
    last_price: float
    session: str
    interval: str
    ts: str               # candle timestamp (c3)


def scan_symbol(
    symbol: str,
    interval: str = "1h",
    min_gap_pct: float = 0.0,
) -> Optional[CryptoFVG]:
    """
    Fetches the last 5 candles and checks the 3-candle FVG pattern.
    min_gap_pct: skip FVGs smaller than this % of price (noise filter).
    Returns CryptoFVG or None.
    """
    df = get_ohlcv(symbol, interval=interval, limit=5)
    if df is None or len(df) < 3:
        return None

    c1 = df.iloc[-3]
    c2 = df.iloc[-2]
    c3 = df.iloc[-1]

    fvg_type = direction = None
    gap_size = entry_price = 0.0

    # Bullish FVG: c3.Low > c1.High
    if c3["Low"] > c1["High"]:
        gap_size    = c3["Low"] - c1["High"]
        entry_price = c2["Low"]
        fvg_type    = "BULL_FVG"
        direction   = "LONG"

    # Bearish FVG: c3.High < c1.Low
    elif c3["High"] < c1["Low"]:
        gap_size    = c1["Low"] - c3["High"]
        entry_price = c2["High"]
        fvg_type    = "BEAR_FVG"
        direction   = "SHORT"

    if fvg_type is None:
        return None

    gap_pct = gap_size / entry_price * 100 if entry_price else 0.0
    if gap_pct < min_gap_pct:
        return None

    last_price = get_last_price(symbol)
    session    = crypto_session()

    return CryptoFVG(
        symbol=symbol,
        fvg_type=fvg_type,
        direction=direction,
        gap_size=round(gap_size, 6),
        gap_pct=round(gap_pct, 4),
        entry_price=round(entry_price, 6),
        last_price=last_price,
        session=session,
        interval=interval,
        ts=str(df.index[-1]),
    )


# ---------------------------------------------------------------------------
# Multi-symbol scan (sync — called from async executor in crypto_monitor)
# ---------------------------------------------------------------------------

def scan_all_crypto(
    symbols: list[str],
    interval: str = "1h",
    min_gap_pct: float = 0.0,
) -> list[tuple[str, Optional[CryptoFVG], Optional[str]]]:
    """
    Returns list of (symbol, fvg_or_None, error_or_None).
    """
    results = []
    for sym in symbols:
        try:
            fvg = scan_symbol(sym, interval=interval, min_gap_pct=min_gap_pct)
            results.append((sym, fvg, None))
        except Exception as exc:
            results.append((sym, None, str(exc)))
    return results
