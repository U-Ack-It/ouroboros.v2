"""
Schwab Execution Client

Wraps schwabdev.Client with:
- Safe token loading (never crashes if tokens.json is missing)
- Bracket order builder matching Schwab's TRIGGER → OCO format
- Dry-run mode that logs but never submits

The bracket order structure (Schwab API format):
  Parent: TRIGGER / MARKET (entry fill)
  Child : OCO
    Leg A: SINGLE / LIMIT  (take-profit)
    Leg B: SINGLE / STOP   (stop-loss)
"""

import json
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

try:
    import schwabdev
    _SCHWAB_AVAILABLE = True
except ImportError:
    _SCHWAB_AVAILABLE = False


class SchwabClient:
    """
    Thin wrapper around schwabdev.Client.
    All public methods return (success: bool, message: str, data: dict|None).
    Never raises — all exceptions are caught and returned as failure tuples.
    """

    TOKENS_PATH = "tokens.json"

    def __init__(self):
        self._client: Optional[object] = None
        self._account_hash: Optional[str] = None
        self._connected = False

    # ------------------------------------------------------------------
    # Connection
    # ------------------------------------------------------------------

    def connect(self) -> tuple[bool, str]:
        if not _SCHWAB_AVAILABLE:
            return False, "schwabdev package not installed"

        app_key    = os.getenv("SCHWAB_APP_KEY", "").strip()
        app_secret = os.getenv("SCHWAB_APP_SECRET", "").strip()
        callback   = os.getenv("SCHWAB_CALLBACK_URL", "https://127.0.0.1").strip()

        if not app_key or not app_secret:
            return False, "SCHWAB_APP_KEY / SCHWAB_APP_SECRET not set in .env"

        if not os.path.exists(self.TOKENS_PATH):
            return False, f"tokens.json not found — run python schwab_auth.py first"

        try:
            self._client = schwabdev.Client(app_key, app_secret, callback)
            self._connected = True
            return True, "Schwab client initialised"
        except Exception as exc:
            return False, f"Schwab init failed: {exc}"

    def get_account_hash(self) -> tuple[bool, str]:
        """Resolve the account hash from env var or linked_accounts() API call."""
        env_hash = os.getenv("SCHWAB_ACCOUNT_NUMBER", "").strip()
        if env_hash:
            self._account_hash = env_hash
            return True, f"Account hash from env ({env_hash[:8]}…)"

        if not self._client:
            return False, "Not connected"

        try:
            resp = self._client.linked_accounts()
            accounts = resp.json()
            if not accounts:
                return False, "No linked accounts returned"
            self._account_hash = accounts[0].get("hashValue") or accounts[0].get("accountNumber")
            return True, f"Account hash resolved ({self._account_hash[:8]}…)"
        except Exception as exc:
            return False, f"linked_accounts() failed: {exc}"

    @property
    def ready(self) -> bool:
        return self._connected and self._account_hash is not None

    # ------------------------------------------------------------------
    # Quote
    # ------------------------------------------------------------------

    def get_last_price(self, ticker: str) -> tuple[bool, float]:
        """Returns (success, price). Falls back to yfinance on API failure."""
        if self._client:
            try:
                resp = self._client.quote(ticker)
                data = resp.json()
                price = (
                    data.get(ticker, {}).get("quote", {}).get("lastPrice")
                    or data.get(ticker, {}).get("quote", {}).get("closePrice")
                )
                if price:
                    return True, float(price)
            except Exception:
                pass

        # yfinance fallback
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
        ticker: str,
        direction: str,
        quantity: int,
        entry_price: float,
        sl_pct: float,
        tp_pct: float,
        tp_duration: str = "GOOD_TILL_CANCEL",
        sl_duration: str = "GOOD_TILL_CANCEL",
    ) -> dict:
        """
        Build the TRIGGER → OCO bracket order dict for Schwab API.

        direction: 'LONG' → BUY entry, SELL exits
                   'SHORT' → SELL SHORT entry, BUY TO COVER exits (requires margin)
        """
        if direction == "LONG":
            entry_instruction = "BUY"
            exit_instruction  = "SELL"
            tp_price = round(entry_price * (1 + tp_pct), 2)
            sl_price = round(entry_price * (1 - sl_pct), 2)
        else:  # SHORT
            entry_instruction = "SELL_SHORT"
            exit_instruction  = "BUY_TO_COVER"
            tp_price = round(entry_price * (1 - tp_pct), 2)
            sl_price = round(entry_price * (1 + sl_pct), 2)

        instrument = {"symbol": ticker, "assetType": "EQUITY"}

        return {
            "orderStrategyType": "TRIGGER",
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderLegCollection": [
                {
                    "instruction": entry_instruction,
                    "quantity": quantity,
                    "instrument": instrument,
                }
            ],
            "childOrderStrategies": [
                {
                    "orderStrategyType": "OCO",
                    "childOrderStrategies": [
                        {
                            "orderStrategyType": "SINGLE",
                            "orderType": "LIMIT",
                            "price": tp_price,
                            "session": "NORMAL",
                            "duration": tp_duration,
                            "orderLegCollection": [
                                {
                                    "instruction": exit_instruction,
                                    "quantity": quantity,
                                    "instrument": instrument,
                                }
                            ],
                        },
                        {
                            "orderStrategyType": "SINGLE",
                            "orderType": "STOP",
                            "stopPrice": sl_price,
                            "session": "NORMAL",
                            "duration": sl_duration,
                            "orderLegCollection": [
                                {
                                    "instruction": exit_instruction,
                                    "quantity": quantity,
                                    "instrument": instrument,
                                }
                            ],
                        },
                    ],
                }
            ],
        }

    # ------------------------------------------------------------------
    # Placement
    # ------------------------------------------------------------------

    def place_order(self, payload: dict) -> tuple[bool, Optional[str], Optional[int], Optional[dict]]:
        """
        Submit the order payload to Schwab.
        Returns (success, order_id, http_status, raw_json).
        order_id comes from the Location response header.
        """
        if not self.ready:
            return False, None, None, {"error": "client not ready"}

        try:
            resp = self._client.place_order(self._account_hash, payload)
            status = resp.status_code

            # 201 = created, order_id in Location header
            if status in (200, 201):
                location = resp.headers.get("Location", "")
                order_id = location.rstrip("/").split("/")[-1] if location else None
                try:
                    body = resp.json()
                except Exception:
                    body = {}
                return True, order_id, status, body

            try:
                body = resp.json()
            except Exception:
                body = {"raw": resp.text[:500]}
            return False, None, status, body

        except Exception as exc:
            return False, None, None, {"exception": str(exc)}

    # ------------------------------------------------------------------
    # Account info
    # ------------------------------------------------------------------

    def get_open_orders(self) -> tuple[bool, list]:
        if not self.ready:
            return False, []
        try:
            resp = self._client.account_orders(self._account_hash, maxResults=50, status="WORKING")
            orders = resp.json()
            return True, orders if isinstance(orders, list) else []
        except Exception as exc:
            return False, []

    @staticmethod
    def build_market_payload(ticker: str, side: str, quantity: int) -> dict:
        """
        Build a simple DAY MARKET order for EOD flattening.
        side: 'SELL' (close long) or 'BUY_TO_COVER' (close short).
        """
        return {
            "orderStrategyType": "SINGLE",
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderLegCollection": [
                {
                    "instruction": side,
                    "quantity": quantity,
                    "instrument": {"symbol": ticker, "assetType": "EQUITY"},
                }
            ],
        }

    def get_order_status(self, order_id: str) -> tuple[bool, str, dict]:
        """
        Query a specific order by ID.
        Returns (success, status_string, fills_dict).
        status_string: WORKING | FILLED | CANCELLED | REJECTED | UNKNOWN
        fills_dict: {"fill_price": float} if filled, else {}
        """
        if not self.ready:
            return False, "UNKNOWN", {}
        try:
            resp = self._client.order(self._account_hash, order_id)
            if resp.status_code != 200:
                return False, "UNKNOWN", {}
            data   = resp.json()
            status = data.get("status", "UNKNOWN")
            fills  = {}
            legs   = data.get("orderActivityCollection", [])
            if legs:
                executions = legs[0].get("executionLegs", [])
                if executions:
                    fills["fill_price"] = float(executions[0].get("price", 0))
            return True, status, fills
        except Exception as exc:
            return False, "UNKNOWN", {}

    def cancel_order(self, order_id: str) -> tuple[bool, str]:
        if not self.ready:
            return False, "not connected"
        try:
            resp = self._client.cancel_order(self._account_hash, order_id)
            if resp.status_code in (200, 204):
                return True, f"Order {order_id} cancelled"
            return False, f"HTTP {resp.status_code}"
        except Exception as exc:
            return False, str(exc)
