"""
src/smc/position_sizer.py

Regime-adaptive position sizer. Pure function: no I/O, no API calls.

Sizing rules (from spec):
  BULL    = $450 base
  NEUTRAL = $350 base
  BEAR    = $250 base
  CRISIS  = $0   (no new entries)

If Gate 4 verdict is CAUTION, apply 40% discount to base dollar amount.
Entry/TP/SL derived from FVGBoundaries (gap_top / gap_bottom).
Returns PositionSpec(shares, entry, tp, sl, dollar_size).
"""
from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------
REGIME_DOLLAR_LIMITS: dict[str, float] = {
    "BULL":    450.0,
    "NEUTRAL": 350.0,
    "BEAR":    250.0,
    "CRISIS":  0.0,
}

CAUTION_DISCOUNT: float = 0.40   # 40% reduction when Gate 4 says CAUTION


def _zero_spec() -> PositionSpec:
    return PositionSpec(shares=0, entry=0.0, tp=0.0, sl=0.0, dollar_size=0.0)


def _entry_tp_sl(
    fvg: FVGBoundaries,
    direction: str,
) -> tuple[float, float, float]:
    """
    Derive entry, TP, SL from FVG boundaries.

    BULL:  entry = gap_bottom (conservative), TP = gap_top, SL = entry - gap_width
    BEAR:  entry = gap_top   (conservative), TP = gap_bottom, SL = entry + gap_width
    """
    gap_width = abs(fvg.gap_top - fvg.gap_bottom)
    d = direction.upper()

    if d == "BULL":
        raw_entry = fvg.gap_bottom
        # Honour a more conservative signal entry if provided
        entry = fvg.entry_price if fvg.entry_price > 0 else raw_entry
        tp = fvg.gap_top
        sl = round(raw_entry - gap_width, 4)
    else:   # BEAR
        raw_entry = fvg.gap_top
        entry = fvg.entry_price if fvg.entry_price > 0 else raw_entry
        tp = fvg.gap_bottom
        sl = round(raw_entry + gap_width, 4)

    return entry, tp, sl


def compute_position(
    regime: str,
    pipeline_result: PipelineResult,
    fvg: FVGBoundaries | None = None,
    direction: str = "BULL",
) -> PositionSpec:
    """
    Compute position sizing from regime + gate pipeline result.

    Parameters
    ----------
    regime:          Macro regime string (BULL/NEUTRAL/BEAR/CRISIS).
    pipeline_result: Output of gate_pipeline.run_gates().
    fvg:             FVG boundary data for entry/TP/SL. If None, returns zero spec.
    direction:       Trade direction (BULL or BEAR).

    Returns
    -------
    PositionSpec. shares=0 if regime is CRISIS, pipeline rejected, or FVG is None.
    """
    # Hard blocks
    r = regime.upper()
    if r == "CRISIS":
        return _zero_spec()
    if not pipeline_result.passed:
        return _zero_spec()
    if fvg is None:
        return _zero_spec()

    # Base dollar size from regime
    base_dollars = REGIME_DOLLAR_LIMITS.get(r, 350.0)

    # Apply CAUTION discount if Gate 4 said so
    if (pipeline_result.gate4 is not None
            and pipeline_result.gate4.verdict == LLMVerdict.CAUTION):
        base_dollars *= (1.0 - CAUTION_DISCOUNT)

    if base_dollars <= 0:
        return _zero_spec()

    entry, tp, sl = _entry_tp_sl(fvg, direction)
    if entry <= 0:
        return _zero_spec()

    shares = int(base_dollars / entry)
    if shares <= 0:
        return _zero_spec()

    return PositionSpec(
        shares=shares,
        entry=round(entry, 4),
        tp=round(tp, 4),
        sl=sl,
        dollar_size=round(base_dollars, 2),
    )


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("position_sizer self-test")
    print("=" * 60)

    bull_fvg = FVGBoundaries(gap_top=155.00, gap_bottom=150.00, entry_price=150.50)
    bear_fvg = FVGBoundaries(gap_top=200.00, gap_bottom=195.00, entry_price=199.50)

    approve_g4 = Gate4Result(verdict=LLMVerdict.APPROVE, confidence=0.85)
    caution_g4 = Gate4Result(verdict=LLMVerdict.CAUTION, confidence=0.55)
    reject_g4  = Gate4Result(verdict=LLMVerdict.REJECT,  confidence=0.90)

    passed_result  = PipelineResult(passed=True,  gate4=approve_g4)
    caution_result = PipelineResult(passed=True,  gate4=caution_g4)
    reject_result  = PipelineResult(passed=False, gate4=reject_g4)

    # Test 1: BULL / APPROVE
    r1 = compute_position("BULL", passed_result, fvg=bull_fvg, direction="BULL")
    assert r1.shares > 0, f"Expected shares>0, got {r1.shares}"
    expected = int(450.0 / 150.50)
    assert r1.shares == expected, f"Expected {expected}, got {r1.shares}"
    assert r1.tp == 155.00
    assert r1.entry == 150.50
    print(f"[PASS] Test 1 – BULL/APPROVE: {r1.shares} shares @ {r1.entry}, TP={r1.tp}, SL={r1.sl}")

    # Test 2: NEUTRAL / CAUTION (40% discount)
    r2 = compute_position("NEUTRAL", caution_result, fvg=bull_fvg, direction="BULL")
    discounted = 350.0 * (1.0 - CAUTION_DISCOUNT)   # 210.0
    expected2  = int(discounted / 150.50)
    assert r2.shares == expected2, f"Expected {expected2}, got {r2.shares}"
    print(f"[PASS] Test 2 – NEUTRAL/CAUTION: {r2.shares} shares (discounted ${discounted:.0f})")

    # Test 3: BEAR / APPROVE
    r3 = compute_position("BEAR", passed_result, fvg=bear_fvg, direction="BEAR")
    expected3 = int(250.0 / 199.50)
    assert r3.shares == expected3, f"Expected {expected3}, got {r3.shares}"
    assert r3.tp == 195.00
    assert r3.entry == 199.50
    print(f"[PASS] Test 3 – BEAR/APPROVE: {r3.shares} shares @ {r3.entry}, TP={r3.tp}, SL={r3.sl}")

    # Test 4: CRISIS -> zero
    r4 = compute_position("CRISIS", passed_result, fvg=bull_fvg, direction="BULL")
    assert r4.shares == 0
    print(f"[PASS] Test 4 – CRISIS: 0 shares")

    # Test 5: REJECT (passed=False) -> zero
    r5 = compute_position("BULL", reject_result, fvg=bull_fvg, direction="BULL")
    assert r5.shares == 0
    print(f"[PASS] Test 5 – REJECT (passed=False): 0 shares")

    # Test 6: No FVG -> zero
    r6 = compute_position("BULL", passed_result, fvg=None, direction="BULL")
    assert r6.shares == 0
    print(f"[PASS] Test 6 – No FVG: 0 shares")

    # Test 7: SL is below entry for BULL
    assert r1.sl < r1.entry, f"BULL SL should be below entry: {r1.sl} vs {r1.entry}"
    print(f"[PASS] Test 7 – BULL SL < entry: {r1.sl} < {r1.entry}")

    # Test 8: SL is above entry for BEAR
    assert r3.sl > r3.entry, f"BEAR SL should be above entry: {r3.sl} vs {r3.entry}"
    print(f"[PASS] Test 8 – BEAR SL > entry: {r3.sl} > {r3.entry}")

    print("=" * 60)
    print("All selftests passed.")
    print("=" * 60)
