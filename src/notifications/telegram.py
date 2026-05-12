"""
Telegram Notifier — non-blocking alerts for Ouroboros v2.

Sends HTML-formatted messages via pyTelegramBotAPI.
Degrades silently when TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID are not set
so the trading loop is never blocked by a notification failure.

Usage:
    from src.notifications.telegram import TelegramNotifier
    notifier = TelegramNotifier()
    notifier.send("hello")                       # raw text
    notifier.send_html("<b>hello</b>")           # HTML
    notifier.send_signal(...)                    # structured alert
"""

import os
import threading
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

try:
    import telebot
    _TELEBOT_AVAILABLE = True
except ImportError:
    _TELEBOT_AVAILABLE = False


class TelegramNotifier:
    """
    Thread-safe, non-blocking Telegram notifier.
    Every send() call fires in a daemon thread — the caller never waits.
    """

    def __init__(
        self,
        token: Optional[str] = None,
        chat_id: Optional[str] = None,
    ):
        self._token   = token   or os.getenv("TELEGRAM_BOT_TOKEN",  "").strip()
        self._chat_id = chat_id or os.getenv("TELEGRAM_CHAT_ID",    "").strip()
        self._bot     = None
        self._enabled = False

        if not _TELEBOT_AVAILABLE:
            print("[Telegram] pyTelegramBotAPI not installed — notifications disabled")
            return
        if not self._token:
            print("[Telegram] TELEGRAM_BOT_TOKEN not set — notifications disabled")
            return
        if not self._chat_id:
            print("[Telegram] TELEGRAM_CHAT_ID not set — notifications disabled")
            return

        try:
            self._bot     = telebot.TeleBot(self._token, parse_mode=None)
            self._enabled = True
        except Exception as exc:
            print(f"[Telegram] Init failed: {exc}")

    @property
    def enabled(self) -> bool:
        return self._enabled

    # ------------------------------------------------------------------
    # Core send (always non-blocking)
    # ------------------------------------------------------------------

    def send(self, text: str, parse_mode: str = "HTML") -> None:
        """Fire-and-forget text message. Never raises."""
        if not self._enabled:
            return
        threading.Thread(
            target=self._send_sync,
            args=(text, parse_mode),
            daemon=True,
        ).start()

    def _send_sync(self, text: str, parse_mode: str):
        try:
            # Telegram max 4096 chars — truncate gracefully
            if len(text) > 4090:
                text = text[:4087] + "…"
            self._bot.send_message(self._chat_id, text, parse_mode=parse_mode)
        except Exception as exc:
            print(f"[Telegram] send failed: {exc}")

    # ------------------------------------------------------------------
    # Structured alert builders
    # ------------------------------------------------------------------

    def send_signal(
        self,
        ticker: str,
        asset_class: str,
        fvg_type: str,
        price: float,
        session: str,
        verdict: str,
        confidence: float,
        reasoning: str,
        risk_flags: list,
    ) -> None:
        from src.notifications.messages import fmt_signal
        self.send(fmt_signal(
            ticker, asset_class, fvg_type, price, session,
            verdict, confidence, reasoning, risk_flags,
        ))

    def send_execution(
        self,
        ticker: str,
        direction: str,
        quantity: int,
        entry_price: float,
        tp: float,
        sl: float,
        position_usd: float,
        order_id: Optional[str],
        success: bool,
        dry_run: bool,
        message: str,
    ) -> None:
        from src.notifications.messages import fmt_execution
        self.send(fmt_execution(
            ticker, direction, quantity, entry_price, tp, sl,
            position_usd, order_id, success, dry_run, message,
        ))

    def send_trade_closed(
        self,
        ticker:      str,
        direction:   str,
        quantity:    int,
        entry_price: float,
        exit_price:  float,
        tp:          float,
        sl:          float,
        pnl_usd:     float,
        pnl_pct:     float,
        outcome:     str,
        dry_run:     bool,
    ) -> None:
        from src.notifications.messages import fmt_trade_closed
        self.send(fmt_trade_closed(
            ticker, direction, quantity, entry_price, exit_price,
            tp, sl, pnl_usd, pnl_pct, outcome, dry_run,
        ))

    def send_gate_block(self, ticker: str, gate: str, reason: str) -> None:
        from src.notifications.messages import fmt_gate_block
        self.send(fmt_gate_block(ticker, gate, reason))

    def send_daily_summary(self, stats: dict) -> None:
        from src.notifications.messages import fmt_daily_summary
        self.send(fmt_daily_summary(stats))

    def send_startup(self, watchlist: list[str], dry_run: bool) -> None:
        mode = "🧪 DRY RUN" if dry_run else "⚡ LIVE"
        symbols = ", ".join(watchlist)
        self.send(
            f"🟢 <b>OUROBOROS v2 STARTED</b>\n"
            f"Mode: {mode}\n"
            f"Watching: <code>{symbols}</code>"
        )

    def send_shutdown(self, reason: str = "manual stop") -> None:
        self.send(f"🔴 <b>OUROBOROS v2 STOPPED</b> — {_esc(reason)}")

    def send_error(self, context: str, error: str) -> None:
        self.send(
            f"⚠️ <b>ERROR</b>\n"
            f"<code>{_esc(context)}</code>\n"
            f"{_esc(error)}"
        )


# ---------------------------------------------------------------------------
# HTML escaping helper
# ---------------------------------------------------------------------------

def _esc(text: str) -> str:
    """Escape HTML special chars for Telegram HTML parse mode."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
    )
