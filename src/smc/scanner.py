"""
src/smc/scanner.py — equity scanner over the full SMC pipeline.

Wraps: bar_aggregator (get_bars, 15m + 5m) -> fvg_detector (detect_fvg on 15m)
     -> trend_engine (analyze_trend from both TFs)
     -> gate_pipeline (run_gates, dry_run flag) -> position_sizer (compute_position).

Preserves the ScanResult contract used by async_scanner + heartbeat + telegram:
    fvg = {"type": "BULL_FVG"|"BEAR_FVG"|None,
           "size": float,  # gap in price units (not %)
           "price": float} # entry_price from FVGSignal

Extends ScanResult (compat) with optional attributes attached post-construction:
    result._pipeline: PipelineResult | None
    result._position: PositionSpec  | None
    result._trend:    TrendContext  | None

Callers that don't know about these get the same dict they always had.
Callers that DO know pull the richer objects via the private attrs.

DRY-RUN semantics: dry_run=True skips the Gate 4 LLM call (auto-APPROVE, conf=0).
Default = True to protect the cost freeze. Flip via env OUROBOROS_LIVE_LLM=1.

Alpaca credentials read from env: APCA_API_KEY_ID, APCA_API_SECRET_KEY.
If unset AND no fixture client is injected, scan_symbol returns ScanResult(
error="Alpaca credentials not configured") — no crash, no silent failure.
"""
from __future__ import annotations

import datetime
import os
import time
from typing import Any

# --- Package imports (SMC layer) -----------------------------------------
try:
    from .bar_aggregator import Bar, BarSeries, FixtureBarClient, get_bars
    from .fvg_detector import detect_fvg
    from .trend_engine import analyze_trend
    from .gate_pipeline import run_gates
    from .position_sizer import compute_position
    from .types import (
        FVGDirection, FVGRegime, FVGResult, FVGBoundaries,
        LLMVerdict, PipelineResult, PositionSpec, TrendContext,
    )
except ImportError:  # standalone run (python src/smc/scanner.py)
    from smc.bar_aggregator import Bar, BarSeries, FixtureBarClient, get_bars
    from smc.fvg_detector import detect_fvg
    from smc.trend_engine import analyze_trend
    from smc.gate_pipeline import run_gates
    from smc.position_sizer import compute_position
    from smc.types import (
        FVGDirection, FVGRegime, FVGResult, FVGBoundaries,
        LLMVerdict, PipelineResult, PositionSpec, TrendContext,
    )

# --- ScanResult (existing contract) --------------------------------------
try:
    # async_scanner's neighbour dir; keep the import stable
    from src.core.scanner.scan_result import ScanResult
except ImportError:
    try:
        from core.scanner.scan_result import ScanResult
    except ImportError:
        # Ultimate fallback: define an equivalent locally so this module is
        # standalone-usable in tests. Downstream code that binds to the real
        # ScanResult is unaffected because the real one wins in production.
        from dataclasses import dataclass
        from typing import Optional
        @dataclass
        class ScanResult:  # type: ignore[no-redef]
            ticker: str
            asset_class: str
            fvg: Optional[dict]
            price: float
            scan_ms: float
            error: Optional[str]

# --- Constants -----------------------------------------------------------
_LOOKBACK_MINUTES_15M = 15 * 60      # 15 hours of 15m bars = 60 candles
_LOOKBACK_MINUTES_5M  = 5 * 60       # 5  hours of 5m  bars = 60 candles
_MIN_BARS_FOR_FVG     = 3
_MIN_BARS_FOR_TREND   = 20           # trend_engine needs enough history


def _resolve_regime() -> str:
    """Macro regime for Gate 2. Reads OUROBOROS_REGIME env var; defaults NEUTRAL.
    Wire this to your real VIX/SPY snapshot later; kept env-driven so heartbeat
    can inject the current regime without re-reading the market on every scan.
    """
    return os.environ.get("OUROBOROS_REGIME", "NEUTRAL").upper()


def _fvg_regime_from_macro(macro: str) -> FVGRegime:
    """Map Gate 2 macro regime -> fvg_detector threshold regime.
    (Both share the same names — spec-aligned; no mapping layer needed.)"""
    try:
        return FVGRegime(macro)
    except ValueError:
        return FVGRegime.NEUTRAL


def _shim_fvg_dict(fvg_result: FVGResult) -> dict | None:
    """Convert FVGResult -> the {"type","size","price"} dict downstream expects.
    Backward compatible with the old yfinance find_imbalances() shape."""
    sig = fvg_result.signal
    if sig is None:
        return None
    kind = "BULL_FVG" if sig.direction == FVGDirection.BULL else "BEAR_FVG"
    return {
        "type":  kind,
        "size":  float(sig.gap_high - sig.gap_low),  # price units, matches old code
        "price": float(sig.entry_price),
    }


def _fvg_boundaries(fvg_result: FVGResult) -> FVGBoundaries | None:
    """Extract FVGBoundaries for the position sizer from a passed FVGResult."""
    sig = fvg_result.signal
    if sig is None:
        return None
    # FVGSignal fields: gap_high, gap_low, entry_price
    return FVGBoundaries(
        gap_top=float(sig.gap_high),
        gap_bottom=float(sig.gap_low),
        entry_price=float(sig.entry_price),
    )


def scan_symbol(
    ticker: str,
    asset_class: str = "Equity",
    *,
    client: Any = None,
    dry_run: bool | None = None,
    regime: str | None = None,
    lookback_15m_minutes: int = _LOOKBACK_MINUTES_15M,
    lookback_5m_minutes: int = _LOOKBACK_MINUTES_5M,
    now: datetime.datetime | None = None,
) -> ScanResult:
    """Scan one symbol through the full SMC pipeline. Preserves ScanResult contract.

    dry_run: if None, reads OUROBOROS_LIVE_LLM env var (default dry_run=True).
    client:  Alpaca-compatible client. If None, get_bars falls back to Alpaca
             credentials from env (APCA_API_KEY_ID/APCA_API_SECRET_KEY). If
             those are also unset AND no fixture is injected, we return an
             error ScanResult rather than crashing.
    """
    t0 = time.monotonic()

    # Guard: no live creds and no injected client == cannot scan
    have_creds = bool(os.environ.get("APCA_API_KEY_ID")) and bool(os.environ.get("APCA_API_SECRET_KEY"))
    if client is None and not have_creds:
        return ScanResult(
            ticker=ticker, asset_class=asset_class, fvg=None, price=0.0,
            scan_ms=round((time.monotonic() - t0) * 1000, 1),
            error="Alpaca credentials not configured (APCA_API_KEY_ID / APCA_API_SECRET_KEY)",
        )

    # Resolve dry_run: env var wins if arg is None
    if dry_run is None:
        dry_run = os.environ.get("OUROBOROS_LIVE_LLM", "0") != "1"

    macro_regime = (regime or _resolve_regime()).upper()

    try:
        now = now or datetime.datetime.now(datetime.timezone.utc)
        end_15m = now
        start_15m = end_15m - datetime.timedelta(minutes=lookback_15m_minutes)
        end_5m = now
        start_5m = end_5m - datetime.timedelta(minutes=lookback_5m_minutes)

        # Fetch both timeframes
        bars_15m = get_bars(
            ticker, "15m",
            start=start_15m, end=end_15m,
            client=client,
            api_key=os.environ.get("APCA_API_KEY_ID", ""),
            secret_key=os.environ.get("APCA_API_SECRET_KEY", ""),
            feed="iex",
        )
        bars_5m = get_bars(
            ticker, "5m",
            start=start_5m, end=end_5m,
            client=client,
            api_key=os.environ.get("APCA_API_KEY_ID", ""),
            secret_key=os.environ.get("APCA_API_SECRET_KEY", ""),
            feed="iex",
        )

        if len(bars_15m.bars) < _MIN_BARS_FOR_FVG:
            return ScanResult(
                ticker=ticker, asset_class=asset_class, fvg=None, price=0.0,
                scan_ms=round((time.monotonic() - t0) * 1000, 1),
                error=f"Not enough 15m bars ({len(bars_15m.bars)}) for FVG detection",
            )

        # Last known price: from the freshest bar we have (5m > 15m)
        latest_bar = (bars_5m.bars[-1] if bars_5m.bars else bars_15m.bars[-1])
        price = float(latest_bar.close)

        # Step 1: FVG detection on 15m (HTF)
        fvg_result = detect_fvg(bars_15m, _fvg_regime_from_macro(macro_regime))

        # Step 2: trend context from both TFs (only if we have enough history)
        trend_ctx: TrendContext | None = None
        if len(bars_15m.bars) >= _MIN_BARS_FOR_TREND and len(bars_5m.bars) >= _MIN_BARS_FOR_TREND:
            try:
                trend_ctx = analyze_trend(bars_15m, bars_5m)
            except Exception:
                # trend is enrichment; don't fail the whole scan if it errors
                trend_ctx = None

        # Step 3: gate pipeline
        pipeline = run_gates(
            ticker=ticker,
            fvg_result=fvg_result,
            trend_context=trend_ctx,
            regime=macro_regime,
            position_size=350.0,   # coarse pre-sizing estimate for Gate 4 payload
            dry_run=dry_run,
        )

        # Step 4: position sizing (only if pipeline passed and FVG present)
        position: PositionSpec | None = None
        boundaries = _fvg_boundaries(fvg_result)
        if boundaries is not None:
            direction = "BULL" if (fvg_result.signal and
                                    fvg_result.signal.direction == FVGDirection.BULL) else "BEAR"
            position = compute_position(
                regime=macro_regime,
                pipeline_result=pipeline,
                fvg=boundaries,
                direction=direction,
            )

        # Build the ScanResult (contract-preserved)
        result = ScanResult(
            ticker=ticker,
            asset_class=asset_class,
            fvg=_shim_fvg_dict(fvg_result),
            price=price,
            scan_ms=round((time.monotonic() - t0) * 1000, 1),
            error=None,
        )
        # Attach SMC richness as private attrs (opt-in for aware consumers)
        setattr(result, "_pipeline", pipeline)
        setattr(result, "_position", position)
        setattr(result, "_trend", trend_ctx)
        return result

    except Exception as exc:
        return ScanResult(
            ticker=ticker, asset_class=asset_class, fvg=None, price=0.0,
            scan_ms=round((time.monotonic() - t0) * 1000, 1),
            error=f"{type(exc).__name__}: {exc}",
        )


# --------------------------------------------------------------------------
# Self-test — offline, uses FixtureBarClient. No API keys required.
# --------------------------------------------------------------------------
if __name__ == "__main__":
    print("=" * 60)
    print("src/smc/scanner.py self-test (fixtures, no API)")
    print("=" * 60)

    base = datetime.datetime(2024, 1, 2, 9, 30, tzinfo=datetime.timezone.utc)
    # Build a bullish-FVG fixture: bar[i-1].high < bar[i+1].low
    bars_15 = [
        Bar(open=100.0, high=100.5, low=99.0,  close=99.5,  volume=1000,
            timestamp=base + datetime.timedelta(minutes=15 * i))
        for i in range(3)
    ]
    # Force the gap
    bars_15[2] = Bar(open=101.5, high=102.0, low=101.0, close=101.8, volume=1500,
                     timestamp=base + datetime.timedelta(minutes=30))
    bars_5 = [
        Bar(open=101.8, high=102.1, low=101.7, close=101.9, volume=200,
            timestamp=base + datetime.timedelta(minutes=30 + 5 * i))
        for i in range(3)
    ]

    # FixtureBarClient exposes get_stock_bars(request) -> list of bars.
    # Our patched get_bars short-circuits when client is provided.
    client_15 = FixtureBarClient(bars_15)
    client_5  = FixtureBarClient(bars_5)

    # Test 1: scan with universe-whitelisted ticker (SPY), fixtures inject bars
    # We can't inject a per-timeframe fixture with the current interface, so use
    # a single client returning the 15m fixture (5m fetch will overshoot but
    # detect_fvg only reads bars_15m).
    result = scan_symbol("SPY", asset_class="ETF-Equity",
                        client=client_15, dry_run=True, regime="BULL")
    print(f"ticker={result.ticker} error={result.error}")
    print(f"fvg={result.fvg}")
    print(f"price={result.price}")
    assert result.error is None, f"unexpected error: {result.error}"
    assert result.fvg is not None, "expected a BULL FVG to be detected"
    assert result.fvg["type"] == "BULL_FVG"
    assert result.fvg["size"] > 0
    print("[PASS] scan_symbol produces BULL_FVG dict compatible with old contract")

    pipe = getattr(result, "_pipeline", None)
    assert pipe is not None and pipe.passed, f"expected pipeline pass, got {pipe}"
    assert pipe.gate4 is not None and pipe.gate4.verdict == LLMVerdict.APPROVE
    print("[PASS] pipeline passed all 5 gates (dry-run APPROVE)")

    pos = getattr(result, "_position", None)
    assert pos is not None and pos.shares > 0, f"expected shares > 0, got {pos}"
    print(f"[PASS] position sizer emitted spec: {pos.shares} sh @ {pos.entry}")

    # Test 2: missing creds path
    for var in ("APCA_API_KEY_ID", "APCA_API_SECRET_KEY"):
        os.environ.pop(var, None)
    r2 = scan_symbol("SPY", asset_class="ETF-Equity", client=None, dry_run=True)
    assert r2.error is not None and "credentials" in r2.error.lower()
    print("[PASS] missing-creds path returns error ScanResult (no crash)")

    # Test 3: gate-rejected ticker (not in universe)
    r3 = scan_symbol("XYZQ", asset_class="Equity", client=client_15, dry_run=True)
    p3 = getattr(r3, "_pipeline", None)
    assert p3 is not None and not p3.passed and p3.reject_gate == 0
    print("[PASS] unknown ticker rejected at Gate 0")

    print("=" * 60)
    print("All selftests passed.")
    print("=" * 60)
