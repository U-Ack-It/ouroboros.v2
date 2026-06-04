"""
Market Regime Detector — replaces VADER sentiment.

Uses two objective market signals instead of news text polarity:
  1. VIX (^VIX)    — volatility index; proxy for fear/greed
  2. SPY 200-day MA — trend filter; above = uptrend, below = downtrend

Regime classification:
  BULL    VIX < 18  AND  SPY > 200MA   → strong tailwind, score=1.0
  NEUTRAL VIX 18-25  OR  SPY near 200MA → mixed signals, score=0.5
  BEAR    VIX > 25   OR  SPY < 200MA   → headwind, score=0.0
  CRISIS  VIX > 35                     → block all longs,  score=-1.0

The snapshot is cached for 15 minutes — all symbols in a single heartbeat
scan share one regime read.  Refresh happens automatically on next fetch
after TTL expires.
"""

import json
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yfinance as yf


VIX_CRISIS  = 35.0
VIX_BEAR    = 25.0
VIX_NEUTRAL = 18.0

CACHE_TTL = timedelta(minutes=15)


@dataclass
class RegimeSnapshot:
    vix:         float
    spy_price:   float
    spy_ma200:   float
    above_ma200: bool
    label:       str    # "BULL" | "NEUTRAL" | "BEAR" | "CRISIS"
    score:       float  # 1.0 → -1.0
    fetched_at:  datetime

    def summary(self) -> str:
        trend = "↑ above" if self.above_ma200 else "↓ below"
        return (
            f"{self.label}  VIX={self.vix:.1f}  "
            f"SPY=${self.spy_price:.2f} ({trend} 200MA=${self.spy_ma200:.2f})  "
            f"score={self.score:.1f}"
        )


class MarketRegimeDetector:
    """
    Fetches VIX + SPY 200-day MA and classifies the current market regime.
    Falls back to NEUTRAL if data is unavailable (non-fatal).
    """

    def __init__(self):
        self._cache: Optional[RegimeSnapshot] = None

    def detect(self) -> RegimeSnapshot:
        if self._cache and (datetime.now() - self._cache.fetched_at) < CACHE_TTL:
            return self._cache
        snap = self._fetch()
        self._cache = snap
        self._write_snapshot(snap)
        return snap

    def is_safe(self, ticker: str) -> tuple[bool, float]:
        """
        Drop-in replacement for OuroborosSentiment.is_safe().
        Returns (safe, regime_score).  Only blocks in CRISIS.
        """
        snap = self.detect()
        if snap.label == "CRISIS":
            return False, snap.score
        return True, snap.score

    # ------------------------------------------------------------------

    def _fetch(self) -> RegimeSnapshot:
        try:
            vix = self._get_vix()
        except Exception:
            vix = 20.0  # assume neutral if unavailable

        try:
            spy_price, spy_ma200 = self._get_spy_ma()
        except Exception:
            spy_price, spy_ma200 = 500.0, 500.0  # treat as at-MA

        above = spy_price > spy_ma200

        if vix >= VIX_CRISIS:
            label, score = "CRISIS", -1.0
        elif vix >= VIX_BEAR or not above:
            label, score = "BEAR", 0.0
        elif vix >= VIX_NEUTRAL:
            label, score = "NEUTRAL", 0.5
        else:
            label, score = ("BULL", 1.0) if above else ("NEUTRAL", 0.5)

        return RegimeSnapshot(
            vix=round(vix, 2),
            spy_price=round(spy_price, 2),
            spy_ma200=round(spy_ma200, 2),
            above_ma200=above,
            label=label,
            score=score,
            fetched_at=datetime.now(),
        )

    def _get_vix(self) -> float:
        df = yf.Ticker("^VIX").history(period="3d", interval="1d")
        if df.empty:
            raise ValueError("VIX data empty")
        return float(df["Close"].iloc[-1])

    def _write_snapshot(self, snap: "RegimeSnapshot") -> None:
        out = Path("logs/regime_snapshot.json")
        out.parent.mkdir(exist_ok=True)
        try:
            out.write_text(json.dumps({
                "label":      snap.label,
                "score":      snap.score,
                "vix":        snap.vix,
                "fetched_at": snap.fetched_at.isoformat(),
                "spy_price":  snap.spy_price,
                "spy_ma200":  snap.spy_ma200,
                "above_ma200": snap.above_ma200,
            }))
        except Exception:
            pass

    def _get_spy_ma(self) -> tuple[float, float]:
        df = yf.Ticker("SPY").history(period="300d", interval="1d")
        if df.empty or len(df) < 10:
            raise ValueError("SPY data empty")
        price = float(df["Close"].iloc[-1])
        window = min(200, len(df))
        ma200 = float(df["Close"].rolling(window).mean().iloc[-1])
        return price, ma200
