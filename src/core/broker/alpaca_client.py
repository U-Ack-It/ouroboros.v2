"""
Alpaca Execution Client

Drop-in replacement for SchwabClient. Same public interface.
Supports paper trading (ALPACA_BASE_URL=https://paper-api.alpaca.markets)
and live trading (ALPACA_BASE_URL=https://api.alpaca.markets).

Auth: APCA-API-KEY-ID / APCA-API-SECRET-KEY headers.
No SDK dependency — pure requests.
"""

import json
import os
from typing import Optional
from dotenv import load_dotenv

import requests

load_dotenv()

PAPER_URL = "https://paper-api.alpaca.markets"
LIVE_URL  = "https://api.alpaca.markets"
DATA_URL  = "https://data.alpaca.markets"

# Alpaca → canonical status mapping
_STATUS_MAP = {
    "new":               "WORKING",
    "partially_filled":  "WORKING",
    "filled":            "FILLED",
    "done_for_day":      "CANCELLED",
    "canceled":          "CANCELLED",
    "expired":           "CANCELLED",
    "replaced":          "CANCELLED",
    "pending_cancel":    "WORKING",
    "pending_replace":   "WORKING",
    "rejected":          "REJECTED",
    "held":              "WORKING",
    "accepted":          "WORKING",
    "accepted_for_bidding": "WORKING",
    "stopped":           "WORKING",
    "suspended":         "WORKING",
    "calculated":        "WORKING",
}


class AlpacaClient:
    """
    Thin wrapper around the Alpaca REST API v2.
    All public methods return (success: bool, ...) tuples — never raises.
    """

    def __init__(self):
        self._api_key    = os.getenv("ALPACA_API_KEY", "").strip()
        self._api_secret = os.getenv("ALPACA_API_SECRET", "").strip()
        base_env         = os.getenv("ALPACA_BASE_URL", PAPER_URL).strip().rstrip("/")
        self._base       = base_env if base_env else PAPER_URL
        self._connected  = False
        self._account_id: Optional[str] = None
        self._session    = requests.Session()

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> tuple[bool, str]:
        if not self._api_key or not self._api_secret:
            return False, "ALPACA_API_KEY / ALPACA_API_SECRET not set in .env"

        self._session.headers.update({
            "APCA-API-KEY-ID":     self._api_key,
            "APCA-API-SECRET-KEY": self._api_secret,
            "Content-Type":        "application/json",
            "Accept":              "application/json",
        })

        try:
            resp = self._session.get(f"{self._base}/v2/account", timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                self._account_id = data.get("id", "unknown")
                self._connected  = True
                mode = "PAPER" if "paper" in self._base else "LIVE"
                return True, f"Alpaca {mode} connected (account {self._account_id[:8]}…)"
            return False, f"Alpaca auth failed: HTTP {resp.status_code} — {resp.text[:200]}"
        except Exception as exc:
            return False, f"Alpaca connect error: {exc}"

    def get_account_hash(self) -> tuple[bool, str]:
        """Returns account ID (Alpaca uses UUID, not a hash — compatible alias)."""
        if not self._connected or not self._account_id:
            return False, "Not connected"
        return True, f"Account ID: {self._account_id[:8]}…"

    @property
    def ready(self) -> bool:
        return self._connected and bool(self._account_id)

    # ------------------------------------------------------------------
    # Quote
    # ------------------------------------------------------------------

    def get_last_price(self, ticker: str) -> tuple[bool, float]:
        """Returns (success, last_price). Falls back to yfinance on failure."""
        if self._connected:
            try:
                resp = self._session.get(
                    f"{DATA_URL}/v2/stocks/{ticker}/trades/latest",
                    timeout=8,
                )
                if resp.status_code == 200:
                    price = float(resp.json()["trade"]["p"])
                    if price > 0:
                        return True, price
            except Exception:
                pass

        try:
            import yfinance as yf
            p = float(yf.Ticker(ticker).fast_info.last_price or 0.0)
            return (True, p) if p else (False, 0.0)
        except Exception:
            return False, 0.0

    # ------------------------------------------------------------------
    # Order builder
    # ------------------------------------------------------------------

    @staticmethod
    def build_bracket_payload(
        ticker:      str,
        direction:   str,
        quantity:    int,
        entry_price: float,
        sl_pct:      float,
        tp_pct:      float,
        tp_duration: str = "GOOD_TILL_CANCEL",
        sl_duration: str = "GOOD_TILL_CANCEL",
    ) -> dict:
        """
        Build Alpaca bracket order dict.
        direction: 'LONG' → buy entry / sell exits
                   'SHORT' → sell_short entry / buy exits (requires margin)
        """
        # HTB (hard-to-borrow) assets on Alpaca only allow day orders for shorts
        tif = "day" if direction != "LONG" else ("gtc" if "CANCEL" in tp_duration.upper() else "day")

        if direction == "LONG":
            side     = "buy"
            tp_price = round(entry_price * (1 + tp_pct), 2)
            sl_price = round(entry_price * (1 - sl_pct), 2)
        else:
            side     = "sell"
            tp_price = round(entry_price * (1 - tp_pct), 2)
            sl_price = round(entry_price * (1 + sl_pct), 2)

        return {
            "symbol":      ticker,
            "qty":         str(quantity),
            "side":        side,
            "type":        "market",
            "time_in_force": tif,
            "order_class": "bracket",
            "take_profit": {"limit_price": str(tp_price)},
            "stop_loss":   {"stop_price":  str(sl_price)},
            "_direction":  direction,
            "_entry_price": entry_price,
            "_sl_pct": sl_pct,
            "_tp_pct": tp_pct,
        }

    @staticmethod
    def build_market_payload(ticker: str, side: str, quantity: int) -> dict:
        """
        Simple DAY MARKET order for EOD flattening.
        side: 'SELL' (close long) → mapped to 'sell'
              'BUY_TO_COVER' (close short) → mapped to 'buy'
        """
        alpaca_side = "buy" if side.upper() == "BUY_TO_COVER" else "sell"
        return {
            "symbol":        ticker,
            "qty":           str(quantity),
            "side":          alpaca_side,
            "type":          "market",
            "time_in_force": "day",
        }

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    def place_order(
        self, payload: dict
    ) -> tuple[bool, Optional[str], Optional[int], Optional[dict]]:
        """
        Submit order to Alpaca.
        Returns (success, order_id, http_status, raw_json).
        Strips internal _prefixed keys before sending.
        """
        if not self.ready:
            return False, None, None, {"error": "client not ready"}

        body = {k: v for k, v in payload.items() if not k.startswith("_")}

        try:
            resp = self._session.post(
                f"{self._base}/v2/orders",
                json=body,
                timeout=15,
            )
            status = resp.status_code
            try:
                data = resp.json()
            except Exception:
                data = {"raw": resp.text[:500]}

            if status in (200, 201):
                order_id = data.get("id")
                return True, order_id, status, data

            return False, None, status, data

        except Exception as exc:
            return False, None, None, {"exception": str(exc)}

    # ------------------------------------------------------------------
    # Order management
    # ------------------------------------------------------------------

    def get_open_orders(self) -> tuple[bool, list]:
        if not self.ready:
            return False, []
        try:
            resp = self._session.get(
                f"{self._base}/v2/orders",
                params={"status": "open", "limit": 50},
                timeout=10,
            )
            if resp.status_code == 200:
                orders = resp.json()
                return True, orders if isinstance(orders, list) else []
            return False, []
        except Exception:
            return False, []

    def get_order_status(
        self, order_id: str
    ) -> tuple[bool, str, dict]:
        """
        Returns (success, status_string, fills_dict).
        status_string: WORKING | FILLED | CANCELLED | REJECTED | UNKNOWN
        """
        if not self.ready:
            return False, "UNKNOWN", {}
        try:
            resp = self._session.get(
                f"{self._base}/v2/orders/{order_id}",
                timeout=10,
            )
            if resp.status_code != 200:
                return False, "UNKNOWN", {}

            data   = resp.json()
            raw    = data.get("status", "unknown")
            status = _STATUS_MAP.get(raw, "UNKNOWN")
            fills  = {}

            if status == "FILLED":
                # For bracket orders: entry "filled" means the ENTRY executed,
                # not that the position is closed. Check legs to see if TP/SL fired.
                legs = data.get("legs", [])
                filled_legs = [leg for leg in legs if leg.get("status") == "filled"]
                if legs and not filled_legs:
                    # Entry executed but no exit leg has fired yet — position still open
                    status = "WORKING"
                elif filled_legs:
                    fp = filled_legs[0].get("filled_avg_price")
                    if fp:
                        fills["fill_price"] = float(fp)
                else:
                    # Non-bracket order or legs missing — use entry fill price
                    fp = data.get("filled_avg_price")
                    if fp:
                        fills["fill_price"] = float(fp)

            return True, status, fills

        except Exception:
            return False, "UNKNOWN", {}

    def cancel_order(self, order_id: str) -> tuple[bool, str]:
        if not self.ready:
            return False, "not connected"
        try:
            resp = self._session.delete(
                f"{self._base}/v2/orders/{order_id}",
                timeout=10,
            )
            if resp.status_code in (200, 204):
                return True, f"Order {order_id} cancelled"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, str(exc)
