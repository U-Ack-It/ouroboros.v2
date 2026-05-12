"""
Message formatters for Telegram alerts.

All functions return HTML strings safe for Telegram's parse_mode=HTML.
"""

from datetime import datetime
from typing import Optional


def _esc(text: str) -> str:
    return str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


# ---------------------------------------------------------------------------
# Signal alert (Gate 4 result)
# ---------------------------------------------------------------------------

def fmt_signal(
    ticker: str,
    asset_class: str,
    fvg_type: str,
    price: float,
    session: str,
    verdict: str,
    confidence: float,
    reasoning: str,
    risk_flags: list,
) -> str:
    direction = "LONG 🟢" if fvg_type == "BULL_FVG" else "SHORT 🔴"
    verdict_icon = {"APPROVE": "✅", "CAUTION": "⚠️", "REJECT": "🚫"}.get(verdict, "❓")
    flags_line = f"\n⚑ Flags: {_esc(', '.join(risk_flags))}" if risk_flags else ""

    return (
        f"🔥 <b>OUROBOROS SIGNAL — {_esc(ticker)}</b>\n"
        f"📍 {_esc(asset_class)} | {_esc(session)} | {_esc(fvg_type)}\n"
        f"Direction: {direction}\n"
        f"Price: <code>${price:,.4f}</code>\n"
        f"\n"
        f"Gate 4: {verdict_icon} <b>{_esc(verdict)}</b> (conf={confidence:.2f})\n"
        f"<i>{_esc(reasoning)}</i>"
        f"{flags_line}"
    )


# ---------------------------------------------------------------------------
# Execution result
# ---------------------------------------------------------------------------

def fmt_execution(
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
) -> str:
    mode_icon = "🧪" if dry_run else ("✅" if success else "❌")
    mode_label = "DRY RUN" if dry_run else ("LIVE — PLACED" if success else "LIVE — FAILED")
    dir_icon   = "🟢" if direction == "LONG" else "🔴"

    lines = [
        f"{mode_icon} <b>{_esc(mode_label)}: {_esc(ticker)}</b>",
        f"{dir_icon} {_esc(direction)}  {quantity}x @ <code>${entry_price:,.4f}</code>",
        f"🎯 TP: <code>${tp:,.4f}</code>  |  🛑 SL: <code>${sl:,.4f}</code>",
        f"💰 Position: <code>${position_usd:,.2f}</code>",
    ]
    if order_id and order_id != "DRY-RUN":
        lines.append(f"Order ID: <code>{_esc(order_id)}</code>")
    if not success and not dry_run:
        lines.append(f"⚠️ <i>{_esc(message)}</i>")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Gate block
# ---------------------------------------------------------------------------

def fmt_gate_block(ticker: str, gate: str, reason: str) -> str:
    return (
        f"🚫 <b>{_esc(ticker)} BLOCKED</b>\n"
        f"{_esc(gate)}: {_esc(reason)}"
    )


# ---------------------------------------------------------------------------
# Preflight block (execution-level)
# ---------------------------------------------------------------------------

def fmt_trade_closed(
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
) -> str:
    outcome_icon = "✅ WIN" if outcome == "WIN" else ("❌ LOSS" if outcome == "LOSS" else "⏹ FLAT")
    dir_icon     = "🟢" if direction == "LONG" else "🔴"
    mode         = "🧪 " if dry_run else ""
    pnl_icon     = "📈" if pnl_usd >= 0 else "📉"

    return (
        f"{mode}{outcome_icon} <b>{_esc(ticker)} CLOSED</b> {dir_icon}\n"
        f"Entry : <code>${entry_price:,.4f}</code> → Exit: <code>${exit_price:,.4f}</code>\n"
        f"🎯 TP: <code>${tp:,.4f}</code>  |  🛑 SL: <code>${sl:,.4f}</code>\n"
        f"{pnl_icon} P&L: <code>${pnl_usd:+,.2f}</code> ({pnl_pct:+.2f}%)\n"
        f"{quantity}x @ ${entry_price:,.2f}"
    )


def fmt_preflight_block(ticker: str, reason: str) -> str:
    return (
        f"⏹ <b>{_esc(ticker)}</b> — execution skipped\n"
        f"<i>{_esc(reason)}</i>"
    )


# ---------------------------------------------------------------------------
# Daily summary
# ---------------------------------------------------------------------------

def fmt_daily_summary(stats: dict) -> str:
    """
    stats keys (all optional with defaults):
      date, signals_scanned, approved, blocked,
      wins_today, losses_today, pnl_today,
      equity, win_rate_alltime, pnl_alltime, pnl_pct_alltime,
      max_dd_pct, sparkline
    """
    date_str     = stats.get("date",             datetime.now().strftime("%a %b %d"))
    scanned      = stats.get("signals_scanned",  0)
    approved     = stats.get("approved",         0)
    blocked      = stats.get("blocked",          0)
    wins         = stats.get("wins_today",       0)
    losses       = stats.get("losses_today",     0)
    pnl_today    = stats.get("pnl_today",        0.0)
    equity       = stats.get("equity",           0.0)
    wr_all       = stats.get("win_rate_alltime",  0.0)
    pnl_all      = stats.get("pnl_alltime",       0.0)
    pnl_pct_all  = stats.get("pnl_pct_alltime",   0.0)
    max_dd       = stats.get("max_dd_pct",        0.0)
    spark        = stats.get("sparkline",         "—")

    pnl_today_icon = "📈" if pnl_today >= 0 else "📉"
    pnl_all_icon   = "📈" if pnl_all >= 0 else "📉"

    return (
        f"📊 <b>OUROBOROS DAILY — {_esc(date_str)}</b>\n"
        f"{'─'*30}\n"
        f"Signals scanned : {scanned}\n"
        f"Approved        : {approved}\n"
        f"Blocked         : {blocked}\n"
        f"\n"
        f"Today  {pnl_today_icon}\n"
        f"  Trades  : {wins}W / {losses}L\n"
        f"  P&L     : <code>${pnl_today:+,.2f}</code>\n"
        f"\n"
        f"All-time  {pnl_all_icon}\n"
        f"  Equity  : <code>${equity:,.2f}</code>\n"
        f"  P&L     : <code>${pnl_all:+,.2f}</code> ({pnl_pct_all:+.2f}%)\n"
        f"  Win rate: {wr_all:.1f}%\n"
        f"  Max DD  : {max_dd:.2f}%\n"
        f"\n"
        f"<code>{_esc(spark)}</code>"
    )
