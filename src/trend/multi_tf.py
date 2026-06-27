"""
multi_tf.py — Multi-Timeframe Trend Context (Gate 4 feature layer)

Feeds the LLM gate with HTF (15m) + LTF (5m) structural bias so it can
reason about trend alignment, not just the isolated FVG setup.

Data: Alpaca IEX free tier (same creds as execution). No new deps.
All functions pure. build_trend_context() never raises.
"""

import os
import math
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Literal

import requests

Bias   = Literal["BULL", "BEAR", "RANGE"]
Regime = Literal["TRENDING", "RANGING", "TRANSITION"]


@dataclass(frozen=True)
class TrendContext:
    htf_15m_bias: Bias
    ltf_5m_bias:  Bias
    alignment:    bool
    choch_15m:    bool
    choch_5m:     bool
    momentum_5m:  float
    regime:       Regime
    asof:         datetime

    def to_prompt_block(self) -> str:
        align = "ALIGNED" if self.alignment else "DIVERGENT"
        chs   = [x for x, on in (("15m", self.choch_15m), ("5m", self.choch_5m)) if on]
        choch = ", ".join(f"{c} CHoCH" for c in chs) if chs else "none"
        return (
            f"Multi-TF Trend ({self.asof.strftime('%H:%M UTC')}):\n"
            f"  HTF 15m : {self.htf_15m_bias}\n"
            f"  LTF  5m : {self.ltf_5m_bias}\n"
            f"  Alignment   : {align}\n"
            f"  CHoCH       : {choch}\n"
            f"  Momentum 5m : {self.momentum_5m:+.2f}\n"
            f"  Regime      : {self.regime}"
        )

    @classmethod
    def unavailable(cls) -> "TrendContext":
        return cls("RANGE", "RANGE", False, False, False, 0.0, "RANGING",
                   datetime.now(timezone.utc))


_BASE    = "https://data.alpaca.markets/v2"
_TIMEOUT = 8


def _fetch_bars(ticker: str, timeframe: str, limit: int) -> list:
    key    = os.getenv("ALPACA_API_KEY", "").strip()
    secret = os.getenv("ALPACA_API_SECRET", "").strip()
    if not key or not secret:
        raise RuntimeError("ALPACA_API_KEY / ALPACA_API_SECRET not set")
    r = requests.get(
        f"{_BASE}/stocks/{ticker}/bars",
        params={"timeframe": timeframe, "limit": limit, "feed": "iex", "sort": "asc"},
        headers={"APCA-API-KEY-ID": key, "APCA-API-SECRET-KEY": secret},
        timeout=_TIMEOUT,
    )
    r.raise_for_status()
    return r.json().get("bars") or []


def _ema(values, period):
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(v * k + out[-1] * (1 - k))
    return out


def _atr(bars, period):
    trs = []
    for i in range(1, len(bars)):
        h, l, pc = bars[i]["h"], bars[i]["l"], bars[i - 1]["c"]
        trs.append(max(h - l, abs(h - pc), abs(l - pc)))
    if len(trs) < period:
        return []
    atrs = [sum(trs[:period]) / period]
    for tr in trs[period:]:
        atrs.append((atrs[-1] * (period - 1) + tr) / period)
    return atrs


def _swing_bias(highs, lows):
    if len(highs) < 3:
        return "RANGE"
    if highs[-1] > highs[-2] > highs[-3] and lows[-1] > lows[-2] > lows[-3]:
        return "BULL"
    if lows[-1] < lows[-2] < lows[-3] and highs[-1] < highs[-2] < highs[-3]:
        return "BEAR"
    return "RANGE"


def _slope_bias(closes, period=20):
    e = _ema(closes, period)
    if len(e) < 3:
        return "RANGE"
    if e[-1] > e[-2] > e[-3]:
        return "BULL"
    if e[-1] < e[-2] < e[-3]:
        return "BEAR"
    return "RANGE"


def _combine(swing, slope):
    return swing if (swing == slope and swing != "RANGE") else "RANGE"


def _detect_choch(closes, highs, lows):
    if len(closes) < 4:
        return False
    prev = _swing_bias(highs[:-1], lows[:-1])
    if prev == "BULL":
        return closes[-1] < min(lows[-4:-1])
    if prev == "BEAR":
        return closes[-1] > max(highs[-4:-1])
    return False


def _momentum(closes, roc_p=5, std_p=20):
    if len(closes) < std_p + roc_p:
        return 0.0
    base = closes[-(roc_p + 1)]
    roc  = (closes[-1] - base) / base
    win  = closes[-std_p:]
    mean = sum(win) / len(win)
    var  = sum((x - mean) ** 2 for x in win) / len(win)
    std  = math.sqrt(var) if var > 0 else 1e-9
    return max(-1.0, min(1.0, (roc / (std / closes[-1])) * 0.5))


def _regime(bars):
    a14, a50 = _atr(bars, 14), _atr(bars, 50)
    if not a14 or not a50 or a50[-1] <= 0:
        return "RANGING"
    ratio = a14[-1] / a50[-1]
    if ratio > 1.15:
        return "TRENDING"
    if ratio < 0.85:
        return "RANGING"
    return "TRANSITION"


def build_trend_context(ticker: str) -> TrendContext:
    """2 Alpaca calls (~200ms). Returns .unavailable() on any failure."""
    try:
        b15 = _fetch_bars(ticker, "15Min", 60)
        b5  = _fetch_bars(ticker, "5Min", 60)
        if len(b15) < 20 or len(b5) < 20:
            return TrendContext.unavailable()

        c15 = [b["c"] for b in b15]; h15 = [b["h"] for b in b15]; l15 = [b["l"] for b in b15]
        c5  = [b["c"] for b in b5];  h5  = [b["h"] for b in b5];  l5  = [b["l"] for b in b5]

        bias15 = _combine(_swing_bias(h15, l15), _slope_bias(c15))
        bias5  = _combine(_swing_bias(h5, l5),  _slope_bias(c5))

        return TrendContext(
            htf_15m_bias=bias15,
            ltf_5m_bias=bias5,
            alignment=(bias15 == bias5 and bias15 != "RANGE"),
            choch_15m=_detect_choch(c15, h15, l15),
            choch_5m=_detect_choch(c5, h5, l5),
            momentum_5m=_momentum(c5),
            regime=_regime(b15),
            asof=datetime.now(timezone.utc),
        )
    except Exception:
        return TrendContext.unavailable()
