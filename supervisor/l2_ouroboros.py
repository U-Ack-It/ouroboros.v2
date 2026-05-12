"""
L2 Supervisor — Ouroboros v2

Runs every 15 minutes. Reads logs, ledger, and regime snapshot.
Detects semantic anomalies (not just crashes) and escalates to L3 via
the shared escalation queue + Telegram alert.

Detectors:
  1. PNL_COLLAPSE       — trades closing near entry price (bracket bug)
  2. RETRY_LOOP         — same ticker failing 2+ times in 30 min
  3. GATE4_DEGRADED     — >60% of today's Gate 4 verdicts are low-confidence
  4. STALE_REGIME       — regime_snapshot.json older than 90 min
  5. TRADE_DROUGHT      — active session ran but zero orders placed all day
  6. LOSS_LIMIT_APPROACH— today's P&L > 70% of daily loss floor

Runs standalone:
    venv/bin/python3.14 supervisor/l2_ouroboros.py
Or imported by run_supervisor.py for the joint L2+L3 loop.
"""

import json
import os
import re
import sys
import time
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from typing import Optional

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from supervisor.escalation_queue import already_open, push
from src.notifications.telegram import TelegramNotifier

POLL_INTERVAL_SEC = 900   # 15 minutes

LEDGER_PATH    = "data/trades_ledger.json"
ORDERS_PATH    = "logs/orders.json"
DECISIONS_PATH = "logs/trading_decisions.log"
REGIME_PATH    = "logs/regime_snapshot.json"
EXEC_CFG_PATH  = "config/execution_config.json"

_notifier = TelegramNotifier()


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------

def _load_json(path: str) -> any:
    try:
        with open(path) as f:
            return json.load(f)
    except Exception:
        return None


def _load_trades() -> list:
    raw = _load_json(LEDGER_PATH)
    if isinstance(raw, dict):
        return raw.get("trades", [])
    return raw or []


def _load_orders() -> list:
    raw = _load_json(ORDERS_PATH)
    return raw if isinstance(raw, list) else []


def _load_decisions() -> str:
    try:
        with open(DECISIONS_PATH) as f:
            return f.read()
    except Exception:
        return ""


def _load_regime() -> dict:
    return _load_json(REGIME_PATH) or {}


def _load_exec_cfg() -> dict:
    return _load_json(EXEC_CFG_PATH) or {}


# ---------------------------------------------------------------------------
# Detectors
# ---------------------------------------------------------------------------

def detect_pnl_collapse(trades: list) -> Optional[dict]:
    """
    If the last 4+ closed live trades all have |pnl_pct| < 0.3%, it
    almost certainly means _broker_close() is exiting at entry price
    (bracket legs not being inspected).
    """
    live_closed = [
        t for t in trades
        if t.get("source") == "live"
        and t.get("outcome") in ("WIN", "LOSS")
        and not t.get("dry_run", False)
    ]
    recent = live_closed[-6:]
    if len(recent) < 4:
        return None

    tiny = [t for t in recent if abs(t.get("pnl_pct", 99.0)) < 0.3]
    if len(tiny) < 4:
        return None

    tickers = [t["ticker"] for t in tiny]
    avg_pct  = sum(abs(t.get("pnl_pct", 0)) for t in tiny) / len(tiny)
    return {
        "type":     "PNL_COLLAPSE",
        "severity": "HIGH",
        "detail": (
            f"{len(tiny)}/{len(recent)} recent live trades closed with |P&L| < 0.3% "
            f"(avg {avg_pct:.3f}%). Tickers: {tickers}. "
            f"Expected 1-2% per trade based on TP/SL config."
        ),
        "hypothesis": (
            "position_manager._broker_close() is reading the parent bracket order's "
            "filled_avg_price (entry fill) as the exit price. The TP/SL legs in "
            "data['legs'] are not being checked."
        ),
        "files_to_check": [
            "src/core/broker/alpaca_client.py",
            "portfolio/position_manager.py",
        ],
    }


def detect_retry_loop(orders: list) -> Optional[dict]:
    """
    If the same ticker has 2+ failed API submissions within 30 minutes.
    Indicates _increment_count() not firing on failures.
    """
    now     = datetime.now(timezone.utc)
    cutoff  = now - timedelta(minutes=30)

    recent_failures = []
    for o in orders:
        if o.get("success", True):
            continue
        submitted = o.get("submitted_at", "")
        if not submitted:
            continue
        try:
            ts = datetime.fromisoformat(submitted)
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=timezone.utc)
            if ts > cutoff:
                recent_failures.append(o)
        except Exception:
            pass

    by_ticker = defaultdict(list)
    for o in recent_failures:
        ticker = o.get("order", {}).get("ticker", "UNKNOWN")
        by_ticker[ticker].append(o)

    culprits = {t: v for t, v in by_ticker.items() if len(v) >= 2}
    if not culprits:
        return None

    lines = []
    for ticker, attempts in culprits.items():
        msgs = [a.get("message", "?")[-60:] for a in attempts]
        lines.append(f"  {ticker}: {len(attempts)} failures — last: {msgs[-1]}")

    return {
        "type":     "RETRY_LOOP",
        "severity": "MEDIUM",
        "detail": (
            f"Repeated failed order submissions in last 30 min:\n"
            + "\n".join(lines)
        ),
        "hypothesis": (
            "_increment_count() only fires on result.success=True. "
            "Broker-rejected orders (422, HTB) leave the daily counter at 0, "
            "allowing unlimited retries until market close."
        ),
        "files_to_check": ["src/core/execution/executor.py"],
    }


def detect_gate4_degradation(decisions_log: str) -> Optional[dict]:
    """
    If >60% of today's Gate 4 PASS/BLOCK lines show conf < 0.55,
    Gate 4 is likely running in API-error fallback mode.
    """
    today = date.today().isoformat()
    lines_today = [
        l for l in decisions_log.splitlines()
        if l.startswith(today) and "conf=" in l
    ]
    if len(lines_today) < 5:
        return None

    low_conf = [l for l in lines_today if re.search(r"conf=0\.[0-4]\d", l)]
    ratio    = len(low_conf) / len(lines_today)

    if ratio <= 0.60:
        return None

    # Extract sample of low-conf lines for context
    sample = low_conf[-3:]
    return {
        "type":     "GATE4_DEGRADED",
        "severity": "MEDIUM",
        "detail": (
            f"{len(low_conf)}/{len(lines_today)} Gate 4 verdicts today have conf < 0.55 "
            f"({ratio:.0%}). Positions are being sized at 40-60% of regime target. "
            f"Sample: {sample[-1][:120] if sample else 'n/a'}"
        ),
        "hypothesis": (
            "Claude API returning 529 (overloaded) or timeout errors. "
            "QuantAgent._call_llm() falls back to CAUTION conf=0.50 on exception. "
            "No Telegram alert fires for this degradation."
        ),
        "files_to_check": ["src/llm_agent/agent.py"],
    }


def _in_active_session() -> bool:
    """Mirror of heartbeat.py is_active_session — London 03-05, NY 08-11, Asia 20-23 ET."""
    hour = datetime.now().hour
    return (3 <= hour <= 5) or (8 <= hour <= 11) or (20 <= hour <= 23)


def detect_stale_regime(regime: dict) -> Optional[dict]:
    """
    Regime snapshot older than 90 minutes means the heartbeat may have died
    or regime.py is silently failing.
    """
    fetched_at = regime.get("fetched_at")
    if not fetched_at:
        return {
            "type":     "STALE_REGIME",
            "severity": "HIGH",
            "detail":   "logs/regime_snapshot.json is missing or has no fetched_at field.",
            "hypothesis": (
                "Heartbeat never ran, or regime.py threw on first fetch and "
                "the snapshot was never written."
            ),
            "files_to_check": ["src/sentiment/regime.py", "heartbeat.py"],
        }

    try:
        fetched = datetime.fromisoformat(fetched_at)
        if fetched.tzinfo is None:
            fetched = fetched.replace(tzinfo=timezone.utc)
        age_min = (datetime.now(timezone.utc) - fetched).total_seconds() / 60
    except Exception:
        return None

    if age_min <= 90:
        return None

    # During zombie hours the heartbeat sleeps — stale snapshot is expected
    if not _in_active_session():
        return None

    return {
        "type":     "STALE_REGIME",
        "severity": "MEDIUM",
        "detail": (
            f"Regime snapshot is {age_min:.0f} minutes old "
            f"(label={regime.get('label', '?')}, VIX={regime.get('vix', '?')}). "
            f"Last fetched: {fetched_at}"
        ),
        "hypothesis": (
            "Heartbeat crashed or is sleeping unexpectedly. "
            "muni_scanner is reading this stale value for its alert threshold."
        ),
        "files_to_check": ["src/sentiment/regime.py", "heartbeat.py"],
    }


def detect_trade_drought(trades: list, orders: list, exec_cfg: dict) -> Optional[dict]:
    """
    If today had active session ticks (orders were at least evaluated)
    but zero successful orders placed — suggests gate over-blocking or
    scanner failure.
    """
    today = date.today().isoformat()

    # Any successful order today means trading is working
    today_success = [
        o for o in orders
        if o.get("success") and (o.get("submitted_at") or "").startswith(today)
    ]
    if today_success:
        return None

    # Was there any attempt today at all? (even failed)
    today_attempts = [
        o for o in orders
        if (o.get("submitted_at") or "").startswith(today)
    ]
    if not today_attempts:
        return None

    max_daily = exec_cfg.get("max_daily_trades", 6)
    return {
        "type":     "TRADE_DROUGHT",
        "severity": "LOW",
        "detail": (
            f"Active session ran today ({len(today_attempts)} order attempts) "
            f"but 0 successful placements out of max_daily={max_daily}. "
            f"All orders may be getting pre-flight blocked or API rejected."
        ),
        "hypothesis": (
            "Daily limit already hit from previous failures that were incorrectly "
            "counted, or all FVGs are too small (<min_fvg_pct), or exposure cap reached."
        ),
        "files_to_check": [
            "src/core/execution/executor.py",
            "logs/trading_decisions.log",
        ],
    }


def detect_loss_limit_approach(trades: list, exec_cfg: dict) -> Optional[dict]:
    """
    Alert when today's realized P&L exceeds 70% of the daily loss limit.
    Gives the operator a chance to intervene before the halt kicks in.
    """
    today      = date.today().isoformat()
    loss_limit = float(exec_cfg.get("daily_loss_limit_usd", -150.0))
    threshold  = loss_limit * 0.70   # e.g. -105 when limit is -150

    today_pnl = sum(
        t.get("pnl_usd", 0.0)
        for t in trades
        if t.get("entry_time", "")[:10] == today
        and t.get("outcome") in ("WIN", "LOSS")
        and t.get("source") == "live"
    )

    if today_pnl > threshold:   # not negative enough yet
        return None

    return {
        "type":     "LOSS_LIMIT_APPROACH",
        "severity": "HIGH",
        "detail": (
            f"Today's realized P&L is ${today_pnl:.2f} — "
            f"past 70% of the ${loss_limit:.0f} daily loss limit. "
            f"Trading will halt automatically at ${loss_limit:.0f}."
        ),
        "hypothesis": (
            "Multiple SL hits in today's session. "
            "Review trading_decisions.log for gate approval quality and "
            "consider whether regime sizing is appropriate."
        ),
        "files_to_check": [
            "logs/trading_decisions.log",
            "memory/trade_outcomes.md",
        ],
    }


# ---------------------------------------------------------------------------
# Telegram formatter
# ---------------------------------------------------------------------------

SEVERITY_EMOJI = {"HIGH": "🔴", "MEDIUM": "🟡", "LOW": "🔵"}


def _fmt_escalation(esc: dict) -> str:
    emoji = SEVERITY_EMOJI.get(esc["severity"], "⚪")
    files = "\n".join(f"  • <code>{f}</code>" for f in esc.get("files_to_check", []))
    return (
        f"{emoji} <b>L2 Supervisor — {esc['type']}</b>\n"
        f"<b>Severity:</b> {esc['severity']}  |  <b>ID:</b> <code>{esc['id']}</code>\n\n"
        f"<b>Detail:</b>\n{esc['detail']}\n\n"
        f"<b>Hypothesis:</b>\n{esc['hypothesis']}\n\n"
        f"<b>Files to check:</b>\n{files}\n\n"
        f"<i>L3 Fixer will attempt auto-diagnosis for HIGH severity.</i>"
    )


# ---------------------------------------------------------------------------
# Main scan
# ---------------------------------------------------------------------------

def run_scan() -> list[dict]:
    """Run all detectors and return list of new escalations pushed."""
    trades    = _load_trades()
    orders    = _load_orders()
    decisions = _load_decisions()
    regime    = _load_regime()
    exec_cfg  = _load_exec_cfg()

    detectors = [
        ("PNL_COLLAPSE",        lambda: detect_pnl_collapse(trades)),
        ("RETRY_LOOP",          lambda: detect_retry_loop(orders)),
        ("GATE4_DEGRADED",      lambda: detect_gate4_degradation(decisions)),
        ("STALE_REGIME",        lambda: detect_stale_regime(regime)),
        ("TRADE_DROUGHT",       lambda: detect_trade_drought(trades, orders, exec_cfg)),
        ("LOSS_LIMIT_APPROACH", lambda: detect_loss_limit_approach(trades, exec_cfg)),
    ]

    new_escalations = []
    for atype, fn in detectors:
        try:
            result = fn()
        except Exception as exc:
            print(f"[L2] Detector {atype} error: {exc}")
            continue

        if result is None:
            print(f"[L2] {atype}: OK")
            continue

        if already_open(atype, within_hours=6):
            print(f"[L2] {atype}: DETECTED but escalation already open — skipping duplicate")
            continue

        esc = push(
            anomaly_type   = result["type"],
            severity       = result["severity"],
            detail         = result["detail"],
            hypothesis     = result["hypothesis"],
            files_to_check = result["files_to_check"],
        )
        print(f"[L2] {atype}: ESCALATED → {esc['id']} ({esc['severity']})")
        _notifier.send(_fmt_escalation(esc))
        new_escalations.append(esc)

    return new_escalations


# ---------------------------------------------------------------------------
# Standalone entry point
# ---------------------------------------------------------------------------

def run_loop():
    print("[L2 Supervisor] Starting — poll interval 15 min")
    while True:
        print(f"\n[L2] Scan at {datetime.now().strftime('%H:%M:%S')}")
        try:
            new = run_scan()
            if not new:
                print("[L2] All clear.")
        except Exception as exc:
            print(f"[L2] Scan failed: {exc}")
        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    run_loop()
