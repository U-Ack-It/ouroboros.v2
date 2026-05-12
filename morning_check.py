"""
Ouroboros v2 — Morning Check

Run once before each session to verify all systems are green.
Prints account status, open positions, today's P&L, regime, and Telegram.

Usage:
    venv/bin/python3.14 morning_check.py
"""

import json
import os
import sys
from datetime import date, datetime
from pathlib import Path

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from src.core.broker.alpaca_client import AlpacaClient
from src.notifications.telegram import TelegramNotifier


def _bar(label: str, value: str, width: int = 22):
    return f"  {label:<{width}} {value}"


def morning_check():
    print("\n" + "=" * 50)
    print("  OUROBOROS v2 — MORNING CHECK")
    print("=" * 50)

    issues = []

    # ── 1. Alpaca connection ─────────────────────────────────────────
    print("\n[ BROKER ]")
    client = AlpacaClient()
    ok, msg = client.connect()
    if ok:
        import requests
        r = requests.get(
            f"{client._base}/v2/account",
            headers={
                "APCA-API-KEY-ID":     client._api_key,
                "APCA-API-SECRET-KEY": client._api_secret,
            },
            timeout=10,
        )
        d = r.json()
        cash         = float(d.get("cash", 0))
        buying_power = float(d.get("buying_power", 0))
        portfolio    = float(d.get("portfolio_value", 0))
        status       = d.get("status", "?").upper()
        mode         = "PAPER" if "paper" in client._base else "LIVE"

        print(_bar("Mode:",         mode))
        print(_bar("Status:",       status))
        print(_bar("Cash:",         f"${cash:,.2f}"))
        print(_bar("Buying power:", f"${buying_power:,.2f}"))
        print(_bar("Portfolio:",    f"${portfolio:,.2f}"))

        if status != "ACTIVE":
            issues.append(f"Alpaca account status: {status}")
        if cash < 100:
            issues.append("Cash below $100")
    else:
        print(f"  ❌ {msg}")
        issues.append(msg)

    # ── 2. Open positions ────────────────────────────────────────────
    print("\n[ OPEN POSITIONS ]")
    try:
        from portfolio.ledger import load_open_positions
        open_pos = load_open_positions()
        if not open_pos:
            print("  None")
        else:
            for p in open_pos:
                print(_bar(
                    f"{p.get('ticker','?')} {p.get('direction','?')}",
                    f"entry=${p.get('entry_price',0):.2f}  "
                    f"TP=${p.get('take_profit',0):.2f}  "
                    f"SL=${p.get('stop_loss',0):.2f}"
                ))
    except Exception as e:
        print(f"  (ledger unavailable: {e})")

    # ── 3. Today's P&L ──────────────────────────────────────────────
    print("\n[ TODAY'S P&L ]")
    try:
        from portfolio.ledger import load_trades
        today = date.today().isoformat()
        trades = [
            t for t in load_trades()
            if t.get("entry_time", "")[:10] == today
            and t.get("outcome") in ("WIN", "LOSS")
        ]
        wins   = sum(1 for t in trades if t["outcome"] == "WIN")
        losses = sum(1 for t in trades if t["outcome"] == "LOSS")
        pnl    = sum(float(t.get("pnl_usd", 0)) for t in trades)
        print(_bar("Trades:", f"{wins}W / {losses}L"))
        print(_bar("P&L:",    f"${pnl:+,.2f}"))
    except Exception as e:
        print(f"  (unavailable: {e})")

    # ── 4. Regime snapshot ──────────────────────────────────────────
    print("\n[ REGIME ]")
    snap_path = Path("logs/regime_snapshot.json")
    if snap_path.exists():
        try:
            snap = json.loads(snap_path.read_text())
            fetched = datetime.fromisoformat(snap["fetched_at"])
            age_min = (datetime.now() - fetched).total_seconds() / 60
            label   = snap.get("label", "?")
            vix     = snap.get("vix", 0)
            fresh   = "✅" if age_min < 30 else "⚠️ STALE"
            print(_bar("Label:",   label))
            print(_bar("VIX:",     f"{vix:.1f}"))
            print(_bar("Age:",     f"{age_min:.0f} min {fresh}"))
            if age_min > 60:
                issues.append("Regime snapshot older than 60 min — heartbeat may not be running")
        except Exception as e:
            print(f"  (parse error: {e})")
    else:
        print("  No snapshot yet — fires on first heartbeat tick")

    # ── 5. Telegram ─────────────────────────────────────────────────
    print("\n[ TELEGRAM ]")
    notifier = TelegramNotifier()
    if notifier.enabled:
        print("  ✅ Connected — @electrocointek_v2_bot")
    else:
        print("  ❌ Not configured")
        issues.append("Telegram not configured")

    # ── 6. Config ───────────────────────────────────────────────────
    print("\n[ CONFIG ]")
    try:
        cfg = json.loads(Path("config/execution_config.json").read_text())
        dry = cfg.get("dry_run", True)
        print(_bar("dry_run:",        str(dry)))
        print(_bar("max_daily_trades:", str(cfg.get("max_daily_trades", "?"))))
        print(_bar("daily_loss_limit:", f"${cfg.get('daily_loss_limit_usd', 0):,.0f}"))
        print(_bar("TP / SL:",         f"{cfg.get('take_profit_pct',0)*100:.0f}% / {cfg.get('stop_loss_pct',0)*100:.0f}%"))
        if dry:
            issues.append("dry_run=true — no real orders will be placed")
    except Exception as e:
        print(f"  (unavailable: {e})")

    # ── Summary ─────────────────────────────────────────────────────
    print("\n" + "=" * 50)
    if not issues:
        print("  🟢 ALL SYSTEMS GO — ARMED FOR SESSION")
        notifier.send("🌅 <b>Ouroboros Morning Check</b>\n🟢 All systems go — armed for session.")
    else:
        print("  ⚠️  ISSUES DETECTED:")
        for i in issues:
            print(f"    • {i}")
    print("=" * 50 + "\n")


if __name__ == "__main__":
    morning_check()
