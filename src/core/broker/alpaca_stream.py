"""
Alpaca Trade Stream — WebSocket listener for real-time order fill notifications.

Connects to Alpaca's trading stream, receives order updates, and fires
Telegram notifications when positions open or close.

Run as a background process alongside heartbeat.py:
    python3 -m src.core.broker.alpaca_stream
"""

import asyncio
import json
import os
import signal
import sys
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent.parent.parent / ".env")

ALPACA_KEY    = os.getenv("ALPACA_API_KEY", "")
ALPACA_SECRET = os.getenv("ALPACA_SECRET_KEY", "")
PAPER         = os.getenv("ALPACA_BASE_URL", "").find("paper") != -1 or True
WS_URL        = (
    "wss://paper-api.alpaca.markets/stream"
    if PAPER else
    "wss://api.alpaca.markets/stream"
)


def _telegram_notify(msg: str):
    try:
        import requests
        token   = os.getenv("TELEGRAM_BOT_TOKEN", "")
        chat_id = os.getenv("TELEGRAM_CHAT_ID", "")
        if not token or not chat_id:
            return
        requests.post(
            f"https://api.telegram.org/bot{token}/sendMessage",
            json={"chat_id": chat_id, "text": msg, "parse_mode": "Markdown"},
            timeout=5,
        )
    except Exception:
        pass


def _format_fill(event: dict) -> str:
    order  = event.get("order", {})
    ticker = order.get("symbol", "?")
    side   = order.get("side", "?").upper()
    qty    = order.get("filled_qty", order.get("qty", "?"))
    price  = order.get("filled_avg_price") or order.get("limit_price", "?")
    status = order.get("status", "?").upper()
    oid    = order.get("id", "")[:8]

    if status == "FILLED":
        return (f"✅ *FILLED* — {side} {qty}x {ticker} @ ${float(price):.2f}\n"
                f"Order: `{oid}`")
    elif status == "PARTIALLY_FILLED":
        return (f"⚡ *PARTIAL FILL* — {side} {qty}x {ticker} @ ${float(price):.2f}\n"
                f"Order: `{oid}`")
    elif status in ("CANCELED", "EXPIRED"):
        return f"❌ *{status}* — {side} {qty}x {ticker} | Order: `{oid}`"
    return ""


async def _stream():
    import websockets

    print(f"[AlpacaStream] Connecting to {WS_URL}...")

    async with websockets.connect(WS_URL) as ws:
        # Authenticate
        await ws.send(json.dumps({
            "action": "authenticate",
            "data": {"key_id": ALPACA_KEY, "secret_key": ALPACA_SECRET}
        }))
        auth_resp = json.loads(await ws.recv())
        if auth_resp.get("data", {}).get("status") != "authorized":
            print(f"[AlpacaStream] Auth failed: {auth_resp}")
            return

        print("[AlpacaStream] Authenticated. Listening for trade updates...")

        # Subscribe to trade updates
        await ws.send(json.dumps({
            "action": "listen",
            "data": {"streams": ["trade_updates"]}
        }))

        async for raw in ws:
            try:
                msg  = json.loads(raw)
                data = msg.get("data", {})

                if msg.get("stream") != "trade_updates":
                    continue

                event_type = data.get("event", "")
                if event_type in ("fill", "partial_fill", "canceled", "expired"):
                    text = _format_fill(data)
                    if text:
                        ts = datetime.now(timezone.utc).strftime("%H:%M UTC")
                        _telegram_notify(f"{text}\n_{ts}_")
                        print(f"[AlpacaStream] {event_type}: {data.get('order', {}).get('symbol')}")

            except Exception as e:
                print(f"[AlpacaStream] Parse error: {e}")


def main():
    if not ALPACA_KEY or not ALPACA_SECRET:
        print("[AlpacaStream] Missing ALPACA_API_KEY or ALPACA_SECRET_KEY in .env")
        sys.exit(1)

    loop = asyncio.new_event_loop()

    def _shutdown(*_):
        print("\n[AlpacaStream] Shutting down...")
        loop.stop()

    signal.signal(signal.SIGINT,  _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    while True:
        try:
            loop.run_until_complete(_stream())
        except Exception as e:
            print(f"[AlpacaStream] Disconnected ({e}) — reconnecting in 10s...")
            import time; time.sleep(10)


if __name__ == "__main__":
    main()
