from __future__ import annotations

import datetime
import statistics
from dataclasses import dataclass
from enum import Enum
from typing import List, Optional, Sequence

try:
    from .types import (
        FVGDirection, Bias, Alignment, TrendRegime, FVGRegime,
        LLMVerdict, FVGSignal, FVGResult, FVGBoundaries, TrendContext,
        GateResult, Gate4Result, PipelineResult, PositionSpec,
    )
except ImportError:
    from smc.types import (
        FVGDirection, Bias, Alignment, TrendRegime, FVGRegime,
        LLMVerdict, FVGSignal, FVGResult, FVGBoundaries, TrendContext,
        GateResult, Gate4Result, PipelineResult, PositionSpec,
    )

try:
    from .bar_aggregator import Bar, BarSeries
except ImportError:
    from bar_aggregator import Bar, BarSeries

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------

# removed: use TrendRegime from types.py
    TRENDING = "TRENDING"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"

# Also expose FVG direction strings so callers can pass them directly

def _ema(values: Sequence[float], period: int) -> List[float]:
    """
    Compute Exponential Moving Average.

    Returns a list of the same length; initial values where the window is not
    yet full are initialised with the SMA of available points.
    """
    if not values:
        return []
    k = 2.0 / (period + 1)
    result: List[float] = []
    for i, v in enumerate(values):
        if i == 0:
            result.append(v)
        else:
            result.append(v * k + result[-1] * (1 - k))
    return result

def _atr(bars: Sequence[Bar], period: int = 14) -> float:
    """
    Average True Range over the last *period* bars.

    Returns 0.0 when the series is too short to compute.
    """
    if len(bars) < 2:
        return 0.0

    trs: List[float] = []
    for i in range(1, len(bars)):
        prev_close = bars[i - 1].close
        high = bars[i].high
        low = bars[i].low
        tr = max(high - low, abs(high - prev_close), abs(low - prev_close))
        trs.append(tr)

    window = trs[-period:] if len(trs) >= period else trs
    return statistics.mean(window) if window else 0.0

def _swing_highs_lows(
    bars: Sequence[Bar],
    lookback: int = 3,
) -> tuple[List[float], List[float]]:
    """
    Return lists of swing-high prices and swing-low prices.

    A swing high at index *i* means bars[i].high is the highest in the
    surrounding *lookback* bars on each side.  Likewise for swing lows.
    """
    n = len(bars)
    swing_highs: List[float] = []
    swing_lows: List[float] = []

    for i in range(lookback, n - lookback):
        window_highs = [bars[j].high for j in range(i - lookback, i + lookback + 1)]
        window_lows  = [bars[j].low  for j in range(i - lookback, i + lookback + 1)]
        if bars[i].high == max(window_highs):
            swing_highs.append(bars[i].high)
        if bars[i].low == min(window_lows):
            swing_lows.append(bars[i].low)

    return swing_highs, swing_lows

def _bias_from_bars(bars: Sequence[Bar]) -> Bias:
    """
    Determine directional bias using EMA slope + swing structure.

    Rules
    -----
    * Uses a fast EMA(8) and slow EMA(21).
    * If fast > slow at the end of the series: initial lean is BULL.
    * If fast < slow: initial lean is BEAR.
    * Swing structure confirms or demotes to RANGE when highs/lows are mixed.
    """
    if len(bars) < 5:
        return Bias.RANGE

    closes = [b.close for b in bars]
    fast_ema = _ema(closes, 8)
    slow_ema = _ema(closes, 21)

    # EMA relationship at the last bar
    ema_diff = fast_ema[-1] - slow_ema[-1]
    price_range = max(b.high for b in bars) - min(b.low for b in bars)
    # Normalise: treat as RANGE if the EMA spread is negligible relative to range
    if price_range == 0:
        return Bias.RANGE
    relative_spread = abs(ema_diff) / price_range

    if relative_spread < 0.02:
        return Bias.RANGE

    ema_bias = Bias.BULL if ema_diff > 0 else Bias.BEAR

    # Swing structure confirmation
    lookback = max(2, len(bars) // 10)
    swing_highs, swing_lows = _swing_highs_lows(bars, lookback=lookback)

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        # Higher highs AND higher lows → BULL
        hh = swing_highs[-1] > swing_highs[-2]
        hl = swing_lows[-1]  > swing_lows[-2]
        lh = swing_highs[-1] < swing_highs[-2]
        ll = swing_lows[-1]  < swing_lows[-2]

        if hh and hl:
            structure_bias = Bias.BULL
        elif lh and ll:
            structure_bias = Bias.BEAR
        else:
            structure_bias = Bias.RANGE

        # Both must agree for a strong bias
        if structure_bias == ema_bias:
            return ema_bias
        if structure_bias == Bias.RANGE:
            return ema_bias  # trust EMA when structure is ambiguous
        # Disagreement → RANGE
        return Bias.RANGE

    # Not enough swings: fall back to EMA alone
    return ema_bias

def _detect_choch(bars: Sequence[Bar]) -> bool:
    """
    Detect a Change of Character (CHoCH).

    A CHoCH occurs when the most recent swing structure breaks the prior
    directional assumption:
    - In a series with prior higher-highs, price makes a lower low below
      the second-to-last swing low.
    - In a series with prior lower-lows, price makes a higher high above
      the second-to-last swing high.

    Returns ``True`` when a CHoCH is detected.
    """
    if len(bars) < 10:
        return False

    lookback = max(2, len(bars) // 10)
    swing_highs, swing_lows = _swing_highs_lows(bars, lookback=lookback)

    last_close = bars[-1].close

    if len(swing_highs) >= 2 and len(swing_lows) >= 2:
        prior_hh = swing_highs[-2] < swing_highs[-1]  # was making higher highs
        prior_ll = swing_lows[-2]  > swing_lows[-1]   # was making lower lows

        # CHoCH bearish: was bullish (HH), now breaks below prior swing low
        if prior_hh and last_close < swing_lows[-2]:
            return True
        # CHoCH bullish: was bearish (LL), now breaks above prior swing high
        if prior_ll and last_close > swing_highs[-2]:
            return True

    return False

def _momentum(bars: Sequence[Bar]) -> float:
    """
    Normalised momentum in [-1.0, +1.0].

    Uses the Rate-of-Change (ROC) of the last N closes vs the midpoint of
    the series, normalised to the overall price range.
    """
    if len(bars) < 2:
        return 0.0

    closes = [b.close for b in bars]
    price_range = max(b.high for b in bars) - min(b.low for b in bars)

    if price_range == 0:
        return 0.0

    # Compare last close to first close
    roc = closes[-1] - closes[0]
    # Normalise: clamp to [-1, 1]
    normalised = roc / price_range
    return max(-1.0, min(1.0, normalised))

def _regime_from_bars(bars: Sequence[Bar], atr_ratio_threshold: float = 1.5) -> TrendRegime:
    """
    Classify market regime using ATR ratio: recent ATR vs. longer ATR.

    * recent_atr / long_atr > threshold  → VOLATILE
    * Trending: EMA(8) consistently on one side of EMA(21)
    * Otherwise → RANGING
    """
    if len(bars) < 5:
        return TrendRegime.RANGING

    recent_atr = _atr(bars, period=7)
    long_atr    = _atr(bars, period=min(20, len(bars) - 1))

    if long_atr > 0 and (recent_atr / long_atr) > atr_ratio_threshold:
        return TrendRegime.VOLATILE

    closes = [b.close for b in bars]
    fast_ema = _ema(closes, 8)
    slow_ema = _ema(closes, 21)

    # Check the last quarter of the series for EMA consistency
    quarter = max(1, len(fast_ema) // 4)
    diffs = [fast_ema[i] - slow_ema[i] for i in range(len(fast_ema) - quarter, len(fast_ema))]

    all_positive = all(d > 0 for d in diffs)
    all_negative = all(d < 0 for d in diffs)

    if all_positive or all_negative:
        return TrendRegime.TRENDING

    return TrendRegime.RANGING

def _compute_alignment(
    htf_bias: Bias,
    ltf_bias: Bias,
    fvg_direction: Optional[str],
) -> Alignment:
    """
    Compute alignment between the two timeframes and the FVG direction.

    Spec:
    * WITH    = htf and ltf agree AND match FVG direction
    * AGAINST = htf and ltf agree AND disagree with FVG direction
    * NEUTRAL = mixed / range or no FVG direction provided
    """
    # Normalise FVG direction
    if fvg_direction is not None:
        fvg_upper = fvg_direction.upper()
        fvg_is_bull: Optional[bool] = fvg_upper in ("BULLISH", "BULL")
        fvg_is_bear: Optional[bool] = fvg_upper in ("BEARISH", "BEAR")
        if not (fvg_is_bull or fvg_is_bear):
            fvg_direction = None  # unknown direction → NEUTRAL

    # HTF/LTF must agree and not be RANGE for a directional alignment
    if htf_bias == Bias.RANGE or ltf_bias == Bias.RANGE:
        return Alignment.NEUTRAL

    if htf_bias != ltf_bias:
        return Alignment.NEUTRAL

    # HTF == LTF, both directional
    if fvg_direction is None:
        # No FVG direction to compare against; bias is aligned but we can't
        # say WITH/AGAINST without the FVG, so return NEUTRAL
        return Alignment.NEUTRAL

    fvg_bull = fvg_direction.upper() in ("BULLISH", "BULL")
    trend_bull = htf_bias == Bias.BULL

    if fvg_bull == trend_bull:
        return Alignment.WITH
    else:
        return Alignment.AGAINST

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def analyze_trend(
    htf_bars: BarSeries,
    ltf_bars: BarSeries,
    fvg_direction: Optional[str] = None,
) -> TrendContext:
    """
    Produce a ``TrendContext`` from higher-timeframe and lower-timeframe bars.

    Parameters
    ----------
    htf_bars:
        15-minute (or higher) bar series used for macro bias.
    ltf_bars:
        5-minute bar series used for entry-level bias.
    fvg_direction:
        Optional FVG direction string: ``"BULLISH"`` / ``"BULL"`` or
        ``"BEARISH"`` / ``"BEAR"``.  Used to compute *alignment*.
        When ``None``, alignment will be ``NEUTRAL``.

    Returns
    -------
    TrendContext
        Frozen dataclass with all required fields populated.
    """
    htf_seq: Sequence[Bar] = htf_bars.bars
    ltf_seq: Sequence[Bar] = ltf_bars.bars

    htf_bias = _bias_from_bars(htf_seq)
    ltf_bias = _bias_from_bars(ltf_seq)

    alignment = _compute_alignment(htf_bias, ltf_bias, fvg_direction)

    choch = _detect_choch(ltf_seq)  # CHoCH is an entry-timeframe signal

    mom = _momentum(ltf_seq)

    # Regime from the lower timeframe (captures short-term volatility)
    regime = _regime_from_bars(ltf_seq)

    return TrendContext(
        htf_bias=htf_bias,
        ltf_bias=ltf_bias,
        alignment=alignment,
        choch=choch,
        momentum=mom,
        regime=regime,
    )

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=== trend_engine selftest ===")

    BASE_TS = datetime.datetime(2024, 1, 2, 9, 30, tzinfo=datetime.timezone.utc)

    def _make_bar(i: int, o: float, h: float, l: float, c: float, v: float = 10000.0) -> Bar:
        return Bar(
            timestamp=BASE_TS + datetime.timedelta(minutes=5 * i),
            open=o, high=h, low=l, close=c, volume=v,
        )

    # ------------------------------------------------------------------
    # Fixture 1: clear uptrend on both timeframes
    # ------------------------------------------------------------------
    def _bullish_series(n: int, start: float = 100.0, step: float = 0.30) -> List[Bar]:
        bars: List[Bar] = []
        price = start
        for i in range(n):
            o = price
            c = price + step
            h = c + 0.10
            l = o - 0.05
            bars.append(_make_bar(i, o, h, l, c))
            price = c
        return bars

    # ------------------------------------------------------------------
    # Fixture 2: clear downtrend on both timeframes
    # ------------------------------------------------------------------
    def _bearish_series(n: int, start: float = 120.0, step: float = 0.30) -> List[Bar]:
        bars: List[Bar] = []
        price = start
        for i in range(n):
            o = price
            c = price - step
            h = o + 0.05
            l = c - 0.10
            bars.append(_make_bar(i, o, h, l, c))
            price = c
        return bars

    # ------------------------------------------------------------------
    # Fixture 3: choppy / sideways
    # ------------------------------------------------------------------
    def _ranging_series(n: int, base: float = 110.0) -> List[Bar]:
        import math
        bars: List[Bar] = []
        for i in range(n):
            c = base + math.sin(i * 0.5) * 0.5
            o = base + math.sin((i - 1) * 0.5) * 0.5
            h = max(o, c) + 0.10
            l = min(o, c) - 0.10
            bars.append(_make_bar(i, o, h, l, c))
        return bars

    errors: List[str] = []

    # ---------------------------------------------------------------
    # Test A: WITH alignment (bull HTF + bull LTF + BULLISH FVG)
    # ---------------------------------------------------------------
    htf_a = BarSeries("TEST", "15Min", _bullish_series(40, start=100.0, step=0.5))
    ltf_a = BarSeries("TEST", "5Min",  _bullish_series(60, start=100.0, step=0.3))
    ctx_a = analyze_trend(htf_a, ltf_a, fvg_direction="BULLISH")

    print(f"[A] TrendContext: {ctx_a}")
    assert ctx_a.htf_bias == Bias.BULL, f"[A] htf_bias expected BULL, got {ctx_a.htf_bias}"
    assert ctx_a.ltf_bias == Bias.BULL, f"[A] ltf_bias expected BULL, got {ctx_a.ltf_bias}"
    assert ctx_a.alignment == Alignment.WITH, f"[A] alignment expected WITH, got {ctx_a.alignment}"
    assert -1.0 <= ctx_a.momentum <= 1.0, f"[A] momentum out of range: {ctx_a.momentum}"
    assert isinstance(ctx_a.choch, bool), "[A] choch must be bool"
    assert isinstance(ctx_a.regime, TrendRegime), "[A] regime must be Regime enum"
    print("  [A] PASS: bull/bull/BULLISH → WITH")

    # ---------------------------------------------------------------
    # Test B: AGAINST alignment (bull HTF + bull LTF + BEARISH FVG)
    # ---------------------------------------------------------------
    htf_b = BarSeries("TEST", "15Min", _bullish_series(40))
    ltf_b = BarSeries("TEST", "5Min",  _bullish_series(60))
    ctx_b = analyze_trend(htf_b, ltf_b, fvg_direction="BEARISH")

    print(f"[B] TrendContext: {ctx_b}")
    assert ctx_b.alignment == Alignment.AGAINST, f"[B] alignment expected AGAINST, got {ctx_b.alignment}"
    print("  [B] PASS: bull/bull/BEARISH → AGAINST")

    # ---------------------------------------------------------------
    # Test C: NEUTRAL when range detected
    # ---------------------------------------------------------------
    htf_c = BarSeries("TEST", "15Min", _ranging_series(40))
    ltf_c = BarSeries("TEST", "5Min",  _ranging_series(60))
    ctx_c = analyze_trend(htf_c, ltf_c, fvg_direction="BULLISH")

    print(f"[C] TrendContext: {ctx_c}")
    # Range series → htf/ltf bias likely RANGE → alignment NEUTRAL
    assert ctx_c.alignment == Alignment.NEUTRAL, (
        f"[C] alignment expected NEUTRAL for ranging market, got {ctx_c.alignment}"
    )
    print("  [C] PASS: ranging/ranging → NEUTRAL")

    # ---------------------------------------------------------------
    # Test D: NEUTRAL when no FVG direction provided
    # ---------------------------------------------------------------
    htf_d = BarSeries("TEST", "15Min", _bullish_series(40))
    ltf_d = BarSeries("TEST", "5Min",  _bullish_series(60))
    ctx_d = analyze_trend(htf_d, ltf_d, fvg_direction=None)

    print(f"[D] TrendContext: {ctx_d}")
    assert ctx_d.alignment == Alignment.NEUTRAL, (
        f"[D] alignment expected NEUTRAL when no FVG dir, got {ctx_d.alignment}"
    )
    print("  [D] PASS: no FVG direction → NEUTRAL")

    # ---------------------------------------------------------------
    # Test E: AGAINST for bear/bear/BULLISH
    # ---------------------------------------------------------------
    htf_e = BarSeries("TEST", "15Min", _bearish_series(40))
    ltf_e = BarSeries("TEST", "5Min",  _bearish_series(60))
    ctx_e = analyze_trend(htf_e, ltf_e, fvg_direction="BULLISH")

    print(f"[E] TrendContext: {ctx_e}")
    assert ctx_e.htf_bias == Bias.BEAR, f"[E] htf_bias expected BEAR, got {ctx_e.htf_bias}"
    assert ctx_e.ltf_bias == Bias.BEAR, f"[E] ltf_bias expected BEAR, got {ctx_e.ltf_bias}"
    assert ctx_e.alignment == Alignment.AGAINST, f"[E] alignment expected AGAINST, got {ctx_e.alignment}"
    print("  [E] PASS: bear/bear/BULLISH → AGAINST")

    # ---------------------------------------------------------------
    # Test F: WITH for bear/bear/BEARISH
    # ---------------------------------------------------------------
    htf_f = BarSeries("TEST", "15Min", _bearish_series(40))
    ltf_f = BarSeries("TEST", "5Min",  _bearish_series(60))
    ctx_f = analyze_trend(htf_f, ltf_f, fvg_direction="BEARISH")

    print(f"[F] TrendContext: {ctx_f}")
    assert ctx_f.alignment == Alignment.WITH, f"[F] alignment expected WITH, got {ctx_f.alignment}"
    print("  [F] PASS: bear/bear/BEARISH → WITH")

    # ---------------------------------------------------------------
    # Test G: Empty bars → graceful RANGE/NEUTRAL
    # ---------------------------------------------------------------
    htf_g = BarSeries("TEST", "15Min", [])
    ltf_g = BarSeries("TEST", "5Min",  [])
    ctx_g = analyze_trend(htf_g, ltf_g, fvg_direction="BULLISH")

    print(f"[G] TrendContext: {ctx_g}")
    assert ctx_g.htf_bias == Bias.RANGE
    assert ctx_g.ltf_bias == Bias.RANGE
    assert ctx_g.alignment == Alignment.NEUTRAL
    assert ctx_g.choch is False
    assert ctx_g.momentum == 0.0
    print("  [G] PASS: empty bars → graceful defaults")

    # ---------------------------------------------------------------
    # Test H: Momentum range check on a mixed series
    # ---------------------------------------------------------------
    mixed = _bullish_series(30) + _bearish_series(10, start=_bullish_series(30)[-1].close)
    ltf_h = BarSeries("TEST", "5Min", mixed)
    htf_h = BarSeries("TEST", "15Min", _bullish_series(20))
    ctx_h = analyze_trend(htf_h, ltf_h)
    assert -1.0 <= ctx_h.momentum <= 1.0, f"[H] momentum out of range: {ctx_h.momentum}"
    print(f"  [H] PASS: momentum={ctx_h.momentum:.3f} in [-1, 1]")

    if errors:
        print("\nFAILED:")
        for e in errors:
            print(f"  {e}")
        sys.exit(1)
    else:
        print("\nAll selftest assertions passed.")
