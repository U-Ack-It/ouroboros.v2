"""
Ouroboros v2 — Portfolio Analytics Report

Reads the persistent trade ledger and produces rolling-window performance
analysis: Sharpe, Sortino, drawdown curves, per-asset-class breakdown.

Usage:
    python portfolio_report.py                        # all-time from ledger
    python portfolio_report.py --since 2025-01-01     # filter by date
    python portfolio_report.py --window 30            # last 30 calendar days
    python portfolio_report.py --import-backtest reports/backtests/backtest_20260510_231531.json
    python portfolio_report.py --import-latest        # import most recent backtest
    python portfolio_report.py --clear                # wipe the ledger (confirm required)
"""

import argparse
import os
import sys
from datetime import datetime

from portfolio.ledger import (
    load_trades, import_backtest, clear_ledger, LEDGER_PATH
)
from portfolio.analytics import compute_all, rolling_metrics


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def find_latest_backtest(out_dir: str = "reports/backtests") -> str:
    files = [
        os.path.join(out_dir, f)
        for f in os.listdir(out_dir)
        if f.startswith("backtest_") and f.endswith(".json")
    ]
    if not files:
        raise FileNotFoundError(f"No backtest files in {out_dir}")
    return max(files, key=os.path.getmtime)


def sparkline(values: list[float], width: int = 50) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if len(values) < 2:
        return "—"
    step = max(1, len(values) // width)
    sampled = values[::step]
    lo, hi = min(sampled), max(sampled)
    if hi == lo:
        return bars[3] * len(sampled)
    norm = [(v - lo) / (hi - lo) for v in sampled]
    return "".join(bars[int(v * (len(bars) - 1))] for v in norm)


def dd_sparkline(dd_series: list[float], width: int = 50) -> str:
    """Drawdown series — values are <= 0, show depth visually."""
    if not dd_series:
        return "—"
    bars = "▔▀▄█"  # shallowest to deepest drawdown
    step = max(1, len(dd_series) // width)
    sampled = dd_series[::step]
    lo = min(sampled)  # most negative = deepest
    if lo == 0:
        return "▔" * len(sampled)
    norm = [abs(v / lo) for v in sampled]  # 0 = no dd, 1 = max dd
    return "".join(bars[int(n * (len(bars) - 1))] for n in norm)


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_report(trades: list[dict], starting_capital: float = 3000.0, label: str = "ALL TIME"):
    sep  = "=" * 66
    dash = "-" * 66

    stats = compute_all(trades, starting_capital)
    if "error" in stats:
        print(f"\n  No resolved trades to analyse.")
        return

    roll = rolling_metrics(trades, windows=[7, 30, 90], starting_capital=starting_capital)
    ac   = stats["asset_classes"]

    print(f"\n{sep}")
    print(f"  OUROBOROS v2 — PORTFOLIO ANALYTICS  [{label}]")
    print(f"  Ledger: {LEDGER_PATH}  |  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{sep}\n")

    # Overview
    print(f"OVERVIEW")
    print(f"{dash}")
    print(f"  Starting capital     : ${stats['starting_capital']:>10,.2f}")
    print(f"  Final equity         : ${stats['final_equity']:>10,.2f}")
    print(f"  Net P&L              : ${stats['net_pnl']:>+10,.2f}  ({stats['total_return_pct']:+.2f}%)")
    print(f"  Peak equity          : ${stats['peak_equity']:>10,.2f}")
    print(f"  Min equity           : ${stats['min_equity']:>10,.2f}")
    print()

    # Trade stats
    print(f"TRADE STATISTICS")
    print(f"{dash}")
    print(f"  Total resolved       : {stats['total_resolved']}  ({stats['open']} still open)")
    print(f"  Wins / Losses        : {stats['wins']} / {stats['losses']}  ({stats['win_rate']:.1f}% win rate)")
    print(f"  Avg win              : ${stats['avg_win']:>+.4f}")
    print(f"  Avg loss             : ${stats['avg_loss']:>+.4f}")
    print(f"  Profit factor        : {stats['profit_factor']:.2f}x")
    print()

    # Risk metrics
    print(f"RISK METRICS")
    print(f"{dash}")
    print(f"  Sharpe ratio (ann.)  : {stats['sharpe']:>8.4f}")
    print(f"  Sortino ratio (ann.) : {stats['sortino']:>8.4f}  (downside-only)")
    print(f"  Max drawdown         : ${stats['max_dd_usd']:>8,.2f}  ({stats['max_dd_pct']:.2f}%)")
    print(f"  Avg recovery (bars)  : {stats['avg_recovery_bars']}")
    print()

    # Rolling windows
    print(f"ROLLING WINDOWS")
    print(f"{dash}")
    print(f"  {'Period':<12} {'Trades':>7} {'Win%':>6} {'Net P&L':>10} {'Sharpe':>8} {'Sortino':>8} {'MaxDD%':>8}")
    print(f"  {'-'*12} {'-------':>7} {'------':>6} {'----------':>10} {'--------':>8} {'--------':>8} {'--------':>8}")
    for days, m in roll.items():
        label_str = f"Last {days}d"
        if m["trades"] == 0:
            print(f"  {label_str:<12} {'—':>7} {'—':>6} {'—':>10} {'—':>8} {'—':>8} {'—':>8}")
        else:
            print(
                f"  {label_str:<12} {m['trades']:>7} {m['win_rate']:>5.1f}%"
                f" {m['net_pnl']:>+10.2f} {m['sharpe']:>8.3f} {m['sortino']:>8.3f} {m['max_dd_pct']:>7.2f}%"
            )
    print()

    # Asset class breakdown
    print(f"BY ASSET CLASS")
    print(f"{dash}")
    print(f"  {'Asset Class':<22} {'W':>4} {'L':>4} {'Win%':>6} {'Net P&L':>10} {'Avg Win':>9} {'Avg Loss':>9} {'PF':>6}")
    print(f"  {'-'*22} {'----':>4} {'----':>4} {'------':>6} {'----------':>10} {'--------':>9} {'--------':>9} {'------':>6}")
    for ac_name, s in sorted(ac.items(), key=lambda x: -x[1]["net_pnl"]):
        pf_str = f"{s['pf']:.2f}x" if s['pf'] != float("inf") else "  ∞"
        print(
            f"  {ac_name:<22} {s['wins']:>4} {s['losses']:>4} {s['win_rate']:>5.1f}%"
            f" {s['net_pnl']:>+10.2f} {s['avg_win']:>+9.4f} {s['avg_loss']:>+9.4f} {pf_str:>6}"
        )
    print()

    # Equity curve sparkline
    eq  = stats["equity_curve"]
    dds = stats["dd_series"]
    print(f"EQUITY CURVE")
    print(f"{dash}")
    lo, hi = min(eq), max(eq)
    spark = sparkline(eq)
    print(f"  ${lo:,.0f} {spark} ${hi:,.0f}")
    print(f"  Start ${eq[0]:,.2f}  →  End ${eq[-1]:,.2f}")
    print()

    print(f"DRAWDOWN CURVE  (depth over time)")
    print(f"{dash}")
    min_dd = min(dds)
    dd_spark = dd_sparkline(dds)
    print(f"  0% {'▔'*3} {dd_spark} {'▔'*3} {min_dd:.2f}%")
    print()

    # Verdict
    print(f"VERDICT")
    print(f"{dash}")
    wr = stats["win_rate"]
    pf = stats["profit_factor"]
    sh = stats["sharpe"]
    so = stats["sortino"]
    if wr >= 55 and pf >= 1.5 and sh >= 1.0 and so >= 1.0:
        print(f"  ✅ STRONG — Win {wr:.1f}%, PF {pf:.2f}x, Sharpe {sh:.2f}, Sortino {so:.2f}")
    elif wr >= 50 and pf >= 1.2:
        print(f"  ⚠️  VIABLE — Win {wr:.1f}%, PF {pf:.2f}x — monitor drawdown closely")
    else:
        print(f"  ❌ UNDERPERFORMING — Win {wr:.1f}%, PF {pf:.2f}x — review filters")
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 Portfolio Analytics")
    p.add_argument("--since", help="Filter: only trades since YYYY-MM-DD")
    p.add_argument("--window", type=int, help="Show only the last N calendar days")
    p.add_argument("--capital", type=float, default=3000.0, help="Starting capital (default 3000)")
    p.add_argument("--import-backtest", dest="import_path", help="Import backtest JSON into ledger")
    p.add_argument("--import-latest", action="store_true", help="Import most recent backtest JSON")
    p.add_argument("--clear", action="store_true", help="Wipe the ledger (prompts for confirmation)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()

    # Destructive ops first
    if args.clear:
        confirm = input("This will delete all ledger data. Type YES to confirm: ").strip()
        if confirm == "YES":
            clear_ledger()
            print("Ledger cleared.")
        else:
            print("Aborted.")
        sys.exit(0)

    # Imports
    if args.import_latest:
        path = find_latest_backtest()
        n = import_backtest(path)
        print(f"Imported {n} trades from {os.path.basename(path)}")

    if args.import_path:
        n = import_backtest(args.import_path)
        print(f"Imported {n} trades from {os.path.basename(args.import_path)}")

    # Load & filter
    since = args.since
    if args.window and not since:
        from datetime import timedelta
        since = (datetime.utcnow() - timedelta(days=args.window)).strftime("%Y-%m-%d")

    trades = load_trades(since=since)

    if not trades:
        print(f"\n  Ledger is empty. Run with --import-latest to load backtest data.")
        sys.exit(0)

    label = f"SINCE {since}" if since else "ALL TIME"
    print_report(trades, starting_capital=args.capital, label=label)
