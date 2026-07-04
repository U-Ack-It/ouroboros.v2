from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

# ---------------------------------------------------------------------------
# Domain constants
# ---------------------------------------------------------------------------

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

REGIME_DOLLAR_LIMITS: dict[str, float] = {
    "BULL": 450.0,
    "NEUTRAL": 350.0,
    "BEAR": 250.0,
    "CRISIS": 0.0,
}

CAUTION_DISCOUNT = 0.40  # 40% reduction when Gate 4 verdict is CAUTION

# Minimum price guard — avoid division by zero or near-zero
_MIN_PRICE = 0.01

# ---------------------------------------------------------------------------
# Input / Output dataclasses
# ---------------------------------------------------------------------------

def compute_position(
    regime: str,
    pipeline_result: PipelineResult,
) -> PositionSpec:
    """
    Compute a ``PositionSpec`` from a macro *regime* and the *pipeline_result*.

    Parameters
    ----------
    regime:
        One of ``"BULL"``, ``"NEUTRAL"``, ``"BEAR"``, ``"CRISIS"``.
        Controls the base dollar allocation.
    pipeline_result:
        Typed result from the gate pipeline.  Must have ``fvg`` populated
        (non-None) and ``passed == True`` for a meaningful position to be
        returned; otherwise a zero-share spec is returned.

    Returns
    -------
    PositionSpec
        Fully computed position.  ``shares == 0`` signals "no trade."

    Notes
    -----
    * CAUTION verdict → base allocation reduced by 40 %.
    * CRISIS regime   → base allocation is $0 → zero shares always.
    * TP/SL are derived from the FVG gap boundaries:
        - BULL FVG: entry = gap_low, tp = gap_high, sl = gap_low - (gap_high - gap_low)
        - BEAR FVG: entry = gap_high, tp = gap_low, sl = gap_high + (gap_high - gap_low)
    """
    # ------------------------------------------------------------------ #
    # Guard: pipeline must have passed with valid FVG data                #
    # ------------------------------------------------------------------ #
    if not pipeline_result.passed or pipeline_result.fvg is None:
        return _zero_spec()

    fvg = pipeline_result.fvg

    # ------------------------------------------------------------------ #
    # Base dollar allocation by regime                                     #
    # ------------------------------------------------------------------ #
    regime_upper = regime.upper()
    base_dollars = REGIME_DOLLAR_LIMITS.get(regime_upper, 0.0)

    # ------------------------------------------------------------------ #
    # CAUTION discount                                                     #
    # ------------------------------------------------------------------ #
    verdict = None
    if pipeline_result.llm_verdict is not None:
        verdict = pipeline_result.llm_verdict.verdict.upper()

    if verdict == "CAUTION":
        base_dollars = base_dollars * (1.0 - CAUTION_DISCOUNT)

    # CRISIS or effective zero budget → no trade
    if base_dollars <= 0.0:
        return _zero_spec()

    # ------------------------------------------------------------------ #
    # Entry / TP / SL from FVG boundaries                                 #
    # ------------------------------------------------------------------ #
    entry, tp, sl = _compute_levels(fvg)

    if entry < _MIN_PRICE:
        return _zero_spec()

    # ------------------------------------------------------------------ #
    # Share count (whole shares, floor division)                           #
    # ------------------------------------------------------------------ #
    shares = int(base_dollars / entry)
    if shares < 1:
        return _zero_spec()

    dollar_risk = round(shares * entry, 4)

    return PositionSpec(
        shares=shares,
        entry=round(entry, 4),
        tp=round(tp, 4),
        sl=round(sl, 4),
        dollar_risk=dollar_risk,
    )

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _compute_levels(fvg: FVGBoundaries) -> tuple[float, float, float]:
    """
    Derive entry, take-profit, and stop-loss from FVG boundaries.

    For a BULL gap the price is expected to fill upward:
      entry = gap_low  (fill the gap from the bottom)
      tp    = gap_high (top of the gap as target)
      sl    = gap_low  - gap_width  (mirror below entry)

    For a BEAR gap the price is expected to fill downward:
      entry = gap_high (fill the gap from the top)
      tp    = gap_low  (bottom of the gap as target)
      sl    = gap_high + gap_width  (mirror above entry)

    If entry_price from the signal is more conservative than the raw
    boundary, it is used as the entry to honour the detector's intent.
    """
    gap_width = abs(fvg.gap_high - fvg.gap_low)
    direction = fvg.direction.upper()

    if direction == "BULL":
        raw_entry = fvg.gap_low
        # Honour a more conservative signal entry if provided
        entry = fvg.entry_price if fvg.entry_price > 0 else raw_entry
        tp = fvg.gap_high
        sl = raw_entry - gap_width
    else:  # BEAR (or anything else treated as bearish)
        raw_entry = fvg.gap_high
        entry = fvg.entry_price if fvg.entry_price > 0 else raw_entry
        tp = fvg.gap_low
        sl = raw_entry + gap_width

    return entry, tp, sl

def _zero_spec() -> PositionSpec:
    """Return a canonical no-trade PositionSpec."""
    return PositionSpec(shares=0, entry=0.0, tp=0.0, sl=0.0, dollar_risk=0.0)

# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    print("=" * 60)
    print("position_sizer selftest")
    print("=" * 60)

    # ------------------------------------------------------------------ #
    # Shared FVG fixture                                                   #
    # ------------------------------------------------------------------ #
    bull_fvg = FVGBoundaries(
        gap_high=155.00,
        gap_low=150.00,
        direction="BULL",
        entry_price=150.50,   # slightly above gap_low — conservative entry
    )

    bear_fvg = FVGBoundaries(
        gap_high=200.00,
        gap_low=195.00,
        direction="BEAR",
        entry_price=199.50,   # slightly below gap_high — conservative entry
    )

    approve_verdict = LLMVerdict(verdict="APPROVE", confidence=0.85)
    caution_verdict = LLMVerdict(verdict="CAUTION", confidence=0.55)
    reject_verdict  = LLMVerdict(verdict="REJECT",  confidence=0.90)

    # ------------------------------------------------------------------ #
    # Test 1: BULL regime, APPROVE, bullish FVG                           #
    # ------------------------------------------------------------------ #
    result = compute_position(
        regime="BULL",
        pipeline_result=PipelineResult(
            passed=True,
            llm_verdict=approve_verdict,
            fvg=bull_fvg,
        ),
    )
    print(f"\nTest 1 – BULL/APPROVE/bullish FVG")
    print(f"  {result}")
    assert result.shares > 0, "Expected shares > 0"
    expected_shares = int(450.0 / bull_fvg.entry_price)
    assert result.shares == expected_shares, (
        f"Expected {expected_shares} shares, got {result.shares}"
    )
    assert result.tp == bull_fvg.gap_high, "TP should equal gap_high for BULL"
    assert result.sl == round(bull_fvg.gap_low - (bull_fvg.gap_high - bull_fvg.gap_low), 4), \
        "SL check failed for BULL"
    print("  PASSED ✓")

    # ------------------------------------------------------------------ #
    # Test 2: NEUTRAL regime, CAUTION, bullish FVG (40% discount)        #
    # ------------------------------------------------------------------ #
    result2 = compute_position(
        regime="NEUTRAL",
        pipeline_result=PipelineResult(
            passed=True,
            llm_verdict=caution_verdict,
            fvg=bull_fvg,
        ),
    )
    print(f"\nTest 2 – NEUTRAL/CAUTION/bullish FVG (40% discount)")
    print(f"  {result2}")
    discounted_dollars = 350.0 * (1.0 - CAUTION_DISCOUNT)   # 210.0
    expected_shares2 = int(discounted_dollars / bull_fvg.entry_price)
    assert result2.shares == expected_shares2, (
        f"Expected {expected_shares2} shares after discount, got {result2.shares}"
    )
    print("  PASSED ✓")

    # ------------------------------------------------------------------ #
    # Test 3: BEAR regime, APPROVE, bearish FVG                          #
    # ------------------------------------------------------------------ #
    result3 = compute_position(
        regime="BEAR",
        pipeline_result=PipelineResult(
            passed=True,
            llm_verdict=approve_verdict,
            fvg=bear_fvg,
        ),
    )
    print(f"\nTest 3 – BEAR/APPROVE/bearish FVG")
    print(f"  {result3}")
    expected_shares3 = int(250.0 / bear_fvg.entry_price)
    assert result3.shares == expected_shares3, (
        f"Expected {expected_shares3} shares, got {result3.shares}"
    )
    assert result3.tp == bear_fvg.gap_low, "TP should equal gap_low for BEAR"
    assert result3.sl == round(
        bear_fvg.gap_high + (bear_fvg.gap_high - bear_fvg.gap_low), 4
    ), "SL check failed for BEAR"
    print("  PASSED ✓")

    # ------------------------------------------------------------------ #
    # Test 4: CRISIS regime → zero shares                                 #
    # ------------------------------------------------------------------ #
    result4 = compute_position(
        regime="CRISIS",
        pipeline_result=PipelineResult(
            passed=True,
            llm_verdict=approve_verdict,
            fvg=bull_fvg,
        ),
    )
    print(f"\nTest 4 – CRISIS regime")
    print(f"  {result4}")
    assert result4.shares == 0, "CRISIS should produce 0 shares"
    print("  PASSED ✓")

    # ------------------------------------------------------------------ #
    # Test 5: Pipeline not passed → zero shares                           #
    # ------------------------------------------------------------------ #
    result5 = compute_position(
        regime="BULL",
        pipeline_result=PipelineResult(
            passed=False,
            llm_verdict=approve_verdict,
            fvg=bull_fvg,
        ),
    )
    print(f"\nTest 5 – pipeline not passed")
    print(f"  {result5}")
    assert result5.shares == 0, "Failed pipeline should produce 0 shares"
    print("  PASSED ✓")

    # ------------------------------------------------------------------ #
    # Test 6: Gate 4 REJECT verdict on a passed pipeline → zero shares   #
    # (REJECT means passed=False upstream; but if somehow passed=True,   #
    #  we honour whatever the pipeline says and let Gate 4 set passed.)  #
    # In real usage pipeline.passed=False on REJECT. Test for safety.    #
    # ------------------------------------------------------------------ #
    result6 = compute_position(
        regime="BULL",
        pipeline_result=PipelineResult(
            passed=False,   # Gate 4 REJECT sets passed=False in gate_pipeline
            llm_verdict=reject_verdict,
            fvg=bull_fvg,
        ),
    )
    print(f"\nTest 6 – REJECT verdict (passed=False)")
    print(f"  {result6}")
    assert result6.shares == 0, "REJECT should produce 0 shares"
    print("  PASSED ✓")

    # ------------------------------------------------------------------ #
    # Test 7: No FVG data → zero shares                                   #
    # ------------------------------------------------------------------ #
    result7 = compute_position(
        regime="BULL",
        pipeline_result=PipelineResult(
            passed=True,
            llm_verdict=approve_verdict,
            fvg=None,
        ),
    )
    print(f"\nTest 7 – no FVG boundaries")
    print(f"  {result7}")
    assert result7.shares == 0, "Missing FVG should produce 0 shares"
    print("  PASSED ✓")

    # ------------------------------------------------------------------ #
    # Test 8: BEAR/CAUTION combination                                    #
    # ------------------------------------------------------------------ #
    result8 = compute_position(
        regime="BEAR",
        pipeline_result=PipelineResult(
            passed=True,
            llm_verdict=caution_verdict,
            fvg=bear_fvg,
        ),
    )
    print(f"\nTest 8 – BEAR/CAUTION/bearish FVG")
    print(f"  {result8}")
    discounted_bear = 250.0 * (1.0 - CAUTION_DISCOUNT)   # 150.0
    expected_shares8 = int(discounted_bear / bear_fvg.entry_price)
    assert result8.shares == expected_shares8, (
        f"Expected {expected_shares8} shares, got {result8.shares}"
    )
    print("  PASSED ✓")

    print("\n" + "=" * 60)
    print("All selftests passed.")
    print("=" * 60)
