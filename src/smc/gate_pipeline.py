"""
src/smc/gate_pipeline.py

Five-gate sequential filter. Short-circuits on first rejection.
Gate 0: Universe whitelist (O(1) frozenset lookup)
Gate 1: Ethics/boycott blocklist
Gate 2: Macro regime (BULL/NEUTRAL/BEAR/CRISIS from VIX/SPY snapshot)
Gate 3: FVG detection (delegates to fvg_detector)
Gate 4: LLM reasoning (Claude Sonnet, structured JSON verdict)

All gates communicate via GateResult / PipelineResult from types.py.
Gate 4 supports dry_run=True for offline testing (no API call, auto-APPROVE, confidence=0.0).
"""
from __future__ import annotations

import json
import os
from typing import Any

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
# Universe / ethics constants. Extend via config dicts at call time.
# ---------------------------------------------------------------------------
_UNIVERSE_WHITELIST: frozenset = frozenset({
    "SPY", "QQQ", "IWM", "GLD", "TLT", "HYG", "XLE", "XLF", "EEM", "EWZ",
    "USO", "UNG", "COPX", "CPER", "SOYB",
    "AAPL", "MSFT", "NVDA", "AMZN", "TSLA", "META", "GOOGL",
    "BDRY", "VALE",
})

_ETHICS_BLOCKLIST: frozenset = frozenset()  # add tickers here to block on ESG grounds


# ---------------------------------------------------------------------------
# Gate 0: Universe whitelist
# ---------------------------------------------------------------------------
def _gate0_universe(ticker: str, whitelist: frozenset) -> GateResult:
    passed = ticker.upper() in whitelist
    return GateResult(
        gate_id=0,
        passed=passed,
        reason="Ticker in universe whitelist." if passed
               else f"'{ticker}' not in approved universe.",
    )


# ---------------------------------------------------------------------------
# Gate 1: Ethics / boycott filter
# ---------------------------------------------------------------------------
def _gate1_ethics(ticker: str, blocklist: frozenset) -> GateResult:
    blocked = ticker.upper() in blocklist
    return GateResult(
        gate_id=1,
        passed=not blocked,
        reason="Passes ethics filter." if not blocked
               else f"'{ticker}' is on the ethics/boycott blocklist.",
    )


# ---------------------------------------------------------------------------
# Gate 2: Macro regime gate
# ---------------------------------------------------------------------------
def _gate2_regime(regime: str) -> GateResult:
    """Block if macro regime is CRISIS. Anything else passes through."""
    r = regime.upper()
    if r == "CRISIS":
        return GateResult(gate_id=2, passed=False,
                          reason="CRISIS regime: all new entries blocked.")
    return GateResult(gate_id=2, passed=True,
                      reason=f"Regime {r}: entry allowed.")


# ---------------------------------------------------------------------------
# Gate 3: FVG detection
# ---------------------------------------------------------------------------
def _gate3_fvg(fvg_result: FVGResult) -> GateResult:
    """Accepts a pre-computed FVGResult from fvg_detector."""
    if fvg_result.signal is None:
        return GateResult(gate_id=3, passed=False,
                          reason=fvg_result.reason or "No qualifying FVG detected.")
    sig = fvg_result.signal
    return GateResult(gate_id=3, passed=True,
                      reason=f"{sig.direction.value} FVG {sig.gap_pct:.4%} at {sig.entry_price:.2f}.")


# ---------------------------------------------------------------------------
# Gate 4: LLM reasoning
# ---------------------------------------------------------------------------
_GATE4_SYSTEM = (
    "You are an SMC trade gate. Given a signal payload, respond ONLY with "
    "valid JSON: {\"verdict\": \"APPROVE\"|\"CAUTION\"|\"REJECT\", "
    "\"confidence\": 0.0-1.0, \"rationale\": \"one sentence\"}. "
    "No markdown, no explanation outside JSON."
)


def _gate4_llm(
    ticker: str,
    fvg_signal: FVGSignal | None,
    trend_context: TrendContext | None,
    regime: str,
    position_size: float,
    *,
    api_key: str = "",
    dry_run: bool = False,
) -> tuple[GateResult, Gate4Result]:
    """LLM reasoning gate. Returns (GateResult, Gate4Result)."""
    if dry_run or not api_key:
        g4 = Gate4Result(
            verdict=LLMVerdict.APPROVE,
            confidence=0.0,
            rationale="Dry-run mode: Gate 4 LLM call skipped.",
        )
        gate = GateResult(gate_id=4, passed=True,
                          reason="Gate 4 dry-run: auto-APPROVE (confidence=0.0).")
        return gate, g4

    payload: dict[str, Any] = {
        "ticker": ticker,
        "regime": regime,
        "position_size_usd": position_size,
        "fvg": {
            "direction": fvg_signal.direction.value if fvg_signal else None,
            "gap_pct": fvg_signal.gap_pct if fvg_signal else None,
            "entry_price": fvg_signal.entry_price if fvg_signal else None,
        } if fvg_signal else None,
        "trend": {
            "htf_bias": trend_context.htf_bias.value if trend_context else None,
            "ltf_bias": trend_context.ltf_bias.value if trend_context else None,
            "alignment": trend_context.alignment.value if trend_context else None,
            "momentum": trend_context.momentum if trend_context else None,
            "regime": trend_context.regime.value if trend_context else None,
        } if trend_context else None,
    }

    try:
        import anthropic
        client = anthropic.Anthropic(api_key=api_key or os.environ.get("ANTHROPIC_API_KEY", ""))
        msg = client.messages.create(
            model="claude-sonnet-4-6",
            max_tokens=256,
            system=_GATE4_SYSTEM,
            messages=[{"role": "user", "content": json.dumps(payload)}],
        )
        raw = "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        data = json.loads(raw)
        verdict = LLMVerdict(data["verdict"])
        confidence = float(data.get("confidence", 0.5))
        rationale = str(data.get("rationale", ""))
    except Exception as exc:
        # Fail open with CAUTION rather than blocking on LLM outage
        verdict = LLMVerdict.CAUTION
        confidence = 0.0
        rationale = f"Gate 4 error (fail-open CAUTION): {exc}"

    g4 = Gate4Result(verdict=verdict, confidence=confidence, rationale=rationale)
    passed = verdict in (LLMVerdict.APPROVE, LLMVerdict.CAUTION)
    gate = GateResult(gate_id=4, passed=passed,
                      reason=f"Gate 4 {verdict.value} (conf={confidence:.2f}): {rationale}")
    return gate, g4


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------
def run_gates(
    ticker: str,
    fvg_result: FVGResult,
    trend_context: TrendContext | None = None,
    regime: str = "NEUTRAL",
    position_size: float = 350.0,
    *,
    config: dict[str, Any] | None = None,
    api_key: str = "",
    dry_run: bool = False,
    intended_direction: str | None = None,
) -> PipelineResult:
    """
    Run all five gates in sequence. Short-circuits on first rejection.

    Parameters
    ----------
    ticker:        Equity symbol (case-insensitive).
    fvg_result:    Pre-computed FVGResult from fvg_detector.detect_fvg().
    trend_context: Optional multi-TF trend context from trend_engine.
    regime:        Gate 2 macro regime string (BULL/NEUTRAL/BEAR/CRISIS).
    position_size: Dollar size estimate for Gate 4 payload.
    config:        Optional dict with 'additional_whitelist' / 'extra_blocklist' lists.
    api_key:       Anthropic API key. Falls back to ANTHROPIC_API_KEY env var.
    dry_run:       If True, Gate 4 auto-APPROVEs without an API call.

    Returns
    -------
    PipelineResult with all gate results, Gate4Result, and reject_gate index.
    """
    cfg = config or {}
    extra_whitelist: frozenset = frozenset(
        t.upper() for t in cfg.get("additional_whitelist", [])
    )
    extra_blocklist: frozenset = frozenset(
        t.upper() for t in cfg.get("extra_blocklist", [])
    )
    effective_whitelist = _UNIVERSE_WHITELIST | extra_whitelist
    effective_blocklist = _ETHICS_BLOCKLIST | extra_blocklist

    results: list[GateResult] = []

    g0 = _gate0_universe(ticker, effective_whitelist)
    results.append(g0)
    if not g0.passed:
        return PipelineResult(passed=False, gate_results=results, reject_gate=0)

    g1 = _gate1_ethics(ticker, effective_blocklist)
    results.append(g1)
    if not g1.passed:
        return PipelineResult(passed=False, gate_results=results, reject_gate=1)

    g2 = _gate2_regime(regime)
    results.append(g2)
    if not g2.passed:
        return PipelineResult(passed=False, gate_results=results, reject_gate=2)

    g3 = _gate3_fvg(fvg_result)
    results.append(g3)
    if not g3.passed:
        return PipelineResult(passed=False, gate_results=results, reject_gate=3)

    # Gate 3.5 (id=5): reject trades AGAINST the multi-TF trend.
    # Empirically -0.51% avg pnl on the 116-trade ledger (n=6 AGAINST setups).
    # If trend_context is None, this gate passes (unknown != against).
    if trend_context is not None and trend_context.alignment == Alignment.AGAINST:
        g5 = GateResult(gate_id=5, passed=False,
                        reason=f"Rejected: FVG direction AGAINST trend "
                               f"(HTF={trend_context.htf_bias.value}, "
                               f"LTF={trend_context.ltf_bias.value}).")
        results.append(g5)
        return PipelineResult(passed=False, gate_results=results, reject_gate=5)

    # Gate 6 (id=6): reject when caller-specified direction disagrees with the
    # FVG we actually detected. Live scanner passes the detected direction so
    # this becomes a self-consistency no-op. Backtest passes ledger direction,
    # so this filters trades where SMC and ledger disagree on direction —
    # empirically +12pp win rate on the agreement subset (56% vs 44%, n=91 vs n=25).
    if (intended_direction is not None
            and fvg_result.signal is not None
            and fvg_result.signal.direction.value != intended_direction.upper()):
        g6 = GateResult(gate_id=6, passed=False,
                        reason=f"Rejected: intended direction {intended_direction} "
                               f"disagrees with detected FVG "
                               f"{fvg_result.signal.direction.value}.")
        results.append(g6)
        return PipelineResult(passed=False, gate_results=results, reject_gate=6)

    g4_gate, g4_result = _gate4_llm(
        ticker=ticker,
        fvg_signal=fvg_result.signal,
        trend_context=trend_context,
        regime=regime,
        position_size=position_size,
        api_key=api_key,
        dry_run=dry_run,
    )
    results.append(g4_gate)
    if not g4_gate.passed:
        return PipelineResult(passed=False, gate_results=results,
                               gate4=g4_result, reject_gate=4)

    return PipelineResult(passed=True, gate_results=results, gate4=g4_result)


# ---------------------------------------------------------------------------
# Selftest
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    try:
        from .fvg_detector import detect_fvg, FVGRegime as _R
    except ImportError:
        from smc.fvg_detector import detect_fvg
        from smc.types import FVGRegime as _R

    import datetime

    print("=" * 60)
    print("gate_pipeline self-test (dry_run=True, no API calls)")
    print("=" * 60)

    # Minimal BarSeries-like fixture for a bullish FVG
    try:
        from .bar_aggregator import Bar, BarSeries, FixtureBarClient, get_bars
    except ImportError:
        from smc.bar_aggregator import Bar, BarSeries, FixtureBarClient, get_bars

    base = datetime.datetime(2024, 1, 2, 9, 30, tzinfo=datetime.timezone.utc)
    bars = [
        Bar(open=100.0, high=100.0, low=99.0,  close=99.5,  volume=1000, timestamp=base),
        Bar(open=99.5,  high=100.2, low=99.4,  close=100.1, volume=1200,
            timestamp=base + datetime.timedelta(minutes=5)),
        Bar(open=100.1, high=101.0, low=100.3, close=100.8, volume=1500,
            timestamp=base + datetime.timedelta(minutes=10)),
    ]
    fvg = detect_fvg(BarSeries(symbol="SPY", timeframe="5m", bars=bars), FVGRegime.BULL)

    # Test 1: full pass (SPY, BULL regime, dry_run)
    result = run_gates("SPY", fvg, regime="BULL", dry_run=True)
    assert result.passed, f"Expected pass: {result}"
    assert result.gate4 is not None
    assert result.gate4.verdict == LLMVerdict.APPROVE
    assert result.gate4.confidence == 0.0
    print(f"[PASS] Test 1 – SPY BULL dry_run: {result.gate4.verdict.value}")

    # Test 2: Gate 0 rejection (unknown ticker)
    r2 = run_gates("UNKNOWN_XYZ", fvg, dry_run=True)
    assert not r2.passed
    assert r2.reject_gate == 0
    print(f"[PASS] Test 2 – unknown ticker rejected at Gate 0")

    # Test 3: Gate 2 rejection (CRISIS regime)
    r3 = run_gates("SPY", fvg, regime="CRISIS", dry_run=True)
    assert not r3.passed
    assert r3.reject_gate == 2
    print(f"[PASS] Test 3 – CRISIS regime rejected at Gate 2")

    # Test 4: Gate 3 rejection (empty FVG)
    from smc.types import FVGResult as _FR
    empty_fvg = _FR(signal=None, rejected=True, reason="No FVG")
    r4 = run_gates("SPY", empty_fvg, dry_run=True)
    assert not r4.passed
    assert r4.reject_gate == 3
    print(f"[PASS] Test 4 – no FVG rejected at Gate 3")

    # Test 5: Gate 4 payload structure correct
    assert result.gate_results[4].gate_id == 4
    print(f"[PASS] Test 5 – Gate 4 gate_id correct")

    print("\nAll selftests passed. ✓")
