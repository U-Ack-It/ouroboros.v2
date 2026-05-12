"""
Ouroboros Telegram Bot — interactive control across all 3 systems.

Commands:
  /status   — all systems health
  /trades   — recent Ouroboros P&L
  /alerts   — current muni bond alerts
  /leads    — top GallerySense leads
  /pause    — pause Ouroboros trade execution
  /resume   — resume Ouroboros trade execution
  /help     — command list
"""

import json
import os
import sys
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent / ".env")

import telebot

TOKEN   = os.getenv("TELEGRAM_BOT_TOKEN", "")
CHAT_ID = os.getenv("TELEGRAM_CHAT_ID",   "")

OUROBOROS = Path(__file__).parent
MUNI      = Path("/home/u-ack-it/Projects/muni_scanner")
GALLERY   = Path("/home/u-ack-it/gallerysense")
PAUSE_FLAG = OUROBOROS / "logs/paused"

sys.path.insert(0, str(OUROBOROS))

bot = telebot.TeleBot(TOKEN)


def _only_owner(msg):
    return str(msg.chat.id) == str(CHAT_ID)


# ── /help ────────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["help", "start"])
def cmd_help(msg):
    if not _only_owner(msg): return
    bot.reply_to(msg,
        "📡 <b>Ouroboros Control</b>\n\n"
        "/status  — all systems\n"
        "/trades  — P&L last 7 days\n"
        "/alerts  — muni bond alerts\n"
        "/leads   — top GallerySense leads\n"
        "/pause   — halt trade execution\n"
        "/resume  — resume trading\n",
        parse_mode="HTML"
    )


# ── /status ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["status"])
def cmd_status(msg):
    if not _only_owner(msg): return
    lines = ["📊 <b>Systems Status</b>\n"]

    # Ouroboros heartbeat
    paused = PAUSE_FLAG.exists()
    log    = OUROBOROS / "logs/heartbeat.log"
    last   = ""
    if log.exists():
        last = log.read_text().strip().splitlines()[-1]
    lines.append(f"<b>Ouroboros</b> {'⏸ PAUSED' if paused else '🟢 running'}")
    if last:
        lines.append(f"  <code>{last[-80:]}</code>")

    # Macro quadrant
    mq_path = OUROBOROS / "logs/macro_quadrant.json"
    if mq_path.exists():
        mq = json.loads(mq_path.read_text())
        g  = "↑" if mq.get("growth_up") else "↓"
        i  = "↑" if mq.get("inflation_up") else "↓"
        lines.append(f"  Macro: {mq.get('quadrant')} growth{g} inflation{i}")

    # muni_scanner
    agent_log = MUNI / "logs/bond_email_agent.log"
    if agent_log.exists():
        last_muni = agent_log.read_text().strip().splitlines()[-1]
        lines.append(f"\n<b>Muni Scanner</b> 🟢 running")
        lines.append(f"  <code>{last_muni[-80:]}</code>")

    # GallerySense
    scores = GALLERY / "leads/scores.json"
    if scores.exists():
        d = json.loads(scores.read_text())
        lines.append(f"\n<b>GallerySense</b> 🟢 running")
        lines.append(f"  HOT:{d.get('hot',0)} WARM:{d.get('warm',0)} "
                     f"COLD:{d.get('cold',0)}  (scored {d.get('scored_at','?')})")

    bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")


# ── /trades ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["trades"])
def cmd_trades(msg):
    if not _only_owner(msg): return
    try:
        from portfolio.ledger import load_trades
        cutoff = (date.today() - timedelta(days=7)).isoformat()
        trades = [t for t in load_trades(since=cutoff)
                  if t.get("outcome") in ("WIN", "LOSS")]
        if not trades:
            bot.reply_to(msg, "No closed trades in the past 7 days.")
            return
        wins      = sum(1 for t in trades if t.get("outcome") == "WIN")
        total_pnl = sum(t.get("pnl_usd", 0.0) for t in trades)
        by_ticker = defaultdict(list)
        for t in trades:
            by_ticker[t.get("ticker", "?")].append(t)
        lines = [
            f"📈 <b>Trades — last 7 days</b>\n"
            f"{len(trades)} trades  {wins}W/{len(trades)-wins}L  "
            f"net <b>${total_pnl:+.2f}</b>\n",
            "<pre>Ticker   W/L       P&L"
            "\n" + "─"*28
        ]
        for ticker in sorted(by_ticker):
            ts  = by_ticker[ticker]
            w   = sum(1 for t in ts if t.get("outcome") == "WIN")
            pnl = sum(t.get("pnl_usd", 0.0) for t in ts)
            lines.append(f"{ticker:<8} {w}W/{len(ts)-w}L   ${pnl:>+8.2f}")
        lines.append("─"*28)
        lines.append(f"{'TOTAL':<8}           ${total_pnl:>+8.2f}</pre>")
        bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")
    except Exception as e:
        bot.reply_to(msg, f"Error: {e}")


# ── /alerts ──────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["alerts"])
def cmd_alerts(msg):
    if not _only_owner(msg): return
    today = date.today().strftime("%Y%m%d")
    path  = MUNI / f"output/alerts_{today}.json"
    if not path.exists():
        path = MUNI / "output/latest_alerts.json"
    if not path.exists():
        bot.reply_to(msg, "No muni alerts on file.")
        return
    alerts = json.loads(path.read_text())
    if not alerts:
        bot.reply_to(msg, "✅ No anomalies detected today.")
        return
    lines = [f"🔔 <b>{len(alerts)} Muni Alert(s)</b>\n"]
    for a in alerts:
        lines.append(
            f"<b>{a['issuer']}</b> ({a['state']})\n"
            f"YTW {a['ytw']}%  +{a['spread_bps']}bps  {a['rating']}\n"
            f"EMMA: emma.msrb.org/Security/Details/{a['cusip']}\n"
        )
    bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")


# ── /leads ───────────────────────────────────────────────────────────────────

@bot.message_handler(commands=["leads"])
def cmd_leads(msg):
    if not _only_owner(msg): return
    scores = GALLERY / "leads/scores.json"
    if not scores.exists():
        bot.reply_to(msg, "No scores.json — run score-leads first.")
        return
    d     = json.loads(scores.read_text())
    leads = d.get("leads", [])[:8]
    icons = {"HOT": "🔥", "WARM": "🌤", "COLD": "❄️"}
    lines = [f"👥 <b>GallerySense Leads</b>  "
             f"HOT:{d.get('hot',0)} WARM:{d.get('warm',0)} COLD:{d.get('cold',0)}\n"]
    for l in leads:
        lines.append(
            f"{icons.get(l['priority'],'?')} [{l['score']}/10] "
            f"<b>{l['name']}</b>\n"
            f"  {l['reason'][:70]}"
        )
    bot.reply_to(msg, "\n".join(lines), parse_mode="HTML")


# ── /pause / /resume ─────────────────────────────────────────────────────────

@bot.message_handler(commands=["pause"])
def cmd_pause(msg):
    if not _only_owner(msg): return
    PAUSE_FLAG.parent.mkdir(exist_ok=True)
    PAUSE_FLAG.touch()
    bot.reply_to(msg, "⏸ <b>Ouroboros paused.</b> Send /resume to restart trading.", parse_mode="HTML")


@bot.message_handler(commands=["resume"])
def cmd_resume(msg):
    if not _only_owner(msg): return
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
        bot.reply_to(msg, "▶️ <b>Ouroboros resumed.</b> Trading active.", parse_mode="HTML")
    else:
        bot.reply_to(msg, "Already running — not paused.")


# ── main ─────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    print(f"Telegram bot started (chat_id={CHAT_ID})")
    bot.infinity_polling(timeout=20, long_polling_timeout=20)
