"""
Crypto Data Feed — Binance public REST API.

No API key required for market data.
Returns pandas DataFrames matching yfinance column conventions
(Open, High, Low, Close, Volume) so FVG detection is identical.

Supported intervals: 1m 3m 5m 15m 30m 1h 2h 4h 6h 8h 12h 1d 3d 1w 1M
"""

import time
from datetime import datetime, timezone
from typing import Optional

import pandas as pd
import requests

BINANCE_BASE = "https://api.binance.com"
BINANCE_KLINES = f"{BINANCE_BASE}/api/v3/klines"
BINANCE_PRICE  = f"{BINANCE_BASE}/api/v3/ticker/price"

_SESSION = requests.Session()
_SESSION.headers.update({"User-Agent": "ouroboros-v2/1.0"})

# Fallback yfinance symbol map  (Binance symbol → yfinance ticker)
YF_FALLBACK = {
    "BTCUSDT":  "BTC-USD",
    "ETHUSDT":  "ETH-USD",
    "SOLUSDT":  "SOL-USD",
    "BNBUSDT":  "BNB-USD",
    "ADAUSDT":  "ADA-USD",
    "DOGEUSDT": "DOGE-USD",
    "XRPUSDT":  "XRP-USD",
    "AVAXUSDT": "AVAX-USD",
    "DOTUSDT":  "DOT-USD",
    "LINKUSDT": "LINK-USD",
}


def get_ohlcv(
    symbol: str,
    interval: str = "1h",
    limit: int = 100,
    use_fallback: bool = True,
) -> Optional[pd.DataFrame]:
    """
    Fetch OHLCV from Binance. Returns DataFrame with DatetimeIndex and
    columns: Open, High, Low, Close, Volume.
    Returns None on failure (logs to stderr via print).
    """
    try:
        resp = _SESSION.get(
            BINANCE_KLINES,
            params={"symbol": symbol, "interval": interval, "limit": limit},
            timeout=8,
        )
        resp.raise_for_status()
        raw = resp.json()
        if not raw:
            raise ValueError("empty response")

        df = pd.DataFrame(raw, columns=[
            "open_time", "Open", "High", "Low", "Close", "Volume",
            "close_time", "quote_vol", "num_trades",
            "taker_buy_base", "taker_buy_quote", "ignore",
        ])
        df["Open"]   = df["Open"].astype(float)
        df["High"]   = df["High"].astype(float)
        df["Low"]    = df["Low"].astype(float)
        df["Close"]  = df["Close"].astype(float)
        df["Volume"] = df["Volume"].astype(float)
        df.index = pd.to_datetime(df["open_time"], unit="ms", utc=True).dt.tz_convert(None)
        df = df[["Open", "High", "Low", "Close", "Volume"]]
        return df

    except Exception as exc:
        if use_fallback and symbol in YF_FALLBACK:
            return _yf_fallback(YF_FALLBACK[symbol], interval, limit)
        print(f"[crypto/feed] {symbol} fetch failed: {exc}")
        return None


def _yf_fallback(yf_ticker: str, interval: str, limit: int) -> Optional[pd.DataFrame]:
    try:
        import yfinance as yf
        # Map Binance intervals to yfinance intervals
        interval_map = {
            "1m": "1m", "5m": "5m", "15m": "15m", "30m": "30m",
            "1h": "1h", "4h": "1h", "1d": "1d",
        }
        yf_interval = interval_map.get(interval, "1h")
        period = "7d" if yf_interval in ("1m", "5m", "15m", "30m") else "60d"
        df = yf.Ticker(yf_ticker).history(period=period, interval=yf_interval)
        if df.empty:
            return None
        idx = pd.to_datetime(df.index)
        df.index = idx.tz_convert(None) if idx.tz is not None else idx
        return df[["Open", "High", "Low", "Close", "Volume"]].tail(limit)
    except Exception as exc:
        print(f"[crypto/feed] yfinance fallback {yf_ticker} failed: {exc}")
        return None


def get_last_price(symbol: str) -> float:
    """Returns the current price — Binance ticker with yfinance fallback."""
    try:
        resp = _SESSION.get(BINANCE_PRICE, params={"symbol": symbol}, timeout=5)
        resp.raise_for_status()
        return float(resp.json()["price"])
    except Exception:
        pass
    # Fallback: last close from a 1-candle yfinance fetch
    if symbol in YF_FALLBACK:
        try:
            import yfinance as yf
            fast = yf.Ticker(YF_FALLBACK[symbol]).fast_info
            price = float(getattr(fast, "last_price", 0.0) or 0.0)
            if price:
                return price
        except Exception:
            pass
    # Last resort: last Close from a fresh OHLCV fetch
    df = get_ohlcv(symbol, interval="1h", limit=1, use_fallback=True)
    if df is not None and not df.empty:
        return float(df.iloc[-1]["Close"])
    return 0.0


def binance_available() -> bool:
    """Quick connectivity check — returns True if Binance REST is reachable."""
    try:
        resp = _SESSION.get(f"{BINANCE_BASE}/api/v3/ping", timeout=4)
        return resp.status_code == 200
    except Exception:
        return False
