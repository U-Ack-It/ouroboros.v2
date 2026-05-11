"""
Ouroboros v2 — Daily Summary

Reads today's orders log + portfolio ledger and pushes a P&L digest
to Telegram. Designed to be run as a cron job at market close.

Usage:
    python daily_summary.py              # sends to Telegram + prints to stdout
    python daily_summary.py --print-only # stdout only, no Telegram
    python daily_summary.py --test       # sends a test message to verify the bot works

Cron (run at 4:30 PM ET every weekday):
    30 16 * * 1-5 cd /home/u-ack-it/Projects/ouroboros.v2 && venv/bin/python3.14 daily_summary.py >> logs/daily_summary.log 2>&1
"""

import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta

sys.path.insert(0, os.getcwd())

from portfolio.ledger import load_trades
from portfolio.analytics import compute_all
from src.notifications.telegram import TelegramNotifier


ORDERS_LOG = "logs/orders.json"


# ---------------------------------------------------------------------------
# Stats builders
# ---------------------------------------------------------------------------

def _sparkline(equity: list[float], width: int = 30) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if len(equity) < 2:
        return "—"
    step = max(1, len(equity) // width)
    sampled = equity[::step]
    lo, hi = min(sampled), max(sampled)
    if hi == lo:
        return bars[3] * len(sampled)
    norm = [(v - lo) / (hi - lo) for v in sampled]
    return "".join(bars[int(v * (len(bars) - 1))] for v in norm)


def build_stats() -> dict:
    today_str = date.today().isoformat()
    all_trades = load_trades()

    # All-time analytics
    global_stats = compute_all(all_trades) if all_trades else {}

    # Today's resolved trades
    today_trades = [
        t for t in all_trades
        if t.get("entry_time", "")[:10] == today_str
        and t.get("outcome") in ("WIN", "LOSS")
    ]
    wins_today   = sum(1 for t in today_trades if t["outcome"] == "WIN")
    losses_today = sum(1 for t in today_trades if t["outcome"] == "LOSS")
    pnl_today    = sum(t.get("pnl_usd", 0.0) for t in today_trades)

    # Today's orders log (includes dry-run)
    approved_today = 0
    blocked_today  = 0
    try:
        if os.path.exists(ORDERS_LOG):
            with open(ORDERS_LOG) as f:
                orders = json.load(f)
            today_orders = [o for o in orders if o.get("submitted_at", "")[:10] == today_str]
            approved_today = sum(1 for o in today_orders if o.get("success"))
            blocked_today  = sum(1 for o in today_orders if not o.get("success"))
    except Exception:
        pass

    equity_curve = global_stats.get("equity_curve", [])
    spark = _sparkline(equity_curve) if equity_curve else "—"

    return {
        "date":             date.today().strftime("%a %b %d, %Y"),
        "signals_scanned":  approved_today + blocked_today,
        "approved":         approved_today,
        "blocked":          blocked_today,
        "wins_today":       wins_today,
        "losses_today":     losses_today,
        "pnl_today":        pnl_today,
        "equity":           global_stats.get("final_equity",     0.0),
        "win_rate_alltime": global_stats.get("win_rate",          0.0),
        "pnl_alltime":      global_stats.get("net_pnl",           0.0),
        "pnl_pct_alltime":  global_stats.get("total_return_pct",  0.0),
        "max_dd_pct":       global_stats.get("max_dd_pct",        0.0),
        "sparkline":        spark,
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 Daily Summary")
    p.add_argument("--print-only", action="store_true", help="Print to stdout only — no Telegram")
    p.add_argument("--test",       action="store_true", help="Send a test ping to verify bot config")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    notifier = TelegramNotifier()

    if args.test:
        notifier.send("🤖 <b>Ouroboros bot test</b> — connection OK ✅")
        print("Test message sent (check Telegram).")
        sys.exit(0)

    stats = build_stats()

    # Print to stdout
    print(f"\n{'='*50}")
    print(f"OUROBOROS DAILY SUMMARY — {stats['date']}")
    print(f"{'='*50}")
    print(f"  Approved trades  : {stats['approved']}")
    print(f"  Today W/L        : {stats['wins_today']}W / {stats['losses_today']}L")
    print(f"  Today P&L        : ${stats['pnl_today']:+.2f}")
    print(f"  Equity           : ${stats['equity']:,.2f}")
    print(f"  All-time P&L     : ${stats['pnl_alltime']:+.2f} ({stats['pnl_pct_alltime']:+.2f}%)")
    print(f"  Win rate         : {stats['win_rate_alltime']:.1f}%")
    print(f"  Max drawdown     : {stats['max_dd_pct']:.2f}%")
    print(f"  Equity curve     : {stats['sparkline']}")
    print(f"{'='*50}\n")

    if not args.print_only:
        notifier.send_daily_summary(stats)
        if notifier.enabled:
            print("Daily summary sent to Telegram.")
        else:
            print("Telegram not configured — printed to stdout only.")
