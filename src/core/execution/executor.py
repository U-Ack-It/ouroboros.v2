"""
Trade Executor — orchestrates the path from approved signal to filled order.

Flow:
  1. Pre-flight checks  (price sanity, daily trade limit, long-only guard)
  2. Position sizing    (USD budget → share quantity)
  3. Build bracket      (TRIGGER → OCO via SchwabClient)
  4. Submit or dry-run  (respects execution_config.json dry_run flag)
  5. Persist result     (orders.json log + portfolio ledger)

Call from heartbeat after validate_risk() returns True:
    result = executor.execute(ticker, asset_class, fvg, price, session)
"""

import json
import os
from datetime import datetime, date, timezone
from typing import Optional

from src.core.broker.schwab_client import SchwabClient
from src.core.broker.order import BracketOrder, OrderResult
from src.notifications.telegram import TelegramNotifier
from portfolio.ledger import load_trades


CONFIG_PATH    = "config/execution_config.json"
RISK_PATH      = "config/risk_policy.json"


class TradeExecutor:
    def __init__(
        self,
        config_path: str = CONFIG_PATH,
        risk_path: str   = RISK_PATH,
    ):
        self._cfg  = self._load(config_path)
        self._risk = self._load(risk_path)

        self._client   = SchwabClient()
        self._notifier = TelegramNotifier()
        self._daily_counts: dict[str, dict[str, int]] = {}  # date → {ticker: count, _total: count}

        # Attempt broker connection at startup (non-fatal)
        ok, msg = self._client.connect()
        if ok:
            ok2, msg2 = self._client.get_account_hash()
            print(f"[Executor] Schwab: {msg} | {msg2}")
        else:
            print(f"[Executor] Schwab offline ({msg}) — dry_run forced ON")
            self._cfg["dry_run"] = True

    # ------------------------------------------------------------------
    # Main entry point
    # ------------------------------------------------------------------

    def execute(
        self,
        ticker:      str,
        asset_class: str,
        fvg_type:    str,        # BULL_FVG | BEAR_FVG
        price:       float,
        session:     str,
    ) -> OrderResult:
        """
        Full execution pipeline for one signal.
        Returns OrderResult regardless of outcome — never raises.
        """
        dry_run   = self._cfg.get("dry_run", True)
        long_only = self._cfg.get("long_only", True)
        sl_pct    = self._cfg.get("stop_loss_pct",   0.01)
        tp_pct    = self._cfg.get("take_profit_pct", 0.02)

        direction = "LONG" if fvg_type == "BULL_FVG" else "SHORT"

        # ---- Pre-flight ------------------------------------------------
        fail = self._preflight(ticker, direction, price, long_only)
        if fail:
            return self._rejected(ticker, asset_class, fvg_type, direction,
                                  price, sl_pct, tp_pct, session, dry_run, fail)

        # ---- Live price (fresher than scanner price) -------------------
        ok, live_price = self._client.get_last_price(ticker)
        if ok and live_price > 0:
            price = live_price

        # ---- Position sizing ------------------------------------------
        position_usd = float(
            self._risk.get("risk_parameters", {}).get("max_position_size_usd", 450.0)
        )
        quantity = max(1, int(position_usd / price))

        # ---- Build order record ----------------------------------------
        bracket = BracketOrder(
            ticker=ticker,
            direction=direction,
            quantity=quantity,
            entry_price=round(price, 4),
            stop_loss=round(price * (1 - sl_pct) if direction == "LONG" else price * (1 + sl_pct), 4),
            take_profit=round(price * (1 + tp_pct) if direction == "LONG" else price * (1 - tp_pct), 4),
            sl_pct=sl_pct,
            tp_pct=tp_pct,
            position_usd=round(quantity * price, 2),
            session=session,
            fvg_type=fvg_type,
            dry_run=dry_run,
        )

        # ---- Submit or dry-run ----------------------------------------
        if dry_run:
            result = OrderResult(
                order=bracket,
                success=True,
                order_id="DRY-RUN",
                http_status=None,
                message=(
                    f"[DRY RUN] Would place {direction} {quantity}x {ticker} "
                    f"@ ~${price:.2f} | TP ${bracket.take_profit:.2f} | SL ${bracket.stop_loss:.2f}"
                ),
                raw_response=None,
            )
        else:
            payload = SchwabClient.build_bracket_payload(
                ticker=ticker,
                direction=direction,
                quantity=quantity,
                entry_price=price,
                sl_pct=sl_pct,
                tp_pct=tp_pct,
                tp_duration=self._cfg.get("tp_duration", "GOOD_TILL_CANCEL"),
                sl_duration=self._cfg.get("sl_duration", "GOOD_TILL_CANCEL"),
            )
            ok, order_id, http_status, raw = self._client.place_order(payload)
            result = OrderResult(
                order=bracket,
                success=ok,
                order_id=order_id,
                http_status=http_status,
                message=(
                    f"PLACED {direction} {quantity}x {ticker} | TP ${bracket.take_profit:.2f} | SL ${bracket.stop_loss:.2f} | order={order_id}"
                    if ok else
                    f"FAILED {ticker}: HTTP {http_status} — {raw}"
                ),
                raw_response=raw,
            )

        # ---- Persist ---------------------------------------------------
        self._log_order(result)
        if result.success:
            self._increment_count(ticker)
            self._append_to_ledger(bracket, result)

        # ---- Notify ----------------------------------------------------
        self._notifier.send_execution(
            ticker=bracket.ticker,
            direction=bracket.direction,
            quantity=bracket.quantity,
            entry_price=bracket.entry_price,
            tp=bracket.take_profit,
            sl=bracket.stop_loss,
            position_usd=bracket.position_usd,
            order_id=result.order_id,
            success=result.success,
            dry_run=bracket.dry_run,
            message=result.message,
        )

        return result

    # ------------------------------------------------------------------
    # Pre-flight guard
    # ------------------------------------------------------------------

    def _preflight(
        self,
        ticker: str,
        direction: str,
        price: float,
        long_only: bool,
    ) -> Optional[str]:
        # Direction guard
        if long_only and direction == "SHORT":
            return f"SHORT blocked (long_only=true) — BEAR_FVG signals are logged but not executed"

        # Price sanity
        lo = self._cfg.get("min_price_sanity", 0.50)
        hi = self._cfg.get("max_price_sanity", 100_000.0)
        if not (lo <= price <= hi):
            return f"Price ${price:.2f} outside sanity bounds [{lo}, {hi}]"

        # Daily trade limit (total)
        today = date.today().isoformat()
        counts = self._daily_counts.get(today, {})
        max_daily = self._cfg.get("max_daily_trades", 6)
        if counts.get("_total", 0) >= max_daily:
            return f"Daily trade limit reached ({max_daily})"

        # Daily loss limit
        loss_limit = float(self._cfg.get("daily_loss_limit_usd", -150.0))
        today_pnl  = self._get_today_pnl(today)
        if today_pnl <= loss_limit:
            return f"Daily loss limit hit (${today_pnl:.2f} ≤ ${loss_limit:.2f}) — trading halted"

        # Per-ticker limit
        max_per_ticker = self._cfg.get("max_trades_per_ticker_per_day", 1)
        if counts.get(ticker, 0) >= max_per_ticker:
            return f"{ticker} already traded today ({max_per_ticker}/day limit)"

        return None

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _get_today_pnl(self, today: str) -> float:
        try:
            trades = load_trades()
            return sum(
                t.get("pnl_usd", 0.0)
                for t in trades
                if t.get("entry_time", "")[:10] == today
                and t.get("outcome") in ("WIN", "LOSS")
            )
        except Exception:
            return 0.0

    def _increment_count(self, ticker: str):
        today = date.today().isoformat()
        if today not in self._daily_counts:
            self._daily_counts[today] = {}
        self._daily_counts[today][ticker] = self._daily_counts[today].get(ticker, 0) + 1
        self._daily_counts[today]["_total"] = self._daily_counts[today].get("_total", 0) + 1

    def _log_order(self, result: OrderResult):
        log_path = self._cfg.get("orders_log", "logs/orders.json")
        os.makedirs(os.path.dirname(log_path), exist_ok=True)
        try:
            existing = []
            if os.path.exists(log_path):
                with open(log_path) as f:
                    existing = json.load(f)
            existing.append(result.to_dict())
            with open(log_path, "w") as f:
                json.dump(existing, f, indent=2)
        except Exception as e:
            print(f"[Executor] log_order failed: {e}")

    def _append_to_ledger(self, bracket: BracketOrder, result: OrderResult):
        try:
            from portfolio.ledger import append_trade
            trade = {
                "ticker":           bracket.ticker,
                "asset_class":      "Unknown",
                "session":          bracket.session,
                "direction":        bracket.direction,
                "fvg_type":         bracket.fvg_type,
                "fvg_size":         0.0,
                "entry_time":       result.submitted_at,
                "entry_price":      bracket.entry_price,
                "stop_loss":        bracket.stop_loss,
                "take_profit":      bracket.take_profit,
                "exit_price":       bracket.entry_price,
                "exit_time":        result.submitted_at,
                "outcome":          "OPEN",
                "pnl_usd":          0.0,
                "pnl_pct":          0.0,
                "position_size_usd": bracket.position_usd,
                "shares":           bracket.quantity,
                "order_id":         result.order_id,
                "dry_run":          bracket.dry_run,
            }
            append_trade(trade, source="live" if not bracket.dry_run else "dry_run")
        except Exception as e:
            print(f"[Executor] ledger append failed: {e}")

    def _rejected(
        self,
        ticker, asset_class, fvg_type, direction,
        price, sl_pct, tp_pct, session, dry_run, reason
    ) -> OrderResult:
        bracket = BracketOrder(
            ticker=ticker, direction=direction, quantity=0,
            entry_price=price,
            stop_loss=round(price * (1 - sl_pct), 4),
            take_profit=round(price * (1 + tp_pct), 4),
            sl_pct=sl_pct, tp_pct=tp_pct, position_usd=0.0,
            session=session, fvg_type=fvg_type, dry_run=dry_run,
        )
        return OrderResult(
            order=bracket, success=False, order_id=None,
            http_status=None, message=f"PRE-FLIGHT BLOCKED: {reason}",
            raw_response=None,
        )

    @staticmethod
    def _load(path: str) -> dict:
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return {}
