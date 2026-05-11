"""
Ouroboros v2 — Backtest Report Generator
Reads a saved backtest JSON and produces a full performance analysis:
win rate, P&L curve, Sharpe ratio, max drawdown, per-asset and per-session breakdown.

Usage:
    python backtest_report.py reports/backtests/backtest_20260510_120000.json
    python backtest_report.py --latest          # auto-find most recent file
"""

import json
import os
import sys
import argparse
import math
from datetime import datetime


# ---------------------------------------------------------------------------
# Load
# ---------------------------------------------------------------------------

def load_results(path: str) -> tuple[list[dict], dict]:
    with open(path) as f:
        data = json.load(f)
    return data["trades"], data.get("config", {})


def find_latest_report(out_dir: str = "reports/backtests") -> str:
    files = [
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if f.startswith("backtest_") and f.endswith(".json")
    ]
    if not files:
        raise FileNotFoundError(f"No backtest files in {out_dir}")
    return max(files, key=os.path.getmtime)


# ---------------------------------------------------------------------------
# Metrics
# ---------------------------------------------------------------------------

def sharpe_ratio(returns: list[float], risk_free: float = 0.0) -> float:
    """Annualised Sharpe ratio from a list of per-trade returns (%)."""
    if len(returns) < 2:
        return 0.0
    n = len(returns)
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    # Annualise assuming ~252 trading days, ~3 trades/day avg
    ann_factor = math.sqrt(252 * 3)
    return round((mean - risk_free) / std * ann_factor, 4)


def max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """Returns (max_drawdown_usd, max_drawdown_pct) from an equity curve."""
    peak = equity_curve[0]
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = peak - v
        dd_pct = dd / peak * 100 if peak else 0
        if dd > max_dd_usd:
            max_dd_usd = dd
            max_dd_pct = dd_pct
    return round(max_dd_usd, 2), round(max_dd_pct, 4)


def profit_factor(wins_pnl: list[float], losses_pnl: list[float]) -> float:
    gross_profit = sum(wins_pnl)
    gross_loss = abs(sum(losses_pnl))
    return round(gross_profit / gross_loss, 4) if gross_loss else float("inf")


# ---------------------------------------------------------------------------
# Report
# ---------------------------------------------------------------------------

def generate_report(trades: list[dict], config: dict, path: str):
    if not trades:
        print("No trades to analyse.")
        return

    starting_capital = config.get("starting_capital", 3000)
    position_size = config.get("position_size_usd", 450)

    wins   = [t for t in trades if t["outcome"] == "WIN"]
    losses = [t for t in trades if t["outcome"] == "LOSS"]
    opens  = [t for t in trades if t["outcome"] == "OPEN"]

    total = len(trades)
    win_rate = len(wins) / total * 100 if total else 0

    wins_pnl   = [t["pnl_usd"] for t in wins]
    losses_pnl = [t["pnl_usd"] for t in losses]
    all_pnl    = [t["pnl_usd"] for t in trades]

    net_pnl     = sum(all_pnl)
    avg_win     = sum(wins_pnl) / len(wins_pnl) if wins_pnl else 0
    avg_loss    = sum(losses_pnl) / len(losses_pnl) if losses_pnl else 0
    pf          = profit_factor(wins_pnl, losses_pnl)
    all_returns = [t["pnl_pct"] for t in trades]
    sharpe      = sharpe_ratio(all_returns)

    # Equity curve
    equity = [starting_capital]
    for t in trades:
        equity.append(equity[-1] + t["pnl_usd"])
    max_eq  = max(equity)
    min_eq  = min(equity)
    final_eq = equity[-1]
    total_return_pct = (final_eq - starting_capital) / starting_capital * 100
    max_dd_usd, max_dd_pct = max_drawdown(equity)

    # Per-ticker breakdown
    ticker_stats: dict[str, dict] = {}
    for t in trades:
        tk = t["ticker"]
        if tk not in ticker_stats:
            ticker_stats[tk] = {"wins": 0, "losses": 0, "pnl": 0.0, "asset_class": t["asset_class"]}
        if t["outcome"] == "WIN":
            ticker_stats[tk]["wins"] += 1
        elif t["outcome"] == "LOSS":
            ticker_stats[tk]["losses"] += 1
        ticker_stats[tk]["pnl"] += t["pnl_usd"]

    # Per-session breakdown
    session_stats: dict[str, dict] = {}
    for t in trades:
        s = t["session"]
        if s not in session_stats:
            session_stats[s] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t["outcome"] == "WIN":
            session_stats[s]["wins"] += 1
        elif t["outcome"] == "LOSS":
            session_stats[s]["losses"] += 1
        session_stats[s]["pnl"] += t["pnl_usd"]

    # Per-FVG type
    fvg_stats: dict[str, dict] = {}
    for t in trades:
        ft = t["fvg_type"]
        if ft not in fvg_stats:
            fvg_stats[ft] = {"wins": 0, "losses": 0, "pnl": 0.0}
        if t["outcome"] == "WIN":
            fvg_stats[ft]["wins"] += 1
        elif t["outcome"] == "LOSS":
            fvg_stats[ft]["losses"] += 1
        fvg_stats[ft]["pnl"] += t["pnl_usd"]

    # -------------------------------------------------------------------------
    # Print report
    # -------------------------------------------------------------------------
    sep  = "=" * 62
    dash = "-" * 62

    print(f"\n{sep}")
    print(f"  OUROBOROS v2 — BACKTEST PERFORMANCE REPORT")
    print(f"  Source: {os.path.basename(path)}")
    print(f"  Period: {config.get('start_date')} → {config.get('end_date')}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{sep}\n")

    print(f"{'OVERVIEW':}")
    print(f"{dash}")
    print(f"  Starting capital     : ${starting_capital:,.2f}")
    print(f"  Final equity         : ${final_eq:,.2f}")
    print(f"  Net P&L              : ${net_pnl:+,.2f}  ({total_return_pct:+.2f}%)")
    print(f"  Peak equity          : ${max_eq:,.2f}")
    print(f"  Min equity           : ${min_eq:,.2f}")
    print()

    print(f"TRADE STATISTICS")
    print(f"{dash}")
    print(f"  Total signals        : {total}")
    print(f"  Wins                 : {len(wins)}  ({win_rate:.1f}%)")
    print(f"  Losses               : {len(losses)}  ({100 - win_rate:.1f}%)")
    print(f"  Still open           : {len(opens)}")
    print(f"  Avg win              : ${avg_win:+.4f}")
    print(f"  Avg loss             : ${avg_loss:+.4f}")
    print(f"  Profit factor        : {pf:.2f}x")
    print(f"  Sharpe ratio (ann.)  : {sharpe:.4f}")
    print(f"  Max drawdown         : ${max_dd_usd:,.2f}  ({max_dd_pct:.2f}%)")
    print()

    print(f"BY TICKER")
    print(f"{dash}")
    print(f"  {'Ticker':<10} {'Asset Class':<22} {'W':>4} {'L':>4} {'Win%':>6} {'P&L':>10}")
    print(f"  {'-'*10} {'-'*22} {'----':>4} {'----':>4} {'------':>6} {'----------':>10}")
    for tk, s in sorted(ticker_stats.items(), key=lambda x: -x[1]["pnl"]):
        t_total = s["wins"] + s["losses"]
        wr = s["wins"] / t_total * 100 if t_total else 0
        print(f"  {tk:<10} {s['asset_class']:<22} {s['wins']:>4} {s['losses']:>4} {wr:>5.1f}% {s['pnl']:>+10.2f}")
    print()

    print(f"BY SESSION")
    print(f"{dash}")
    print(f"  {'Session':<15} {'W':>4} {'L':>4} {'Win%':>6} {'P&L':>10}")
    print(f"  {'-'*15} {'----':>4} {'----':>4} {'------':>6} {'----------':>10}")
    for sn, s in sorted(session_stats.items(), key=lambda x: -x[1]["pnl"]):
        t_total = s["wins"] + s["losses"]
        wr = s["wins"] / t_total * 100 if t_total else 0
        print(f"  {sn:<15} {s['wins']:>4} {s['losses']:>4} {wr:>5.1f}% {s['pnl']:>+10.2f}")
    print()

    print(f"BY FVG TYPE")
    print(f"{dash}")
    for ft, s in sorted(fvg_stats.items(), key=lambda x: -x[1]["pnl"]):
        t_total = s["wins"] + s["losses"]
        wr = s["wins"] / t_total * 100 if t_total else 0
        print(f"  {ft:<15}  W:{s['wins']}  L:{s['losses']}  Win%:{wr:.1f}%  P&L:${s['pnl']:+.2f}")
    print()

    # Equity curve (ASCII sparkline)
    print(f"EQUITY CURVE (ASCII)")
    print(f"{dash}")
    step = max(1, len(equity) // 50)
    sampled = equity[::step]
    lo, hi = min(sampled), max(sampled)
    height = 8
    bars = "▁▂▃▄▅▆▇█"
    if hi > lo:
        normalized = [(v - lo) / (hi - lo) for v in sampled]
    else:
        normalized = [0.5] * len(sampled)
    sparkline = "".join(bars[int(v * (len(bars) - 1))] for v in normalized)
    print(f"  ${lo:,.0f} {'_'*3} {sparkline} {'_'*3} ${hi:,.0f}")
    print(f"  Start: ${equity[0]:,.2f}  →  End: ${equity[-1]:,.2f}")
    print()

    print(f"VERDICT")
    print(f"{dash}")
    if win_rate >= 55 and pf >= 1.5 and sharpe >= 0.5:
        print(f"  ✅ STRATEGY VIABLE — Win rate {win_rate:.1f}%, PF {pf:.2f}x, Sharpe {sharpe:.2f}")
    elif win_rate >= 45 and pf >= 1.0:
        print(f"  ⚠️  MARGINAL — Review session/ticker filters before going live")
    else:
        print(f"  ❌ UNDERPERFORMING — Win rate {win_rate:.1f}%, PF {pf:.2f}x — do not deploy")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("path", nargs="?", help="Path to backtest JSON file")
    p.add_argument("--latest", action="store_true", help="Auto-load most recent backtest")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    if args.latest or not args.path:
        path = find_latest_report()
        print(f"Loading: {path}")
    else:
        path = args.path

    trades, config = load_results(path)
    generate_report(trades, config, path)
