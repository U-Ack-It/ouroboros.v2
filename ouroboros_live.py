"""
Ouroboros v2 — Live Launcher

Pre-flight check + heartbeat startup in one command.
Verifies Alpaca connection and Telegram before handing off to heartbeat.py.

Usage:
    venv/bin/python3.14 ouroboros_live.py          # check + launch
    venv/bin/python3.14 ouroboros_live.py --check  # check only, don't launch
"""

import os
import sys
import argparse

from dotenv import load_dotenv
load_dotenv()

sys.path.insert(0, os.path.dirname(__file__))

from src.core.broker.alpaca_client import AlpacaClient
from src.notifications.telegram import TelegramNotifier


def preflight() -> bool:
    print("Ouroboros v2 — Pre-flight check")
    print("-" * 34)

    ok_count = 0

    # Alpaca
    client = AlpacaClient()
    ok, msg = client.connect()
    if ok:
        import requests
        r = requests.get(
            f"{client._base}/v2/account",
            headers={"APCA-API-KEY-ID": client._api_key,
                     "APCA-API-SECRET-KEY": client._api_secret},
            timeout=10,
        )
        d    = r.json()
        mode = "PAPER" if "paper" in client._base else "LIVE ⚡"
        cash = float(d.get("cash", 0))
        print(f"  ✅ Alpaca {mode}  cash=${cash:,.2f}")
        ok_count += 1
    else:
        print(f"  ❌ Alpaca: {msg}")

    # Telegram
    notifier = TelegramNotifier()
    if notifier.enabled:
        print("  ✅ Telegram connected")
        ok_count += 1
    else:
        print("  ⚠️  Telegram not configured (continuing anyway)")
        ok_count += 1  # non-fatal

    all_ok = ok_count >= 2
    print("-" * 34)
    print(f"  {'🟢 READY' if all_ok else '🔴 BLOCKED — fix issues above'}")
    print()
    return all_ok


def launch_heartbeat():
    """Hand off to heartbeat.py main loop."""
    print("Starting heartbeat...\n")
    import heartbeat
    heartbeat.run_pulse()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--check", action="store_true", help="Pre-flight check only")
    args = parser.parse_args()

    ready = preflight()

    if args.check or not ready:
        sys.exit(0 if ready else 1)

    launch_heartbeat()
