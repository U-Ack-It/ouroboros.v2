"""
Portfolio Ledger — persistent trade store.

Appends individual trades and bulk-imports from backtest JSON files.
All trades are stored in data/trades_ledger.json with a stable schema.

Schema per trade entry:
  id, source, ticker, asset_class, session, direction, fvg_type, fvg_size,
  entry_price, stop_loss, take_profit, exit_price,
  entry_time, exit_time, outcome, pnl_usd, pnl_pct,
  position_size_usd, shares, recorded_at
"""

import json
import os
import uuid
from datetime import datetime, timezone

LEDGER_PATH = "data/trades_ledger.json"


def _load_raw() -> dict:
    if not os.path.exists(LEDGER_PATH):
        return {"schema_version": 1, "trades": []}
    with open(LEDGER_PATH) as f:
        return json.load(f)


def _save_raw(data: dict):
    os.makedirs(os.path.dirname(LEDGER_PATH), exist_ok=True)
    with open(LEDGER_PATH, "w") as f:
        json.dump(data, f, indent=2)


def load_trades(since: str = None) -> list[dict]:
    """
    Load all trades from the ledger.
    since: ISO date string 'YYYY-MM-DD' — only return trades with entry_time >= since.
    """
    data = _load_raw()
    trades = data.get("trades", [])
    if since:
        trades = [t for t in trades if t.get("entry_time", "") >= since]
    return trades


def append_trade(trade: dict, source: str = "live") -> str:
    """
    Append a single trade dict to the ledger. Returns the assigned trade ID.
    trade must contain at minimum: ticker, outcome, pnl_usd, pnl_pct, entry_time.
    """
    data = _load_raw()
    trade_id = str(uuid.uuid4())[:8]
    entry = {
        "id": trade_id,
        "source": source,
        "recorded_at": datetime.now(timezone.utc).isoformat(),
        **trade,
    }
    data["trades"].append(entry)
    _save_raw(data)
    return trade_id


def import_backtest(path: str, skip_open: bool = True) -> int:
    """
    Bulk-import trades from a backtester JSON file.
    Returns number of trades imported.
    skip_open: if True, trades with outcome=OPEN are excluded (incomplete bracket).
    """
    with open(path) as f:
        backtest = json.load(f)

    raw_trades = backtest.get("trades", [])
    imported = 0

    data = _load_raw()
    existing_keys = {
        (t.get("ticker"), t.get("entry_time"), t.get("source"))
        for t in data["trades"]
    }

    for t in raw_trades:
        if skip_open and t.get("outcome") == "OPEN":
            continue
        dedup_key = (t.get("ticker"), t.get("entry_time"), "backtest")
        if dedup_key in existing_keys:
            continue
        trade_id = str(uuid.uuid4())[:8]
        data["trades"].append({
            "id": trade_id,
            "source": "backtest",
            "recorded_at": datetime.now(timezone.utc).isoformat(),
            **t,
        })
        existing_keys.add(dedup_key)
        imported += 1

    _save_raw(data)
    return imported


def load_open_positions() -> list[dict]:
    """Return all trades whose outcome is OPEN (pending bracket fills)."""
    data = _load_raw()
    return [t for t in data.get("trades", []) if t.get("outcome") == "OPEN"]


def update_trade(trade_id: str, updates: dict):
    """
    Update fields of an existing trade by its 'id' field.
    Merges updates dict into the matching trade record.
    """
    data = _load_raw()
    for trade in data["trades"]:
        if trade.get("id") == trade_id:
            trade.update(updates)
            break
    _save_raw(data)


def clear_ledger():
    _save_raw({"schema_version": 1, "trades": []})
