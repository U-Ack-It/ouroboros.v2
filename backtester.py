"""
Ouroboros v2 — SMC Backtester
Replays Fair Value Gap signals across historical OHLC data and simulates
bracket order outcomes (1% SL / 2% TP) matching live trading logic.

Usage:
    python backtester.py                          # uses config/backtest_config.json
    python backtester.py --ticker GLD --days 90   # quick single-ticker run
    python backtester.py --report                 # print last saved results
"""

import json
import os
import sys
import argparse
from datetime import datetime, timezone
from dataclasses import dataclass, asdict
from typing import Optional

import pandas as pd
import yfinance as yf


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CONFIG_PATH = "config/backtest_config.json"
POLICY_PATH = "config/risk_policy.json"


def load_config(path: str = CONFIG_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


def load_policy(path: str = POLICY_PATH) -> dict:
    with open(path) as f:
        return json.load(f)


# ---------------------------------------------------------------------------
# Session filter
# ---------------------------------------------------------------------------

def in_session(dt: datetime, sessions: dict) -> Optional[str]:
    """Returns session name if the timestamp falls in a high-volume window, else None."""
    hour = dt.hour
    for name, window in sessions.items():
        if window["start_hour"] <= hour <= window["end_hour"]:
            return name
    return None


# ---------------------------------------------------------------------------
# FVG Detection (mirrors scanner.py logic, applied to DataFrame slices)
# ---------------------------------------------------------------------------

def detect_fvg(c1: pd.Series, c2: pd.Series, c3: pd.Series) -> Optional[dict]:
    """
    3-candle SMC Fair Value Gap detection.
    Returns FVG dict or None.
    """
    # Bullish FVG: gap between c1 high and c3 low
    if c3["Low"] > c1["High"]:
        gap_size = c3["Low"] - c1["High"]
        entry_price = c2["Low"]  # Fill level
        return {
            "type": "BULL_FVG",
            "size": round(gap_size, 6),
            "entry_price": round(entry_price, 6),
            "direction": "LONG",
        }

    # Bearish FVG: gap between c3 high and c1 low
    if c3["High"] < c1["Low"]:
        gap_size = c1["Low"] - c3["High"]
        entry_price = c2["High"]
        return {
            "type": "BEAR_FVG",
            "size": round(gap_size, 6),
            "entry_price": round(entry_price, 6),
            "direction": "SHORT",
        }

    return None


# ---------------------------------------------------------------------------
# Trade simulation
# ---------------------------------------------------------------------------

@dataclass
class TradeResult:
    ticker: str
    asset_class: str
    session: str
    entry_time: str
    entry_price: float
    direction: str
    fvg_type: str
    fvg_size: float
    stop_loss: float
    take_profit: float
    exit_price: float
    exit_time: str
    outcome: str          # WIN | LOSS | OPEN
    pnl_usd: float
    pnl_pct: float
    position_size_usd: float
    shares: float


def simulate_trade(
    ticker: str,
    asset_class: str,
    session: str,
    fvg: dict,
    entry_bar: pd.Series,
    future_bars: pd.DataFrame,
    position_size_usd: float,
    stop_loss_pct: float,
    take_profit_pct: float,
) -> TradeResult:
    """
    Simulates a bracket order from the FVG entry.
    Scans forward candle-by-candle to determine if TP or SL is hit first.
    """
    direction = fvg["direction"]
    entry_price = fvg["entry_price"]
    entry_time = str(entry_bar.name)
    shares = round(position_size_usd / entry_price, 4) if entry_price > 0 else 0

    if direction == "LONG":
        tp = round(entry_price * (1 + take_profit_pct), 6)
        sl = round(entry_price * (1 - stop_loss_pct), 6)
    else:
        tp = round(entry_price * (1 - take_profit_pct), 6)
        sl = round(entry_price * (1 + stop_loss_pct), 6)

    exit_price = entry_price
    exit_time = entry_time
    outcome = "OPEN"

    for idx, bar in future_bars.iterrows():
        if direction == "LONG":
            if bar["Low"] <= sl:
                exit_price = sl
                exit_time = str(idx)
                outcome = "LOSS"
                break
            if bar["High"] >= tp:
                exit_price = tp
                exit_time = str(idx)
                outcome = "WIN"
                break
        else:
            if bar["High"] >= sl:
                exit_price = sl
                exit_time = str(idx)
                outcome = "LOSS"
                break
            if bar["Low"] <= tp:
                exit_price = tp
                exit_time = str(idx)
                outcome = "WIN"
                break

    if direction == "LONG":
        pnl_usd = round((exit_price - entry_price) * shares, 4)
    else:
        pnl_usd = round((entry_price - exit_price) * shares, 4)

    pnl_pct = round(pnl_usd / position_size_usd * 100, 4) if position_size_usd else 0

    return TradeResult(
        ticker=ticker,
        asset_class=asset_class,
        session=session,
        entry_time=entry_time,
        entry_price=entry_price,
        direction=direction,
        fvg_type=fvg["type"],
        fvg_size=fvg["size"],
        stop_loss=sl,
        take_profit=tp,
        exit_price=exit_price,
        exit_time=exit_time,
        outcome=outcome,
        pnl_usd=pnl_usd,
        pnl_pct=pnl_pct,
        position_size_usd=position_size_usd,
        shares=shares,
    )


# ---------------------------------------------------------------------------
# Main backtest engine
# ---------------------------------------------------------------------------

def backtest_ticker(
    ticker: str,
    asset_class: str,
    config: dict,
    verbose: bool = True,
) -> list[TradeResult]:
    """
    Downloads historical data and replays FVG detection + bracket simulation.
    Returns list of TradeResult for every signal fired.
    """
    start = config["start_date"]
    end = config["end_date"]
    interval = config["interval"]
    sessions = config["sessions"]
    session_filter = config.get("session_filter", True)
    position_size_usd = config["position_size_usd"]
    sl_pct = config["stop_loss_pct"]
    tp_pct = config["take_profit_pct"]
    max_per_session = config["max_trades_per_session"]

    if verbose:
        print(f"  Fetching {ticker} ({asset_class}) [{start} → {end}] {interval}...")

    try:
        # yfinance 15m data limited to last 60 days — use 1h for longer periods
        if interval == "15m":
            fetch_interval = "1h"
        else:
            fetch_interval = interval

        df = yf.Ticker(ticker).history(start=start, end=end, interval=fetch_interval)
        if df.empty or len(df) < 3:
            if verbose:
                print(f"  ⚠️  {ticker}: insufficient data — skipping")
            return []
    except Exception as e:
        if verbose:
            print(f"  ❌ {ticker}: data fetch failed — {e}")
        return []

    # Ensure UTC-naive index for comparison
    idx = pd.to_datetime(df.index)
    df.index = idx.tz_convert(None) if idx.tz is not None else idx

    results = []
    daily_session_counts: dict[str, dict[str, int]] = {}  # date -> session -> count
    i = 0

    while i < len(df) - 3:
        c1, c2, c3 = df.iloc[i], df.iloc[i + 1], df.iloc[i + 2]
        bar_time: datetime = df.index[i + 2].to_pydatetime()
        date_str = bar_time.strftime("%Y-%m-%d")

        # Session filter
        session_name = in_session(bar_time, sessions)
        if session_filter and session_name is None:
            i += 1
            continue

        session_key = session_name or "all"

        # Enforce max trades per session per day
        day_counts = daily_session_counts.setdefault(date_str, {})
        session_count = day_counts.get(session_key, 0)
        if session_count >= max_per_session:
            i += 1
            continue

        fvg = detect_fvg(c1, c2, c3)
        if fvg is None:
            i += 1
            continue

        # Forward bars for bracket simulation (next 20 bars)
        future = df.iloc[i + 3: i + 23]
        if future.empty:
            break

        trade = simulate_trade(
            ticker=ticker,
            asset_class=asset_class,
            session=session_key,
            fvg=fvg,
            entry_bar=c3,
            future_bars=future,
            position_size_usd=position_size_usd,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
        )

        results.append(trade)
        day_counts[session_key] = session_count + 1

        # Skip ahead past the signal to avoid overlapping trades on same signal
        i += 3

    if verbose:
        wins = sum(1 for r in results if r.outcome == "WIN")
        print(f"  → {len(results)} signals | {wins} wins | {len(results)-wins} losses")

    return results


def run_backtest(config: dict, policy: dict, verbose: bool = True) -> list[TradeResult]:
    asset_map = policy.get("neutrality_priority", {}).get("asset_mapping", {})
    tickers = config.get("tickers") or list(asset_map.keys())

    all_results: list[TradeResult] = []

    print(f"\n{'='*60}")
    print(f"OUROBOROS v2 — BACKTESTER")
    print(f"Period : {config['start_date']} → {config['end_date']}")
    print(f"Tickers: {', '.join(tickers)}")
    print(f"{'='*60}\n")

    for ticker in tickers:
        asset_class = asset_map.get(ticker, "Unknown")
        results = backtest_ticker(ticker, asset_class, config, verbose)
        all_results.extend(results)

    return all_results


# ---------------------------------------------------------------------------
# Save results
# ---------------------------------------------------------------------------

def save_results(results: list[TradeResult], config: dict) -> str:
    out_dir = config.get("output_dir", "reports/backtests")
    os.makedirs(out_dir, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    out_path = os.path.join(out_dir, f"backtest_{timestamp}.json")

    data = {
        "generated_at": datetime.now().isoformat(),
        "config": config,
        "total_trades": len(results),
        "trades": [asdict(r) for r in results],
    }

    with open(out_path, "w") as f:
        json.dump(data, f, indent=2)

    print(f"\nResults saved → {out_path}")
    return out_path


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 SMC Backtester")
    p.add_argument("--ticker", help="Single ticker override")
    p.add_argument("--days", type=int, help="Lookback days (overrides config dates)")
    p.add_argument("--interval", default=None, help="OHLC interval (e.g. 1h, 1d)")
    p.add_argument("--no-session-filter", action="store_true", help="Disable session window filter")
    p.add_argument("--quiet", action="store_true", help="Suppress per-ticker output")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    config = load_config()
    policy = load_policy()

    if args.ticker:
        config["tickers"] = [args.ticker]

    if args.days:
        end_dt = datetime.now()
        start_dt = end_dt - __import__("timedelta", fromlist=["timedelta"]).__class__(days=args.days)
        from datetime import timedelta
        start_dt = datetime.now() - timedelta(days=args.days)
        config["start_date"] = start_dt.strftime("%Y-%m-%d")
        config["end_date"] = datetime.now().strftime("%Y-%m-%d")

    if args.interval:
        config["interval"] = args.interval

    if args.no_session_filter:
        config["session_filter"] = False

    results = run_backtest(config, policy, verbose=not args.quiet)
    out_path = save_results(results, config)

    # Quick summary
    if results:
        wins = [r for r in results if r.outcome == "WIN"]
        losses = [r for r in results if r.outcome == "LOSS"]
        total_pnl = sum(r.pnl_usd for r in results)
        win_rate = len(wins) / len(results) * 100

        print(f"\n{'='*60}")
        print(f"QUICK SUMMARY")
        print(f"{'='*60}")
        print(f"Total signals : {len(results)}")
        print(f"Wins          : {len(wins)}  ({win_rate:.1f}%)")
        print(f"Losses        : {len(losses)}")
        print(f"Net P&L       : ${total_pnl:+.2f}")
        print(f"{'='*60}")
        print(f"\nRun `python backtest_report.py {out_path}` for full analysis.")
