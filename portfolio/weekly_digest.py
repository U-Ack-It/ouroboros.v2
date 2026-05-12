"""
Weekly trade digest — summarizes the past 7 days from the ledger and sends
an HTML summary via Telegram.  Called by heartbeat.py on Sunday mornings.
"""

import sys
import os
from collections import defaultdict
from datetime import date, timedelta

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from portfolio.ledger import load_trades
from src.notifications.telegram import TelegramNotifier


def send_weekly_digest() -> None:
    cutoff = (date.today() - timedelta(days=7)).isoformat()
    trades = [
        t for t in load_trades(since=cutoff)
        if t.get("outcome") in ("WIN", "LOSS")
    ]

    if not trades:
        return

    by_ticker: dict[str, list] = defaultdict(list)
    for t in trades:
        by_ticker[t.get("ticker", "?")].append(t)

    total_pnl = sum(t.get("pnl_usd", 0.0) for t in trades)
    total_w   = sum(1 for t in trades if t.get("outcome") == "WIN")
    total_l   = len(trades) - total_w

    rows = ""
    for ticker in sorted(by_ticker):
        ts    = by_ticker[ticker]
        wins  = sum(1 for t in ts if t.get("outcome") == "WIN")
        losses = len(ts) - wins
        pnl   = sum(t.get("pnl_usd", 0.0) for t in ts)
        color = "#276749" if pnl >= 0 else "#c53030"
        rows += (
            f"<tr>"
            f"<td><b>{ticker}</b></td>"
            f"<td style='text-align:center'>{wins}W / {losses}L</td>"
            f"<td style='text-align:right;color:{color};font-weight:700'>${pnl:+.2f}</td>"
            f"</tr>"
        )

    sign_color = "#276749" if total_pnl >= 0 else "#c53030"

    html = f"""<b>📊 Weekly Digest — {date.today().isoformat()}</b>

<b>{total_w}W / {total_l}L</b> across {len(by_ticker)} ticker(s) in the past 7 days.
Net P&amp;L: <b>${total_pnl:+.2f}</b>

<pre>{'Ticker':<8} {'W/L':<10} {'P&L':>10}
{'-'*30}
"""

    for ticker in sorted(by_ticker):
        ts    = by_ticker[ticker]
        wins  = sum(1 for t in ts if t.get("outcome") == "WIN")
        losses = len(ts) - wins
        pnl   = sum(t.get("pnl_usd", 0.0) for t in ts)
        html += f"{ticker:<8} {f'{wins}W/{losses}L':<10} ${pnl:>+9.2f}\n"

    html += f"{'─'*30}\n{'TOTAL':<8} {f'{total_w}W/{total_l}L':<10} ${total_pnl:>+9.2f}</pre>"

    notifier = TelegramNotifier()
    notifier.send(html)
    print(
        f"[weekly_digest] Sent: {total_w}W / {total_l}L  "
        f"net ${total_pnl:+.2f} across {len(by_ticker)} ticker(s)."
    )
