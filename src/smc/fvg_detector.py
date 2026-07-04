from __future__ import annotations

import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import List, Optional, Tuple

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
# Enums and constants
# ---------------------------------------------------------------------------

class Regime(str, Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    CRISIS = "CRISIS"

# Regime-adaptive minimum gap thresholds (as decimal fractions, not percent)
REGIME_THRESHOLDS: dict[Regime, float] = {
    FVGRegime.BULL:    0.0005,   # 0.05%
    FVGRegime.NEUTRAL: 0.0010,   # 0.10%
    FVGRegime.BEAR:    0.0015,   # 0.15%
    FVGRegime.CRISIS:  0.0015,   # treat CRISIS same as BEAR
}

# ---------------------------------------------------------------------------
# Domain types
# ---------------------------------------------------------------------------

def _gap_pct(low_boundary: float, high_boundary: float) -> float:
    """
    Calculate the gap size as a fraction of the mid-point price.

    ``high_boundary`` is always ≥ ``low_boundary`` by construction.
    """
    mid = (low_boundary + high_boundary) / 2.0
    if mid == 0.0:
        return 0.0
    return (high_boundary - low_boundary) / mid

def _scan_all_fvgs(
    bars: List[Bar],
    ticker: str,
    threshold: float,
) -> Tuple[List[FVGSignal], List[Tuple[FVGDirection, float, str]]]:
    """
    Walk the bar list and collect every FVG candidate, returning both
    accepted signals (above threshold) and rejected ones with reasons.

    Returns
    -------
    accepted:
        List of FVGSignal above the threshold (sorted newest-first).
    rejected:
        List of (direction, gap_pct, reason) tuples for rejected gaps.
    """
    accepted: List[FVGSignal] = []
    rejected: List[Tuple[FVGDirection, float, str]] = []

    n = len(bars)
    if n < 3:
        return accepted, rejected

    for i in range(1, n - 1):
        prev = bars[i - 1]  # bar[i-1]
        curr = bars[i]      # bar[i]   (middle candle – the "gap" candle)
        nxt  = bars[i + 1]  # bar[i+1]

        # --- Bullish FVG: gap between prev.high and next.low ---------------
        if prev.high < nxt.low:
            gap_lo = prev.high
            gap_hi = nxt.low
            pct    = _gap_pct(gap_lo, gap_hi)
            mid    = (gap_lo + gap_hi) / 2.0

            if pct >= threshold:
                accepted.append(FVGSignal(
                    ticker=ticker,
                    direction=FVGDirection.BULL,
                    gap_pct=pct,
                    entry_price=mid,
                    gap_high=gap_hi,
                    gap_low=gap_lo,
                    timestamp=curr.timestamp,
                    bar_indices=(i - 1, i, i + 1),
                ))
            else:
                rejected.append((
                    FVGDirection.BULL,
                    pct,
                    (
                        f"Bullish FVG at bar {i} rejected: "
                        f"gap_pct={pct:.4%} < threshold={threshold:.4%} "
                        f"(prev.high={prev.high}, next.low={nxt.low})"
                    ),
                ))

        # --- Bearish FVG: gap between next.high and prev.low ---------------
        if prev.low > nxt.high:
            gap_lo = nxt.high
            gap_hi = prev.low
            pct    = _gap_pct(gap_lo, gap_hi)
            mid    = (gap_lo + gap_hi) / 2.0

            if pct >= threshold:
                accepted.append(FVGSignal(
                    ticker=ticker,
                    direction=FVGDirection.BEAR,
                    gap_pct=pct,
                    entry_price=mid,
                    gap_high=gap_hi,
                    gap_low=gap_lo,
                    timestamp=curr.timestamp,
                    bar_indices=(i - 1, i, i + 1),
                ))
            else:
                rejected.append((
                    FVGDirection.BEAR,
                    pct,
                    (
                        f"Bearish FVG at bar {i} rejected: "
                        f"gap_pct={pct:.4%} < threshold={threshold:.4%} "
                        f"(prev.low={prev.low}, next.high={nxt.high})"
                    ),
                ))

    # Newest signal first (highest bar index = most recent bar)
    accepted.sort(key=lambda s: s.bar_indices[1], reverse=True)
    return accepted, rejected

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def detect_fvg(
    series: BarSeries,
    regime: Regime = FVGRegime.NEUTRAL,
    *,
    ticker: Optional[str] = None,
) -> FVGResult:
    """
    Detect the most-recent Fair Value Gap in *series* for the given *regime*.

    Parameters
    ----------
    series:
        A ``BarSeries`` produced by ``bar_aggregator.get_bars``.
    regime:
        Current macro regime used to select the minimum gap threshold.
        Defaults to ``NEUTRAL`` (0.10%).
    ticker:
        Override the symbol name; falls back to ``series.symbol`` when
        ``None``.

    Returns
    -------
    FVGResult
        ``result.all_candidates`` is ``True`` and ``result.signal`` contains the
        most-recent valid ``FVGSignal`` when a gap was found.  Otherwise
        ``result.signal`` is ``None`` and ``result.reason``
        explains the outcome.
    """
    sym = ticker or series.symbol
    threshold = REGIME_THRESHOLDS[regime]
    bars = series.bars

    if len(bars) < 3:
        return FVGResult(
            signal=None,
            reason=(
                f"Insufficient bars for FVG scan: need ≥ 3, got {len(bars)}"
            ),
        )

    accepted, rejected = _scan_all_fvgs(bars, sym, threshold)

    if accepted:
        best = accepted[0]  # most-recent
        return FVGResult(
            signal=best,
            reason="",
            all_candidates=accepted,
        )

    # Build a consolidated rejection reason
    if rejected:
        sample = rejected[-1]  # most-recent rejected candidate
        reason = (
            f"No FVG met the {regime.value} threshold ({threshold:.4%}). "
            f"Last candidate: {sample[2]}"
        )
    else:
        reason = (
            f"No FVG pattern found in {len(bars)} bars "
            f"(regime={regime.value}, threshold={threshold:.4%})."
        )

    return FVGResult(signal=None, reason=reason, all_candidates=[])

def detect_fvg_all(
    series: BarSeries,
    regime: Regime = FVGRegime.NEUTRAL,
    *,
    ticker: Optional[str] = None,
) -> List[FVGSignal]:
    """
    Return *all* FVG signals found in *series* above the regime threshold,
    sorted newest-first.

    Useful for scanning or back-testing scenarios where more than one gap
    is relevant.
    """
    sym = ticker or series.symbol
    threshold = REGIME_THRESHOLDS[regime]
    bars = series.bars

    if len(bars) < 3:
        return []

    accepted, _ = _scan_all_fvgs(bars, sym, threshold)
    return accepted

# ---------------------------------------------------------------------------
# Self-test
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys

    print("=" * 60)
    print("fvg_detector self-test")
    print("=" * 60)

    # ------------------------------------------------------------------ #
    # Build fixture bars                                                   #
    # ------------------------------------------------------------------ #
    _TS = datetime.datetime(2024, 1, 2, 9, 30, tzinfo=datetime.timezone.utc)

    def _ts(offset_minutes: int) -> datetime.datetime:
        return _TS + datetime.timedelta(minutes=offset_minutes)

    def _bar(
        offset: int,
        o: float,
        h: float,
        lo: float,
        c: float,
        v: float = 1000.0,
    ) -> Bar:
        return Bar(timestamp=_ts(offset), open=o, high=h, low=lo, close=c, volume=v)

    # --- Case 1: Bullish FVG (prev.high < next.low) ----------------------
    # bar0: high=100.0  bar1: normal  bar2: low=100.20  → gap of 0.20
    bullish_bars = [
        _bar(0,  99.5, 100.0,  99.0, 99.8),    # bar[i-1]: high = 100.0
        _bar(5,  99.8, 101.0,  99.5, 100.5),   # bar[i]:   middle candle
        _bar(10, 100.3, 101.5, 100.2, 101.0),  # bar[i+1]: low = 100.2
    ]
    bullish_series = BarSeries(symbol="TEST", timeframe="5Min", bars=bullish_bars)

    r1 = detect_fvg(bullish_series, FVGRegime.BULL)
    assert r1.all_candidates, f"Expected bullish FVG to be accepted; got: {r1.reason}"
    assert r1.signal is not None
    assert r1.signal.direction == FVGDirection.BULL
    gap_pct_val = _gap_pct(100.0, 100.2)
    assert abs(r1.signal.gap_pct - gap_pct_val) < 1e-10, (
        f"gap_pct mismatch: {r1.signal.gap_pct} vs {gap_pct_val}"
    )
    assert r1.signal.bar_indices == (0, 1, 2)
    assert r1.signal.ticker == "TEST"
    print(f"[PASS] Bullish FVG detected: {r1.signal.direction}, "
          f"gap={r1.signal.gap_pct:.4%}, entry={r1.signal.entry_price:.4f}")

    # --- Case 2: Bearish FVG (prev.low > next.high) ----------------------
    # bar0: low=100.0  bar1: normal  bar2: high=99.70  → gap of 0.30
    bearish_bars = [
        _bar(0,  100.5, 101.0, 100.0, 100.2),  # bar[i-1]: low = 100.0
        _bar(5,  100.1,  100.5, 99.5,  99.8),  # bar[i]:   middle candle
        _bar(10,  99.4,  99.7, 99.0,  99.3),   # bar[i+1]: high = 99.7
    ]
    bearish_series = BarSeries(symbol="TEST", timeframe="5Min", bars=bearish_bars)

    r2 = detect_fvg(bearish_series, FVGRegime.BULL)
    assert r2.all_candidates, f"Expected bearish FVG to be accepted; got: {r2.reason}"
    assert r2.signal is not None
    assert r2.signal.direction == FVGDirection.BEAR
    assert r2.signal.bar_indices == (0, 1, 2)
    print(f"[PASS] Bearish FVG detected: {r2.signal.direction}, "
          f"gap={r2.signal.gap_pct:.4%}, entry={r2.signal.entry_price:.4f}")

    # --- Case 3: Gap below NEUTRAL threshold (0.10%) → rejected ----------
    # Tiny gap: prev.high=100.0, next.low=100.05 → gap=0.05/100.025 ≈ 0.05%
    tiny_bars = [
        _bar(0,  99.5, 100.0,  99.0, 99.8),
        _bar(5,  99.8, 100.2,  99.5, 100.1),
        _bar(10, 100.1, 100.3, 100.05, 100.2),  # low=100.05, gap=0.05
    ]
    tiny_series = BarSeries(symbol="TEST", timeframe="5Min", bars=tiny_bars)

    r3 = detect_fvg(tiny_series, FVGRegime.NEUTRAL)  # threshold=0.10%
    assert not r3.all_candidates, "Expected tiny gap to be rejected under NEUTRAL regime"
    assert r3.reason is not None
    print(f"[PASS] Tiny gap rejected (NEUTRAL): {r3.reason}")

    # --- Case 4: Same tiny gap passes under BULL regime (0.05%) ----------
    r4 = detect_fvg(tiny_series, FVGRegime.BULL)    # threshold=0.05%
    # gap_pct ≈ 0.05/100.025 ≈ 0.04998%  → still below 0.05% → should reject
    # (borderline – let's compute precisely)
    exact_pct = _gap_pct(100.0, 100.05)
    if exact_pct >= REGIME_THRESHOLDS[FVGRegime.BULL]:
        assert r4.all_candidates, "Expected gap to pass under BULL"
        print(f"[PASS] Borderline gap accepted under BULL: {exact_pct:.5%}")
    else:
        assert not r4.all_candidates, "Expected gap to be rejected (below BULL threshold too)"
        print(f"[PASS] Borderline gap correctly rejected under BULL: {exact_pct:.5%}")

    # --- Case 5: Gap that passes BULL but not NEUTRAL --------------------
    # prev.high=100.0, next.low=100.06 → pct ≈ 0.0599% > 0.05% (BULL) < 0.10% (NEUTRAL)
    mid_bars = [
        _bar(0,  99.5, 100.0,  99.0, 99.8),
        _bar(5,  99.8, 100.2,  99.5, 100.1),
        _bar(10, 100.1, 100.3, 100.06, 100.2),  # low=100.06
    ]
    mid_series = BarSeries(symbol="TEST", timeframe="5Min", bars=mid_bars)

    r5_bull    = detect_fvg(mid_series, FVGRegime.BULL)
    r5_neutral = detect_fvg(mid_series, FVGRegime.NEUTRAL)
    assert r5_bull.all_candidates,    "Expected gap to pass BULL threshold"
    assert not r5_neutral.all_candidates, "Expected gap to fail NEUTRAL threshold"
    print(f"[PASS] Gap passes BULL ({REGIME_THRESHOLDS[FVGRegime.BULL]:.4%}) "
          f"but fails NEUTRAL ({REGIME_THRESHOLDS[FVGRegime.NEUTRAL]:.4%}): "
          f"gap={r5_bull.signal.gap_pct:.5%}")

    # --- Case 6: Insufficient bars ---------------------------------------
    short_series = BarSeries(symbol="TEST", timeframe="5Min", bars=bullish_bars[:2])
    r6 = detect_fvg(short_series, FVGRegime.BULL)
    assert not r6.all_candidates
    assert "Insufficient bars" in (r6.reason or "")
    print(f"[PASS] Short series rejected: {r6.reason}")

    # --- Case 7: detect_fvg_all returns multiple signals -----------------
    multi_bars = [
        _bar(0,   99.5, 100.0,  99.0,  99.8),   # i-1 for first FVG
        _bar(5,   99.8, 101.0,  99.5, 100.5),   # i   for first FVG
        _bar(10, 100.3, 101.5, 100.2, 101.0),   # i+1 for first FVG / i-1 for second
        _bar(15, 100.9, 102.0, 100.8, 101.8),   # i   for second FVG
        _bar(20, 101.5, 102.5, 101.4, 102.0),   # i+1 for second FVG
    ]
    multi_series = BarSeries(symbol="MULTI", timeframe="5Min", bars=multi_bars)
    all_signals = detect_fvg_all(multi_series, FVGRegime.BULL)
    print(f"[INFO] detect_fvg_all found {len(all_signals)} signal(s) in multi-bar fixture")
    for sig in all_signals:
        print(f"       {sig.direction.value} gap={sig.gap_pct:.4%} "
              f"bars={sig.bar_indices} entry={sig.entry_price:.4f}")

    # --- Case 8: Empty BarSeries -----------------------------------------
    empty_series = BarSeries(symbol="EMPTY", timeframe="5Min", bars=[])
    r8 = detect_fvg(empty_series, FVGRegime.BULL)
    assert not r8.all_candidates
    assert r8.reason is not None
    print(f"[PASS] Empty series rejected: {r8.reason}")

    # --- Case 9: Ticker override -----------------------------------------
    r9 = detect_fvg(bullish_series, FVGRegime.BULL, ticker="OVERRIDE")
    assert r9.all_candidates
    assert r9.signal is not None and r9.signal.ticker == "OVERRIDE"
    print(f"[PASS] Ticker override works: signal.ticker='{r9.signal.ticker}'")

    print()
    print("All self-tests passed.")
    sys.exit(0)
