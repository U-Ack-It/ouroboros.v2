"""
Ouroboros v2 — Position Manager

Responsibilities:
  1. Sync open positions — check whether bracket TP/SL has been hit and
     update the ledger accordingly.  Live mode polls Schwab order status;
     dry-run mode simulates using yfinance OHLC bars since entry.

  2. Daily loss limit — if today's realised P&L falls below the configured
     threshold, new trades are blocked for the rest of the session.

  3. EOD flatten — at the configured cutoff (default 3:55 PM ET) any
     remaining OPEN positions are closed at market (live) or current price
     (dry-run) and marked in the ledger.

Usage (standalone CLI):
    venv/bin/python3.14 portfolio/position_manager.py --status
    venv/bin/python3.14 portfolio/position_manager.py --sync
    venv/bin/python3.14 portfolio/position_manager.py --flatten
    venv/bin/python3.14 portfolio/position_manager.py --daily-pnl
"""

import argparse
import json
import os
import sys
from datetime import datetime, date, timedelta
from typing import Optional

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from portfolio.ledger import load_trades, load_open_positions, update_trade
from portfolio.trade_memory import append_outcome
from src.core.broker.schwab_client import SchwabClient
from src.notifications.telegram import TelegramNotifier

import yfinance as yf

CONFIG_PATH = "config/execution_config.json"
RISK_PATH   = "config/risk_policy.json"


class PositionManager:
    def __init__(
        self,
        config_path: str = CONFIG_PATH,
        risk_path:   str = RISK_PATH,
    ):
        self._cfg      = self._load_json(config_path)
        self._risk     = self._load_json(risk_path)
        self._client   = SchwabClient()
        self._notifier = TelegramNotifier()
        self._eod_flattened_on: Optional[str] = None  # date string — prevent double-flatten

        ok, msg = self._client.connect()
        if not ok:
            self._cfg["dry_run"] = True

    # ------------------------------------------------------------------
    # 1. Sync open positions
    # ------------------------------------------------------------------

    def sync_positions(self) -> int:
        """
        Check every OPEN position against broker or yfinance.
        Closes positions where TP or SL has been crossed.
        Returns the number of positions closed this call.
        """
        open_pos = load_open_positions()
        if not open_pos:
            return 0

        closed = 0
        dry_run = self._cfg.get("dry_run", True)

        for pos in open_pos:
            if dry_run or pos.get("dry_run"):
                result = self._simulate_close(pos)
            else:
                result = self._broker_close(pos)

            if result:
                update_trade(pos["id"], result)
                append_outcome(pos, result)
                closed += 1
                self._notifier.send_execution(
                    ticker      = pos.get("ticker", "?"),
                    direction   = pos.get("direction", "LONG"),
                    quantity    = pos.get("shares", 0),
                    entry_price = pos.get("entry_price", 0),
                    tp          = pos.get("take_profit", 0),
                    sl          = pos.get("stop_loss", 0),
                    position_usd= pos.get("position_size_usd", 0),
                    order_id    = pos.get("order_id", "—"),
                    success     = True,
                    dry_run     = dry_run,
                    message     = (
                        f"[CLOSED] {result['outcome']} "
                        f"exit=${result.get('exit_price',0):.2f} "
                        f"pnl=${result.get('pnl_usd',0):+.2f} "
                        f"({result.get('pnl_pct',0):+.2f}%)"
                    ),
                )

        return closed

    def _simulate_close(self, pos: dict) -> Optional[dict]:
        """
        For dry-run positions: fetch OHLC since entry, check if TP or SL
        was crossed. Returns update dict if closed, else None.
        """
        ticker     = pos.get("ticker")
        entry_time = pos.get("entry_time", "")[:10]
        entry_px   = float(pos.get("entry_price", 0))
        tp         = float(pos.get("take_profit", entry_px * 1.02))
        sl         = float(pos.get("stop_loss",   entry_px * 0.99))
        direction  = pos.get("direction", "LONG")
        shares     = int(pos.get("shares", 1))
        pos_usd    = float(pos.get("position_size_usd", shares * entry_px))

        try:
            df = yf.Ticker(ticker).history(start=entry_time, interval="1h")
            if df.empty or len(df) < 2:
                return None
            idx = df.index.tz_convert(None) if df.index.tz else df.index
            df.index = idx
            bars = df.iloc[1:]  # skip the entry bar itself
        except Exception:
            return None

        exit_px   = None
        outcome   = None
        exit_time = None

        if direction == "LONG":
            for i, row in bars.iterrows():
                if row["High"] >= tp:
                    exit_px, outcome = tp, "WIN"
                    exit_time = i
                    break
                if row["Low"] <= sl:
                    exit_px, outcome = sl, "LOSS"
                    exit_time = i
                    break
        else:  # SHORT
            for i, row in bars.iterrows():
                if row["Low"] <= tp:
                    exit_px, outcome = tp, "WIN"
                    exit_time = i
                    break
                if row["High"] >= sl:
                    exit_px, outcome = sl, "LOSS"
                    exit_time = i
                    break

        if exit_px is None:
            return None  # still open

        pnl_usd = round((exit_px - entry_px) * shares if direction == "LONG"
                        else (entry_px - exit_px) * shares, 2)
        pnl_pct = round(pnl_usd / pos_usd * 100, 4) if pos_usd else 0.0

        return {
            "outcome":    outcome,
            "exit_price": round(exit_px, 4),
            "exit_time":  str(exit_time)[:16] if exit_time else datetime.now().isoformat()[:16],
            "pnl_usd":    pnl_usd,
            "pnl_pct":    pnl_pct,
        }

    def _broker_close(self, pos: dict) -> Optional[dict]:
        """
        For live positions: query Schwab order status by order_id.
        Returns update dict if the order is fully filled, else None.
        """
        order_id = pos.get("order_id")
        if not order_id or order_id == "DRY-RUN":
            return self._simulate_close(pos)

        ok, status, fills = self._client.get_order_status(order_id)
        if not ok or status not in ("FILLED", "CANCELLED"):
            return None

        entry_px = float(pos.get("entry_price", 0))
        exit_px  = float(fills.get("fill_price", entry_px))
        shares   = int(pos.get("shares", 1))
        pos_usd  = float(pos.get("position_size_usd", shares * entry_px))
        direction = pos.get("direction", "LONG")

        pnl_usd = round((exit_px - entry_px) * shares if direction == "LONG"
                        else (entry_px - exit_px) * shares, 2)
        pnl_pct = round(pnl_usd / pos_usd * 100, 4) if pos_usd else 0.0
        outcome = "WIN" if pnl_usd > 0 else "LOSS"

        return {
            "outcome":    outcome,
            "exit_price": exit_px,
            "exit_time":  datetime.now().isoformat()[:16],
            "pnl_usd":    pnl_usd,
            "pnl_pct":    pnl_pct,
        }

    # ------------------------------------------------------------------
    # 2. Daily loss limit
    # ------------------------------------------------------------------

    def check_daily_loss(self) -> tuple[bool, str]:
        """
        Returns (blocked, reason).  blocked=True means halt new trades.
        Limit is configured as a negative USD amount, e.g. -150.0.
        """
        limit = float(self._cfg.get("daily_loss_limit_usd", -150.0))
        today_pnl = self._get_today_pnl()
        if today_pnl <= limit:
            return True, f"Daily loss limit reached (${today_pnl:.2f} ≤ limit ${limit:.2f})"
        return False, ""

    def _get_today_pnl(self) -> float:
        today = date.today().isoformat()
        trades = load_trades()
        return sum(
            t.get("pnl_usd", 0.0)
            for t in trades
            if t.get("entry_time", "")[:10] == today
            and t.get("outcome") in ("WIN", "LOSS")
        )

    # ------------------------------------------------------------------
    # 3. EOD flatten
    # ------------------------------------------------------------------

    def eod_check(self) -> bool:
        """
        Flatten all open positions if the current time is at or past the
        configured EOD cutoff. Guards against double-flattening on the same day.
        Returns True if flatten was triggered.
        """
        now     = datetime.now()
        today   = date.today().isoformat()
        hour    = int(self._cfg.get("eod_flatten_hour",   15))
        minute  = int(self._cfg.get("eod_flatten_minute", 55))

        past_cutoff = (now.hour > hour) or (now.hour == hour and now.minute >= minute)
        already_done = (self._eod_flattened_on == today)

        if past_cutoff and not already_done:
            self._eod_flattened_on = today
            n = self.flatten_all(reason="EOD")
            if n:
                self._notifier.send_error(
                    f"EOD flatten: closed {n} open position(s) at market close."
                )
            return True
        return False

    def flatten_all(self, reason: str = "EOD") -> int:
        """
        Close every OPEN position immediately.
        Live: submits a market order via Schwab.
        Dry-run: marks closed at current yfinance price.
        Returns number of positions closed.
        """
        open_pos = load_open_positions()
        if not open_pos:
            return 0

        dry_run = self._cfg.get("dry_run", True)
        closed  = 0

        for pos in open_pos:
            ticker    = pos.get("ticker")
            entry_px  = float(pos.get("entry_price", 0))
            shares    = int(pos.get("shares", 1))
            direction = pos.get("direction", "LONG")
            pos_usd   = float(pos.get("position_size_usd", shares * entry_px))

            # Get current price
            exit_px = self._current_price(ticker, entry_px, dry_run)

            if not dry_run:
                # Submit market close order
                side = "SELL" if direction == "LONG" else "BUY_TO_COVER"
                payload = SchwabClient.build_market_payload(ticker, side, shares)
                ok, oid, _, _ = self._client.place_order(payload)
                if not ok:
                    print(f"  ⚠️  {ticker}: flatten order failed")
                    continue

            pnl_usd = round((exit_px - entry_px) * shares if direction == "LONG"
                            else (entry_px - exit_px) * shares, 2)
            pnl_pct = round(pnl_usd / pos_usd * 100, 4) if pos_usd else 0.0
            outcome = "WIN" if pnl_usd > 0 else "LOSS"

            close_result = {
                "outcome":      outcome,
                "exit_price":   round(exit_px, 4),
                "exit_time":    datetime.now().isoformat()[:16],
                "pnl_usd":      pnl_usd,
                "pnl_pct":      pnl_pct,
                "close_reason": reason,
            }
            update_trade(pos["id"], close_result)
            append_outcome(pos, close_result)
            print(f"  [{reason}] Closed {ticker} {direction} @ ${exit_px:.2f} → {outcome} ${pnl_usd:+.2f}")
            closed += 1

        return closed

    def _current_price(self, ticker: str, fallback: float, dry_run: bool) -> float:
        if not dry_run:
            ok, price = self._client.get_last_price(ticker)
            if ok and price > 0:
                return price
        try:
            info = yf.Ticker(ticker).fast_info
            p = getattr(info, "last_price", None) or getattr(info, "regularMarketPrice", None)
            if p and p > 0:
                return float(p)
        except Exception:
            pass
        return fallback

    # ------------------------------------------------------------------
    # 4. Status display
    # ------------------------------------------------------------------

    def print_status(self):
        open_pos  = load_open_positions()
        today_pnl = self._get_today_pnl()
        limit     = float(self._cfg.get("daily_loss_limit_usd", -150.0))
        blocked, block_msg = self.check_daily_loss()

        SEP  = "=" * 68
        DASH = "-" * 68
        print(f"\n{SEP}")
        print(f"  OUROBOROS v2 — POSITION MANAGER STATUS")
        print(f"  {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print(f"{SEP}")

        print(f"\nDAILY P&L")
        print(DASH)
        sign = "+" if today_pnl >= 0 else ""
        print(f"  Today realized : ${today_pnl:+.2f}  (limit: ${limit:.2f})")
        status_icon = "🚫 HALTED" if blocked else "✅ ACTIVE"
        print(f"  Trading status : {status_icon}")
        if blocked:
            print(f"  {block_msg}")

        print(f"\nOPEN POSITIONS  ({len(open_pos)})")
        print(DASH)
        if not open_pos:
            print("  — none —")
        else:
            print(f"  {'Ticker':<8} {'Dir':<6} {'Qty':>4}  {'Entry':>8}  {'TP':>8}  {'SL':>8}  {'Session':<14}  {'Time'}")
            print(f"  {'------':<8} {'---':<6} {'---':>4}  {'-----':>8}  {'--':>8}  {'--':>8}  {'-'*14:<14}  {'----'}")
            for p in open_pos:
                print(
                    f"  {p.get('ticker','?'):<8} "
                    f"{p.get('direction','?'):<6} "
                    f"{p.get('shares',0):>4}  "
                    f"${p.get('entry_price',0):>7.2f}  "
                    f"${p.get('take_profit',0):>7.2f}  "
                    f"${p.get('stop_loss',0):>7.2f}  "
                    f"{p.get('session','?'):<14}  "
                    f"{str(p.get('entry_time',''))[:16]}"
                )
        print(f"\n{SEP}\n")

    # ------------------------------------------------------------------
    @staticmethod
    def _load_json(path: str) -> dict:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 Position Manager")
    p.add_argument("--status",    action="store_true", help="Show open positions + daily P&L")
    p.add_argument("--sync",      action="store_true", help="Sync open positions (check TP/SL)")
    p.add_argument("--flatten",   action="store_true", help="Force-close all open positions")
    p.add_argument("--daily-pnl", action="store_true", help="Print today's P&L vs limit")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    pm   = PositionManager()

    if args.status or not any([args.sync, args.flatten, args.daily_pnl]):
        pm.print_status()

    if args.sync:
        n = pm.sync_positions()
        print(f"Sync complete — {n} position(s) closed.")

    if args.flatten:
        n = pm.flatten_all(reason="MANUAL")
        print(f"Flatten complete — {n} position(s) closed.")

    if args.daily_pnl:
        blocked, msg = pm.check_daily_loss()
        pnl = pm._get_today_pnl()
        limit = float(pm._cfg.get("daily_loss_limit_usd", -150.0))
        print(f"Today P&L: ${pnl:+.2f}  (limit: ${limit:.2f})  {'🚫 HALTED' if blocked else '✅ OK'}")
        if msg:
            print(f"  {msg}")
