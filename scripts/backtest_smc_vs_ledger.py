#!/usr/bin/env python3
"""
scripts/backtest_smc_vs_ledger.py

Replays historical trades through the SMC pipeline to measure edge.

For each trade in data/trades_ledger.json:
  1. Fetch Alpaca 15m + 5m bars ending at trade.entry_time
  2. Run full SMC pipeline (fvg -> trend -> gates -> sizer) as of that moment
  3. Record: did SMC approve? which gate rejected? what was Gate 4 verdict?
  4. Compare SMC verdict against actual trade outcome (WIN/LOSS)

Emits precision/recall/F1 attribution:
  - Precision: of trades SMC approved, how many won
  - Recall: of trades that won, how many did SMC catch
  - Edge: SMC precision minus baseline win rate

Usage:
  export APCA_API_KEY_ID=...
  export APCA_API_SECRET_KEY=...
  python3 scripts/backtest_smc_vs_ledger.py --limit 20   # smoke test
  python3 scripts/backtest_smc_vs_ledger.py              # full 116

Cost: 0 dollars (dry_run=True, no Gate 4 LLM calls).
Rate: ~2 API calls per trade at 300ms interval = ~2 min for full run.

Assumptions:
  - Ledger is data/trades_ledger.json (JSON, not SQLite)
  - Alpaca has historical bars for the trade's ticker + entry window
  - IEX free tier is enough (SIP not required for historical > 30min old)
  - Regime override via OUROBOROS_REGIME env var (backtest injects it)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))
sys.path.insert(0, str(REPO_ROOT))

from smc.bar_aggregator import get_bars, Bar, BarSeries  # noqa: E402
from smc.fvg_detector   import detect_fvg               # noqa: E402
from smc.trend_engine   import analyze_trend            # noqa: E402
from smc.gate_pipeline  import run_gates                # noqa: E402
from smc.position_sizer import compute_position         # noqa: E402
from smc.types import (  # noqa: E402
    FVGDirection, FVGRegime, FVGBoundaries, LLMVerdict,
    TrendContext, PipelineResult, PositionSpec,
)

LEDGER_PATH        = REPO_ROOT / "data" / "trades_ledger.json"
API_CALL_DELAY_SEC = 0.30           # stay under 200/min Alpaca free-tier limit
LOOKBACK_15M_HRS   = 5 * 24         # 5 days of history to cover weekends
LOOKBACK_5M_HRS    = 5 * 24
END_LAG_MIN        = 30             # IEX free tier restricts recent SIP data


# ---------------------------------------------------------------------------
# Result rows
# ---------------------------------------------------------------------------
@dataclass
class TradeResult:
    trade_id:        str
    ticker:          str
    entry_time:      str
    direction:       str
    outcome:         str   # WIN | LOSS | UNKNOWN
    pnl_pct:         float
    smc_verdict:     str   # APPROVED | REJECTED | ERROR
    reject_gate:     int | None
    gate4_verdict:   str | None
    gate4_conf:      float | None
    fvg_matched:     bool  # did SMC detect same direction as ledger
    trend_alignment: str | None
    error:           str | None = None


# ---------------------------------------------------------------------------
# Ledger IO
# ---------------------------------------------------------------------------
def load_ledger(limit: int | None = None) -> list[dict]:
    with open(LEDGER_PATH) as f:
        raw = json.load(f)
    trades = raw.get("trades", raw) if isinstance(raw, dict) else raw
    if limit:
        trades = trades[:limit]
    return trades


# ---------------------------------------------------------------------------
# Ticker whitelist expansion — the ledger has tickers Gate 0 doesn't know
# ---------------------------------------------------------------------------
def collect_ledger_tickers(trades: list[dict]) -> set[str]:
    return {t["ticker"].upper() for t in trades if t.get("ticker")}


# ---------------------------------------------------------------------------
# Historical bar fetch for a specific past moment
# ---------------------------------------------------------------------------
def _fetch_bars_at(
    ticker: str,
    entry_ts: dt.datetime,
    timeframe: str,
    lookback_hours: int,
    api_key: str,
    secret_key: str,
) -> BarSeries:
    """Fetch bars ending just before entry_ts (mimics real-time scan at that moment)."""
    end = entry_ts - dt.timedelta(minutes=END_LAG_MIN)
    start = end - dt.timedelta(hours=lookback_hours)
    return get_bars(
        ticker, timeframe,
        start=start, end=end,
        api_key=api_key, secret_key=secret_key,
        feed="iex",
    )


# ---------------------------------------------------------------------------
# Replay one trade
# ---------------------------------------------------------------------------
def replay_trade(
    trade: dict,
    api_key: str,
    secret_key: str,
    extra_whitelist: set[str],
) -> TradeResult:
    tid       = trade.get("id", "?")
    ticker    = trade["ticker"].upper()
    outcome   = trade.get("outcome", "UNKNOWN").upper()
    pnl_pct   = float(trade.get("pnl_pct", 0.0))
    direction = trade.get("direction", "").upper()   # LONG | SHORT
    entry_str = trade.get("entry_time", "")

    # Parse entry time; ledger stores 'YYYY-MM-DD HH:MM:SS' in UTC
    try:
        entry_ts = dt.datetime.fromisoformat(entry_str.replace(" ", "T"))
        if entry_ts.tzinfo is None:
            entry_ts = entry_ts.replace(tzinfo=dt.timezone.utc)
    except Exception as exc:
        return TradeResult(
            tid, ticker, entry_str, direction, outcome, pnl_pct,
            "ERROR", None, None, None, False, None,
            error=f"bad entry_time: {exc}",
        )

    # Fetch historical bars
    try:
        bars_15m = _fetch_bars_at(ticker, entry_ts, "15m", LOOKBACK_15M_HRS,
                                   api_key, secret_key)
        time.sleep(API_CALL_DELAY_SEC)
        bars_5m = _fetch_bars_at(ticker, entry_ts, "5m", LOOKBACK_5M_HRS,
                                  api_key, secret_key)
        time.sleep(API_CALL_DELAY_SEC)
    except Exception as exc:
        return TradeResult(
            tid, ticker, entry_str, direction, outcome, pnl_pct,
            "ERROR", None, None, None, False, None,
            error=f"bar fetch failed: {exc}",
        )

    if len(bars_15m.bars) < 3:
        return TradeResult(
            tid, ticker, entry_str, direction, outcome, pnl_pct,
            "ERROR", None, None, None, False, None,
            error=f"insufficient 15m bars ({len(bars_15m.bars)})",
        )

    # Detect FVG
    fvg_result = detect_fvg(bars_15m, FVGRegime.NEUTRAL)

    # FVG direction match: ledger direction LONG needs BULL_FVG, SHORT needs BEAR_FVG
    fvg_matched = False
    if fvg_result.signal is not None:
        want = FVGDirection.BULL if direction == "LONG" else FVGDirection.BEAR
        fvg_matched = fvg_result.signal.direction == want

    # Trend context (best-effort). Pass ledger direction so alignment can
    # compute WITH/AGAINST rather than falling into the "no FVG direction
    # → NEUTRAL" branch.
    ledger_fvg_dir = "BULL" if direction == "LONG" else "BEAR"
    trend_ctx: TrendContext | None = None
    try:
        if len(bars_15m.bars) >= 20 and len(bars_5m.bars) >= 20:
            trend_ctx = analyze_trend(bars_15m, bars_5m, fvg_direction=ledger_fvg_dir)
    except Exception:
        trend_ctx = None

    # Run gates. Widen whitelist so ledger tickers aren't Gate-0 rejected.
    # Regime = NEUTRAL as coarse default; per-trade macro replay is next-step polish.
    # Pass ledger direction so Gate 6 can filter direction disagreements.
    intended = "BULL" if direction == "LONG" else "BEAR"
    pipeline = run_gates(
        ticker=ticker,
        fvg_result=fvg_result,
        trend_context=trend_ctx,
        regime="NEUTRAL",
        position_size=350.0,
        config={"additional_whitelist": list(extra_whitelist)},
        dry_run=True,
        intended_direction=intended,
    )

    verdict     = "APPROVED" if pipeline.passed else "REJECTED"
    reject_gate = pipeline.reject_gate
    g4_verdict  = pipeline.gate4.verdict.value if pipeline.gate4 else None
    g4_conf     = pipeline.gate4.confidence if pipeline.gate4 else None
    alignment   = trend_ctx.alignment.value if trend_ctx else None

    return TradeResult(
        tid, ticker, entry_str, direction, outcome, pnl_pct,
        verdict, reject_gate, g4_verdict, g4_conf, fvg_matched, alignment,
    )


# ---------------------------------------------------------------------------
# Attribution report
# ---------------------------------------------------------------------------
def _pct(n: int, d: int) -> str:
    return f"{100 * n / d:.1f}%" if d else "n/a"


def print_report(rows: list[TradeResult]) -> None:
    total   = len(rows)
    errors  = [r for r in rows if r.smc_verdict == "ERROR"]
    valid   = [r for r in rows if r.smc_verdict in ("APPROVED", "REJECTED")]

    wins    = [r for r in valid if r.outcome == "WIN"]
    losses  = [r for r in valid if r.outcome == "LOSS"]

    approved     = [r for r in valid if r.smc_verdict == "APPROVED"]
    rejected     = [r for r in valid if r.smc_verdict == "REJECTED"]

    tp = [r for r in approved if r.outcome == "WIN"]      # true positive
    fp = [r for r in approved if r.outcome == "LOSS"]     # false positive
    fn = [r for r in rejected if r.outcome == "WIN"]      # false negative (missed edge)
    tn = [r for r in rejected if r.outcome == "LOSS"]     # true negative (correctly blocked)

    print("=" * 72)
    print("SMC PIPELINE vs LEDGER — BACKTEST REPORT")
    print("=" * 72)
    print(f"Total trades:       {total}")
    print(f"Fetch errors:       {len(errors)}")
    print(f"Valid comparisons:  {len(valid)}")
    print()

    if not valid:
        print("[No valid comparisons — check API keys, ticker availability]")
        for e in errors[:5]:
            print(f"  {e.ticker:6} {e.entry_time}: {e.error}")
        return

    baseline_wr = len(wins) / len(valid) if valid else 0
    print(f"Baseline win rate:  {_pct(len(wins), len(valid))}   ({len(wins)}W / {len(losses)}L)")
    print()

    print("Confusion matrix:")
    print(f"                     Actual WIN     Actual LOSS")
    print(f"  SMC APPROVED       {len(tp):>10}    {len(fp):>10}")
    print(f"  SMC REJECTED       {len(fn):>10}    {len(tn):>10}")
    print()

    if approved:
        smc_wr = len(tp) / len(approved)
        print(f"SMC-approved win rate:  {_pct(len(tp), len(approved))}   ({len(tp)}W / {len(fp)}L / n={len(approved)})")
        print(f"Edge vs baseline:       {100*(smc_wr - baseline_wr):+.1f} pp")
    else:
        print("SMC approved 0 trades — pipeline is over-selective.")
    if rejected:
        print(f"SMC-rejected win rate:  {_pct(len(fn), len(rejected))}   ({len(fn)}W / {len(tn)}L / n={len(rejected)})")
        print(f"  (winning trades SMC would have blocked — opportunity cost)")
    print()

    if wins:
        recall = len(tp) / len(wins)
        print(f"Recall (winning trades caught):       {_pct(len(tp), len(wins))}")
    if approved:
        precision = len(tp) / len(approved)
        print(f"Precision (approvals that won):       {_pct(len(tp), len(approved))}")
        if wins and approved:
            p, r = precision, len(tp) / len(wins)
            if p + r > 0:
                f1 = 2 * p * r / (p + r)
                print(f"F1:                                   {f1:.3f}")
    print()

    # Gate-level rejection breakdown
    print("Rejection attribution (which gate killed winners):")
    winner_rejects = [r for r in rejected if r.outcome == "WIN"]
    from collections import Counter
    gate_counts = Counter(r.reject_gate for r in winner_rejects)
    for gate_id, cnt in sorted(gate_counts.items(), key=lambda x: -x[1]):
        label = {0: "universe", 1: "ethics", 2: "regime",
                 3: "fvg", 4: "gate4-llm"}.get(gate_id, f"gate-{gate_id}")
        print(f"  Gate {gate_id} ({label:<10}): blocked {cnt} winners")
    print()

    # Direction agreement analysis
    dir_matched = [r for r in valid if r.fvg_matched]
    dir_matched_wins = [r for r in dir_matched if r.outcome == "WIN"]
    if dir_matched:
        print(f"FVG direction matched ledger direction:  {len(dir_matched)}/{len(valid)} = {_pct(len(dir_matched), len(valid))}")
        print(f"  Of those, win rate:                    {_pct(len(dir_matched_wins), len(dir_matched))}")
    print()

    # Trend alignment vs outcome
    from collections import defaultdict
    align_pnl = defaultdict(list)
    for r in valid:
        if r.trend_alignment:
            align_pnl[r.trend_alignment].append(r.pnl_pct)
    if align_pnl:
        print("Trend alignment vs avg PnL%:")
        for alignment, pnls in align_pnl.items():
            avg = sum(pnls) / len(pnls) if pnls else 0
            print(f"  {alignment:<10}  n={len(pnls):<3}  avg pnl_pct={avg:+.2f}%")
    print()

    # Errors detail
    if errors:
        print(f"First 5 errors (of {len(errors)}):")
        for e in errors[:5]:
            print(f"  {e.ticker:6} {e.entry_time}: {e.error}")
    print("=" * 72)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=None,
                    help="Only replay the first N trades (smoke test)")
    ap.add_argument("--out", default="logs/backtest_smc_report.json",
                    help="Path to write full per-trade results as JSON")
    args = ap.parse_args()

    api_key    = os.environ.get("APCA_API_KEY_ID", "")
    secret_key = os.environ.get("APCA_API_SECRET_KEY", "")
    if not api_key or not secret_key:
        print("ERROR: APCA_API_KEY_ID and APCA_API_SECRET_KEY must be set.")
        return 2

    trades = load_ledger(limit=args.limit)
    print(f"Loaded {len(trades)} trades from {LEDGER_PATH}")

    # Auto-widen whitelist so ledger tickers pass Gate 0
    extra_whitelist = collect_ledger_tickers(trades)
    print(f"Auto-adding {len(extra_whitelist)} ledger tickers to Gate-0 whitelist")

    results: list[TradeResult] = []
    t0 = time.monotonic()
    for i, trade in enumerate(trades, start=1):
        row = replay_trade(trade, api_key, secret_key, extra_whitelist)
        results.append(row)
        # Progress line every 5 trades
        if i % 5 == 0 or i == len(trades):
            elapsed = time.monotonic() - t0
            rate = i / elapsed if elapsed > 0 else 0
            eta = (len(trades) - i) / rate if rate > 0 else 0
            errors = sum(1 for r in results if r.smc_verdict == "ERROR")
            print(f"  [{i:>3}/{len(trades)}] {row.ticker:<6} {row.smc_verdict:<9} "
                  f"outcome={row.outcome:<5}   errors={errors}  ETA={eta:.0f}s")

    print()
    print_report(results)

    # Persist full detail
    out_path = REPO_ROOT / args.out
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with open(out_path, "w") as f:
        json.dump([r.__dict__ for r in results], f, indent=2, default=str)
    print(f"Full per-trade results written to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
