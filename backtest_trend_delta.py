#!/usr/bin/env python3
"""
backtest_trend_delta.py — Trend-Alignment Correlation Study

Question: do trades that ALIGN with multi-TF trend win more than trades
that fight it?

Method (deterministic, zero LLM calls, zero API cost beyond free IEX data):
  1. Load every closed trade from data/trades_ledger.json
  2. For each, reconstruct TrendContext as of its entry_time
  3. Classify: was the trade direction WITH or AGAINST the 15m HTF bias?
  4. Cross-tabulate alignment vs WIN/LOSS, report win rates + edge

Skips trades where IEX has no bars for that date (old history, thin tickers).
Reports coverage explicitly — the result rests only on scorable trades.

Usage:
  python3 backtest_trend_delta.py
  python3 backtest_trend_delta.py --verbose   # per-trade detail
"""

import os
import sys
import json
import time
import argparse
from datetime import datetime, timedelta, timezone

from dotenv import load_dotenv
load_dotenv(dotenv_path=os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"))

import requests

# Reuse the production engine's pure functions — single source of truth
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from src.trend.multi_tf import (
    _ema, _atr, _swing_bias, _slope_bias, _combine,
    _detect_choch, _momentum, _regime, _BASE,
)

LEDGER = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                      "data", "trades_ledger.json")
_KEY = os.getenv("ALPACA_API_KEY", "").strip()
_SEC = os.getenv("ALPACA_API_SECRET", "").strip()
_HEADERS = {"APCA-API-KEY-ID": _KEY, "APCA-API-SECRET-KEY": _SEC}


def fetch_bars_window(ticker, timeframe, end_iso, lookback_bars=60):
    """
    Fetch `lookback_bars` of history ENDING at end_iso (the trade's entry time).
    This reconstructs what the trend engine would have seen at entry —
    no lookahead bias: we only use bars at or before entry.
    """
    end = datetime.fromisoformat(end_iso.replace(" ", "T"))
    if end.tzinfo is None:
        end = end.replace(tzinfo=timezone.utc)
    # Window wide enough to contain lookback_bars of the given timeframe
    minutes = 15 if timeframe == "15Min" else 5
    start = end - timedelta(minutes=minutes * lookback_bars * 3)  # 3x for market gaps
    try:
        r = requests.get(
            f"{_BASE}/stocks/{ticker}/bars",
            params={"timeframe": timeframe, "limit": lookback_bars,
                    "feed": "iex", "sort": "asc",
                    "start": start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "end":   end.strftime("%Y-%m-%dT%H:%M:%SZ")},
            headers=_HEADERS, timeout=10,
        )
        r.raise_for_status()
        return r.json().get("bars") or []
    except Exception:
        return []


def trend_at_entry(ticker, entry_time):
    """Reconstruct (htf_15m_bias, ltf_5m_bias, momentum) as of entry_time."""
    b15 = fetch_bars_window(ticker, "15Min", entry_time)
    b5  = fetch_bars_window(ticker, "5Min",  entry_time)
    if len(b15) < 20 or len(b5) < 20:
        return None  # insufficient data — skip this trade

    c15 = [b["c"] for b in b15]; h15 = [b["h"] for b in b15]; l15 = [b["l"] for b in b15]
    c5  = [b["c"] for b in b5]

    bias15 = _combine(_swing_bias(h15, l15), _slope_bias(c15))
    # 5m bias needs its own highs/lows
    h5 = [b["h"] for b in b5]; l5 = [b["l"] for b in b5]
    bias5  = _combine(_swing_bias(h5, l5), _slope_bias(c5))
    mom    = _momentum(c5)
    return {"htf": bias15, "ltf": bias5, "momentum": mom}


def direction_aligns(trade_direction, htf_bias):
    """LONG aligns with BULL HTF; SHORT aligns with BEAR HTF."""
    if htf_bias == "RANGE":
        return "NEUTRAL"
    if trade_direction == "LONG"  and htf_bias == "BULL":
        return "WITH"
    if trade_direction == "SHORT" and htf_bias == "BEAR":
        return "WITH"
    return "AGAINST"


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--verbose", action="store_true")
    args = ap.parse_args()

    if not _KEY or not _SEC:
        print("FATAL: ALPACA_API_KEY / ALPACA_API_SECRET not loaded from .env")
        sys.exit(1)

    with open(LEDGER) as f:
        trades = json.load(f)["trades"]

    print(f"Loaded {len(trades)} trades from ledger.")
    print("Reconstructing trend context at each entry (IEX free tier)...\n")

    buckets = {
        "WITH":    {"WIN": 0, "LOSS": 0, "pnl": 0.0},
        "AGAINST": {"WIN": 0, "LOSS": 0, "pnl": 0.0},
        "NEUTRAL": {"WIN": 0, "LOSS": 0, "pnl": 0.0},
    }
    scored = 0
    skipped_data = 0
    skipped_outcome = 0

    for i, t in enumerate(trades):
        ticker    = t.get("ticker")
        direction = t.get("direction")
        outcome   = t.get("outcome")
        entry     = t.get("entry_time")
        pnl       = t.get("pnl_pct", 0.0)

        if outcome not in ("WIN", "LOSS"):
            skipped_outcome += 1
            continue
        if not (ticker and direction and entry):
            skipped_data += 1
            continue

        tc = trend_at_entry(ticker, entry)
        time.sleep(0.15)  # gentle rate limiting — 2 calls/trade

        if tc is None:
            skipped_data += 1
            if args.verbose:
                print(f"  [skip] {ticker:5} {entry[:10]} — no IEX bars")
            continue

        align = direction_aligns(direction, tc["htf"])
        buckets[align][outcome] += 1
        buckets[align]["pnl"]   += pnl
        scored += 1

        if args.verbose:
            mark = "✓" if outcome == "WIN" else "✗"
            print(f"  {mark} {ticker:5} {direction:5} {entry[:16]} | "
                  f"HTF={tc['htf']:5} → {align:8} | {outcome} {pnl:+.2f}%")

    # ── Report ────────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("  TREND-ALIGNMENT CORRELATION RESULTS")
    print("=" * 60)
    print(f"  Trades in ledger : {len(trades)}")
    print(f"  Scored           : {scored}")
    print(f"  Skipped (no data): {skipped_data}")
    print(f"  Skipped (open/EOD): {skipped_outcome}")
    print("-" * 60)

    def winrate(b):
        n = b["WIN"] + b["LOSS"]
        return (b["WIN"] / n * 100) if n else 0.0, n

    for label in ("WITH", "AGAINST", "NEUTRAL"):
        b = buckets[label]
        wr, n = winrate(b)
        avg_pnl = (b["pnl"] / n) if n else 0.0
        print(f"  {label:8} trend : {b['WIN']:3}W {b['LOSS']:3}L  "
              f"| win rate {wr:5.1f}%  | avg P&L {avg_pnl:+.2f}%  (n={n})")

    print("-" * 60)
    wr_with, n_with     = winrate(buckets["WITH"])
    wr_against, n_against = winrate(buckets["AGAINST"])
    if n_with and n_against:
        edge = wr_with - wr_against
        print(f"  EDGE (WITH − AGAINST win rate): {edge:+.1f} percentage points")
        if edge > 0:
            print(f"  → Trading WITH the 15m trend won {edge:.0f}pp more often.")
        else:
            print(f"  → No positive edge from trend alignment in this sample.")
    else:
        print("  Insufficient data in one or both buckets for edge calc.")
    print("=" * 60)

    if scored < 10:
        print("\n⚠️  Fewer than 10 trades scored — IEX lacks history for most")
        print("   entries (likely 2024 dates). Result is directional, not")
        print("   statistically robust. For a real number, either:")
        print("   (a) run forward on new paper trades as they accumulate, or")
        print("   (b) use a data source with deeper intraday history.")


if __name__ == "__main__":
    main()
