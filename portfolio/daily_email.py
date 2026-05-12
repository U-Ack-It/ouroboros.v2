"""
Daily P&L email — sent at market close (4:30pm ET).
Reads today's closed trades from the ledger and emails a summary.
"""

import base64
import json
import os
import pickle
import sys
from collections import defaultdict
from datetime import date, datetime
from email.mime.text import MIMEText
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent))

from portfolio.ledger import load_trades

TOKEN_PATH  = Path("/home/u-ack-it/gallerysense/token.pickle")
LEDGER_PATH = Path(__file__).parent.parent / "data/trades_ledger.json"
RECIPIENT   = "olivierboukli@gmail.com"


def _gmail_service():
    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)
    from googleapiclient.discovery import build
    return build("gmail", "v1", credentials=creds)


def send_daily_report() -> None:
    today  = date.today().isoformat()
    trades = [
        t for t in load_trades(since=today)
        if t.get("outcome") in ("WIN", "LOSS")
    ]

    open_positions = [
        t for t in load_trades(since=today)
        if t.get("outcome") not in ("WIN", "LOSS", None)
    ]

    if not trades and not open_positions:
        print(f"[daily_email] No activity today ({today}) — skipping.")
        return

    total_pnl = sum(t.get("pnl_usd", 0.0) for t in trades)
    wins      = sum(1 for t in trades if t.get("outcome") == "WIN")
    losses    = len(trades) - wins

    by_ticker: dict = defaultdict(list)
    for t in trades:
        by_ticker[t.get("ticker", "?")].append(t)

    lines = [
        f"OUROBOROS DAILY — {today}",
        f"{'='*44}",
        f"  Closed trades : {len(trades)}  ({wins}W / {losses}L)",
        f"  Net P&L       : ${total_pnl:+.2f}",
        "",
    ]

    if trades:
        lines.append(f"  {'Ticker':<8}  {'Result':<6}  {'P&L':>10}")
        lines.append(f"  {'─'*28}")
        for ticker in sorted(by_ticker):
            ts   = by_ticker[ticker]
            w    = sum(1 for t in ts if t.get("outcome") == "WIN")
            l    = len(ts) - w
            pnl  = sum(t.get("pnl_usd", 0.0) for t in ts)
            lines.append(f"  {ticker:<8}  {w}W/{l}L   ${pnl:>+9.2f}")
        lines.append(f"  {'─'*28}")
        lines.append(f"  {'TOTAL':<8}           ${total_pnl:>+9.2f}")
        lines.append("")

    # Open positions
    if open_positions:
        lines.append(f"  Open positions: {len(open_positions)}")
        for t in open_positions:
            lines.append(f"    {t.get('ticker','?')}  {t.get('direction','?')}  "
                         f"entry={t.get('entry_price','?')}")
        lines.append("")

    # Macro quadrant context
    macro_path = Path(__file__).parent.parent / "logs/macro_quadrant.json"
    if macro_path.exists():
        try:
            mq = json.loads(macro_path.read_text())
            g  = "↑" if mq.get("growth_up") else "↓"
            i  = "↑" if mq.get("inflation_up") else "↓"
            lines.append(f"  Macro: {mq.get('quadrant')} {mq.get('label')}  "
                         f"growth{g}  inflation{i}")
        except Exception:
            pass

    body = "\n".join(lines)
    subject = (
        f"Ouroboros {today}: {wins}W/{losses}L  ${total_pnl:+.2f}"
        if trades else f"Ouroboros {today}: no closed trades"
    )

    service = _gmail_service()
    msg = MIMEText(body)
    msg["to"]      = RECIPIENT
    msg["subject"] = subject
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()
    print(f"[daily_email] Sent: {subject}")


if __name__ == "__main__":
    send_daily_report()
