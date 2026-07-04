from __future__ import annotations

import json
import datetime
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Set

# ---------------------------------------------------------------------------
# Enums
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

class Regime(str, Enum):
    BULL = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR = "BEAR"
    CRISIS = "CRISIS"

def _gate0_universe(ticker: str) -> GateResult:
    """Gate 0 – Universe whitelist O(1) lookup."""
    passed = ticker.upper() in _UNIVERSE_WHITELIST
    reason = (
        "Ticker in universe whitelist."
        if passed
        else f"Ticker '{ticker}' not in universe whitelist."
    )
    return GateResult(gate_id=0, passed=passed, reason=reason)

def _gate1_ethics(ticker: str) -> GateResult:
    """Gate 1 – Ethics / boycott filter."""
    blocked = ticker.upper() in _ETHICS_BLOCKLIST
    passed = not blocked
    reason = (
        "Ticker passes ethics filter."
        if passed
        else f"Ticker '{ticker}' is on the ethics/boycott blocklist."
    )
    return GateResult(gate_id=1, passed=passed, reason=reason)

def _gate2_regime(regime: Regime) -> GateResult:
    """Gate 2 – Macro regime gate. Rejects on CRISIS."""
    passed = regime != Regime.CRISIS
    reason = (
        f"Regime '{regime.value}' is tradeable."
        if passed
        else "Regime is CRISIS – all trading halted."
    )
    return GateResult(gate_id=2, passed=passed, reason=reason)

def _gate3_fvg(fvg_signal: Optional[Any]) -> GateResult:
    """
    Gate 3 – FVG detection gate.

    Accepts either:
    - An FVGSignal dataclass with a ``gap_pct`` attribute and a truthy value, or
    - A plain dict with key ``"gap_pct"`` and ``"direction"``.
    - None / falsy → rejected.
    """
    if fvg_signal is None:
        return GateResult(
            gate_id=3,
            passed=False,
            reason="No FVG signal detected for this ticker.",
        )

    # Support both dataclass and dict representations
    if isinstance(fvg_signal, dict):
        gap_pct = fvg_signal.get("gap_pct", 0.0)
        direction = fvg_signal.get("direction", "UNKNOWN")
    else:
        gap_pct = getattr(fvg_signal, "gap_pct", 0.0)
        direction = getattr(fvg_signal, "direction", "UNKNOWN")

    if gap_pct <= 0.0:
        return GateResult(
            gate_id=3,
            passed=False,
            reason=f"FVG gap_pct ({gap_pct:.4f}%) is not positive – signal rejected.",
        )

    return GateResult(
        gate_id=3,
        passed=True,
        reason=f"FVG detected: direction={direction}, gap_pct={gap_pct:.4f}%.",
    )

def _build_gate4_payload(
    ticker: str,
    fvg_signal: Any,
    trend_context: Any,
    regime: Regime,
    position_size: float,
) -> Dict[str, Any]:
    """Serialise all upstream context into the structured JSON payload for Gate 4."""

    def _fvg_to_dict(fvg: Any) -> Dict[str, Any]:
        if fvg is None:
            return {}
        if isinstance(fvg, dict):
            return fvg
        return {
            "ticker": getattr(fvg, "ticker", ticker),
            "direction": str(getattr(fvg, "direction", "")),
            "gap_pct": float(getattr(fvg, "gap_pct", 0.0)),
            "entry_price": float(getattr(fvg, "entry_price", 0.0)),
            "timestamp": str(getattr(fvg, "timestamp", "")),
            "bar_indices": list(getattr(fvg, "bar_indices", [])),
        }

    def _trend_to_dict(tc: Any) -> Dict[str, Any]:
        if tc is None:
            return {}
        if isinstance(tc, dict):
            return tc
        return {
            "htf_bias": str(getattr(tc, "htf_bias", "")),
            "ltf_bias": str(getattr(tc, "ltf_bias", "")),
            "alignment": str(getattr(tc, "alignment", "")),
            "choch": bool(getattr(tc, "choch", False)),
            "momentum": float(getattr(tc, "momentum", 0.0)),
            "regime": str(getattr(tc, "regime", "")),
        }

    return {
        "ticker": ticker,
        "fvg_signal": _fvg_to_dict(fvg_signal),
        "trend_context": _trend_to_dict(trend_context),
        "regime": regime.value,
        "position_size": position_size,
        "timestamp_utc": datetime.datetime.utcnow().isoformat(),
    }

def _gate4_llm(
    ticker: str,
    fvg_signal: Any,
    trend_context: Any,
    regime: Regime,
    position_size: float,
    config: Dict[str, Any],
) -> tuple[GateResult, Gate4Result]:
    """
    Gate 4 – LLM reasoning via Claude Sonnet.

    Dry-run mode (config["gate4_dry_run"] == True or no API key):
        Returns APPROVE with confidence=0.0, no API call made.

    Live mode:
        Sends structured JSON to Claude Sonnet, parses JSON response.
        Expected response schema:
            {"verdict": "APPROVE|CAUTION|REJECT", "confidence": 0.0-1.0,
             "reasoning": "..."}
    """
    dry_run: bool = bool(config.get("gate4_dry_run", False))
    api_key: str = config.get("anthropic_api_key", "")

    payload = _build_gate4_payload(
        ticker, fvg_signal, trend_context, regime, position_size
    )

    if dry_run or not api_key:
        g4 = Gate4Result(
            verdict=LLMVerdict.APPROVE,
            confidence=0.0,
            reasoning="Dry-run mode: Gate 4 LLM call skipped.",
            dry_run=True,
        )
        gate = GateResult(
            gate_id=4,
            passed=True,
            reason="Gate 4 dry-run: auto-APPROVE (confidence=0.0).",
        )
        return gate, g4

    # --- Live LLM call -------------------------------------------------------
    try:
        import anthropic  # type: ignore
    except ImportError as exc:
        raise ImportError(
            "anthropic SDK is not installed. "
            "Run `pip install anthropic` or use gate4_dry_run=True."
        ) from exc

    system_prompt = (
        "You are a quantitative trading risk analyst for a Smart Money Concepts "
        "strategy. You will receive a JSON payload describing an FVG trade signal "
        "with macro regime, trend context, and proposed position size. "
        "Respond ONLY with a JSON object (no markdown, no prose) with exactly "
        "three keys: 'verdict' (one of APPROVE, CAUTION, REJECT), "
        "'confidence' (float 0.0 to 1.0), and 'reasoning' (brief string). "
        "APPROVE = high-conviction setup. CAUTION = proceed with reduced size. "
        "REJECT = do not trade."
    )

    user_message = (
        f"Evaluate this SMC trade signal and return your JSON verdict:\n\n"
        f"{json.dumps(payload, indent=2)}"
    )

    model: str = config.get("llm_model", "claude-sonnet-4-5")
    max_tokens: int = int(config.get("llm_max_tokens", 256))

    try:
        client = anthropic.Anthropic(api_key=api_key)
        message = client.messages.create(
            model=model,
            max_tokens=max_tokens,
            system=system_prompt,
            messages=[{"role": "user", "content": user_message}],
        )
        raw_text: str = message.content[0].text.strip()
    except Exception as exc:
        # Fail-safe: treat LLM errors as CAUTION to avoid blocking the pipeline
        g4 = Gate4Result(
            verdict=LLMVerdict.CAUTION,
            confidence=0.0,
            reasoning=f"LLM call failed: {exc}. Defaulting to CAUTION.",
            dry_run=False,
        )
        gate = GateResult(
            gate_id=4,
            passed=True,
            reason="Gate 4 LLM error – defaulting to CAUTION (pipeline continues).",
        )
        return gate, g4

    # Parse the response
    try:
        parsed: Dict[str, Any] = json.loads(raw_text)
        raw_verdict = str(parsed.get("verdict", "REJECT")).upper()
        verdict = LLMVerdict(raw_verdict)
        confidence = float(parsed.get("confidence", 0.0))
        confidence = max(0.0, min(1.0, confidence))
        reasoning = str(parsed.get("reasoning", ""))
    except (json.JSONDecodeError, ValueError, KeyError) as exc:
        # Malformed response → CAUTION
        g4 = Gate4Result(
            verdict=LLMVerdict.CAUTION,
            confidence=0.0,
            reasoning=f"Failed to parse LLM response: {exc}. Raw: {raw_text[:200]}",
            dry_run=False,
        )
        gate = GateResult(
            gate_id=4,
            passed=True,
            reason="Gate 4 LLM parse error – defaulting to CAUTION.",
        )
        return gate, g4

    g4 = Gate4Result(
        verdict=verdict,
        confidence=confidence,
        reasoning=reasoning,
        dry_run=False,
    )

    if verdict == LLMVerdict.REJECT:
        gate = GateResult(
            gate_id=4,
            passed=False,
            reason=f"Gate 4 LLM REJECT (confidence={confidence:.2f}): {reasoning}",
        )
    else:
        label = "APPROVE" if verdict == LLMVerdict.APPROVE else "CAUTION"
        gate = GateResult(
            gate_id=4,
            passed=True,
            reason=f"Gate 4 LLM {label} (confidence={confidence:.2f}): {reasoning}",
        )

    return gate, g4

# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def run_gates(
    ticker: str,
    bars_5m: Any,
    bars_15m: Any,
    regime: Regime,
    config: Dict[str, Any],
    *,
    fvg_signal: Optional[Any] = None,
    trend_context: Optional[Any] = None,
    position_size: float = 0.0,
) -> PipelineResult:
    """
    Run the full 5-gate sequential filter for *ticker*.

    Parameters
    ----------
    ticker:
        The symbol being evaluated.
    bars_5m:
        5-minute BarSeries (from bar_aggregator).  Used by caller context;
        pipeline itself validates presence but does not re-run detection.
    bars_15m:
        15-minute BarSeries (from bar_aggregator).
    regime:
        Macro regime from Gate 2 external input (BULL/NEUTRAL/BEAR/CRISIS).
    config:
        Configuration dict.  Relevant keys:

        - ``gate4_dry_run`` (bool, default False): skip real LLM call.
        - ``anthropic_api_key`` (str): Anthropic API key for live Gate 4.
        - ``llm_model`` (str, default "claude-sonnet-4-5"): model name.
        - ``llm_max_tokens`` (int, default 256): max response tokens.
        - ``additional_whitelist`` (list[str]): extra tickers for Gate 0.
        - ``additional_blocklist`` (list[str]): extra tickers for Gate 1.

    fvg_signal:
        Pre-computed FVGSignal dataclass or dict (from fvg_detector).
        If None, Gate 3 will reject.
    trend_context:
        Pre-computed TrendContext dataclass or dict (from trend_engine).
    position_size:
        Dollar position size to include in Gate 4 payload.

    Returns
    -------
    PipelineResult
        Full record of gate outcomes, short-circuiting on first rejection.
    """

    # Allow caller-supplied whitelist/blocklist extensions
    extra_whitelist: Set[str] = {
        t.upper() for t in config.get("additional_whitelist", [])
    }
    extra_blocklist: Set[str] = {
        t.upper() for t in config.get("additional_blocklist", [])
    }
    effective_whitelist = _UNIVERSE_WHITELIST | extra_whitelist
    effective_blocklist = _ETHICS_BLOCKLIST | extra_blocklist

    ticker_upper = ticker.upper()
    results: List[GateResult] = []
    gate4_result: Optional[Gate4Result] = None

    # ------------------------------------------------------------------ #
    # Gate 0 – Universe whitelist                                          #
    # ------------------------------------------------------------------ #
    g0_passed = ticker_upper in effective_whitelist
    g0 = GateResult(
        gate_id=0,
        passed=g0_passed,
        reason=(
            "Ticker in universe whitelist."
            if g0_passed
            else f"Ticker '{ticker}' not in universe whitelist."
        ),
    )
    results.append(g0)
    if not g0.passed:
        return PipelineResult(
            ticker=ticker,
            passed=False,
            gate_results=results,
            rejected_at=0,
            final_reason=g0.reason,
        )

    # ------------------------------------------------------------------ #
    # Gate 1 – Ethics / boycott filter                                    #
    # ------------------------------------------------------------------ #
    g1_blocked = ticker_upper in effective_blocklist
    g1 = GateResult(
        gate_id=1,
        passed=not g1_blocked,
        reason=(
            "Ticker passes ethics filter."
            if not g1_blocked
            else f"Ticker '{ticker}' is on the ethics/boycott blocklist."
        ),
    )
    results.append(g1)
    if not g1.passed:
        return PipelineResult(
            ticker=ticker,
            passed=False,
            gate_results=results,
            rejected_at=1,
            final_reason=g1.reason,
        )

    # ------------------------------------------------------------------ #
    # Gate 2 – Regime gate                                                 #
    # ------------------------------------------------------------------ #
    g2 = _gate2_regime(regime)
    results.append(g2)
    if not g2.passed:
        return PipelineResult(
            ticker=ticker,
            passed=False,
            gate_results=results,
            rejected_at=2,
            final_reason=g2.reason,
        )

    # ------------------------------------------------------------------ #
    # Gate 3 – FVG detection                                               #
    # ------------------------------------------------------------------ #
    g3 = _gate3_fvg(fvg_signal)
    results.append(g3)
    if not g3.passed:
        return PipelineResult(
            ticker=ticker,
            passed=False,
            gate_results=results,
            rejected_at=3,
            final_reason=g3.reason,
        )

    # ------------------------------------------------------------------ #
    # Gate 4 – LLM reasoning                                               #
    # ------------------------------------------------------------------ #
    g4, gate4_result = _gate4_llm(
        ticker=ticker,
        fvg_signal=fvg_signal,
        trend_context=trend_context,
        regime=regime,
        position_size=position_size,
        config=config,
    )
    results.append(g4)
    if not g4.passed:
        return PipelineResult(
            ticker=ticker,
            passed=False,
            gate_results=results,
            gate4=gate4_result,
            rejected_at=4,
            final_reason=g4.reason,
        )

    # ------------------------------------------------------------------ #
    # All gates passed                                                     #
    # ------------------------------------------------------------------ #
    return PipelineResult(
        ticker=ticker,
        passed=True,
        gate_results=results,
        gate4=gate4_result,
        rejected_at=None,
        final_reason="All gates passed.",
    )

# ---------------------------------------------------------------------------
# Selftest (offline, no API calls)
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import datetime as _dt

    print("=" * 60)
    print("gate_pipeline.py – offline selftest")
    print("=" * 60)

    # Minimal fixture FVG signal as dict
    _fvg = {
        "ticker": "AAPL",
        "direction": "BULL",
        "gap_pct": 0.12,
        "entry_price": 185.50,
        "timestamp": "2024-01-15T10:00:00Z",
        "bar_indices": [10, 11, 12],
    }

    # Minimal fixture trend context as dict
    _trend = {
        "htf_bias": "BULL",
        "ltf_bias": "BULL",
        "alignment": "WITH",
        "choch": False,
        "momentum": 0.65,
        "regime": "TRENDING",
    }

    _config_dry = {"gate4_dry_run": True}

    # --- Test 1: AAPL BULL regime, all gates pass (dry-run) ---------------
    result = run_gates(
        ticker="AAPL",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.BULL,
        config=_config_dry,
        fvg_signal=_fvg,
        trend_context=_trend,
        position_size=450.0,
    )
    assert result.passed, f"Test 1 failed: {result.final_reason}"
    assert result.rejected_at is None
    assert result.gate4 is not None
    assert result.gate4.verdict == LLMVerdict.APPROVE
    assert result.gate4.confidence == 0.0
    assert result.gate4.dry_run is True
    assert len(result.gate_results) == 5
    print(f"[PASS] Test 1 – AAPL BULL all-pass dry-run: {result.final_reason}")

    # --- Test 2: Gate 0 rejection (ticker not in whitelist) ---------------
    result2 = run_gates(
        ticker="FAKESTOCK",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.BULL,
        config=_config_dry,
        fvg_signal=_fvg,
        trend_context=_trend,
        position_size=450.0,
    )
    assert not result2.passed
    assert result2.rejected_at == 0
    assert len(result2.gate_results) == 1  # short-circuited
    print(f"[PASS] Test 2 – Gate 0 rejection: {result2.final_reason}")

    # --- Test 3: Gate 1 rejection (ethics blocklist) ----------------------
    # MO is in blocklist; need to add it to whitelist via extra config
    result3 = run_gates(
        ticker="MO",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.BULL,
        config={**_config_dry, "additional_whitelist": ["MO"]},
        fvg_signal=_fvg,
        trend_context=_trend,
        position_size=450.0,
    )
    assert not result3.passed
    assert result3.rejected_at == 1
    assert len(result3.gate_results) == 2
    print(f"[PASS] Test 3 – Gate 1 rejection (ethics): {result3.final_reason}")

    # --- Test 4: Gate 2 rejection (CRISIS regime) -------------------------
    result4 = run_gates(
        ticker="AAPL",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.CRISIS,
        config=_config_dry,
        fvg_signal=_fvg,
        trend_context=_trend,
        position_size=0.0,
    )
    assert not result4.passed
    assert result4.rejected_at == 2
    assert len(result4.gate_results) == 3
    print(f"[PASS] Test 4 – Gate 2 rejection (CRISIS): {result4.final_reason}")

    # --- Test 5: Gate 3 rejection (no FVG signal) -------------------------
    result5 = run_gates(
        ticker="AAPL",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.BULL,
        config=_config_dry,
        fvg_signal=None,  # no signal
        trend_context=_trend,
        position_size=450.0,
    )
    assert not result5.passed
    assert result5.rejected_at == 3
    assert len(result5.gate_results) == 4
    print(f"[PASS] Test 5 – Gate 3 rejection (no FVG): {result5.final_reason}")

    # --- Test 6: Gate 3 rejection (gap_pct = 0) ---------------------------
    _fvg_bad = {**_fvg, "gap_pct": 0.0}
    result6 = run_gates(
        ticker="AAPL",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.NEUTRAL,
        config=_config_dry,
        fvg_signal=_fvg_bad,
        trend_context=_trend,
        position_size=350.0,
    )
    assert not result6.passed
    assert result6.rejected_at == 3
    print(f"[PASS] Test 6 – Gate 3 rejection (gap_pct=0): {result6.final_reason}")

    # --- Test 7: BEAR regime, valid FVG, dry-run --------------------------
    result7 = run_gates(
        ticker="MSFT",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.BEAR,
        config=_config_dry,
        fvg_signal={**_fvg, "ticker": "MSFT", "direction": "BEAR", "gap_pct": 0.20},
        trend_context={**_trend, "htf_bias": "BEAR", "ltf_bias": "BEAR", "alignment": "WITH"},
        position_size=250.0,
    )
    assert result7.passed
    assert result7.rejected_at is None
    assert result7.gate4 is not None
    assert result7.gate4.dry_run is True
    print(f"[PASS] Test 7 – MSFT BEAR all-pass dry-run: {result7.final_reason}")

    # --- Test 8: additional_whitelist / additional_blocklist extension ----
    result8 = run_gates(
        ticker="NEWCO",
        bars_5m=None,
        bars_15m=None,
        regime=Regime.BULL,
        config={**_config_dry, "additional_whitelist": ["NEWCO"]},
        fvg_signal=_fvg,
        trend_context=_trend,
        position_size=450.0,
    )
    assert result8.passed
    print(f"[PASS] Test 8 – additional_whitelist extension: {result8.final_reason}")

    # --- Test 9: PipelineResult gate_results ordering ---------------------
    assert [g.gate_id for g in result.gate_results] == [0, 1, 2, 3, 4]
    print("[PASS] Test 9 – gate_results ordered 0..4")

    # --- Test 10: Gate 4 payload builder ----------------------------------
    payload = _build_gate4_payload(
        ticker="AAPL",
        fvg_signal=_fvg,
        trend_context=_trend,
        regime=Regime.BULL,
        position_size=450.0,
    )
    assert payload["ticker"] == "AAPL"
    assert payload["regime"] == "BULL"
    assert payload["fvg_signal"]["gap_pct"] == 0.12
    assert payload["trend_context"]["alignment"] == "WITH"
    assert "timestamp_utc" in payload
    print("[PASS] Test 10 – Gate 4 payload structure correct")

    print()
    print("All selftests passed. ✓")
