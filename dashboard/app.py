"""
Ouroboros v2 — Risk Dashboard

FastAPI server that exposes JSON APIs consumed by the single-page HTML
dashboard. Run from the project root:

    venv/bin/python3.14 -m uvicorn dashboard.app:app --reload --port 8765

Endpoints:
    GET /            → HTML dashboard
    GET /api/stats   → portfolio summary (equity, P&L, Sharpe, etc.)
    GET /api/equity  → equity curve [{t, v}]  (resolved trades only)
    GET /api/positions → open positions list
    GET /api/orders  → last N orders from orders log
    GET /api/gate    → last N gate decisions from trading_decisions.log
"""

import json
import math
import os
import re
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import FastAPI, Query
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.staticfiles import StaticFiles

from portfolio.ledger import load_trades
from portfolio.analytics import compute_all

app = FastAPI(title="Ouroboros v2 Dashboard", docs_url=None, redoc_url=None)

# Static files (HTML/JS/CSS)
_STATIC = Path(__file__).parent / "static"
app.mount("/static", StaticFiles(directory=str(_STATIC)), name="static")

ORDERS_LOG   = Path("logs/orders.json")
GATE_LOG     = Path("logs/trading_decisions.log")
RISK_POLICY  = Path("config/risk_policy.json")

def _starting_capital() -> float:
    try:
        return float(json.loads(RISK_POLICY.read_text()).get("starting_capital", 3000.0))
    except Exception:
        return 3000.0

STARTING_CAPITAL = _starting_capital()


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_float(v) -> float:
    if v is None or (isinstance(v, float) and not math.isfinite(v)):
        return 0.0
    return float(v)


def _load_orders(n: int = 50) -> list[dict]:
    if not ORDERS_LOG.exists():
        return []
    try:
        with open(ORDERS_LOG) as f:
            rows = json.load(f)
        return rows[-n:][::-1]  # most-recent first
    except Exception:
        return []


def _load_gate_log(n: int = 50) -> list[dict]:
    """Parse structured lines from trading_decisions.log into dicts."""
    if not GATE_LOG.exists():
        return []
    entries = []
    try:
        lines = GATE_LOG.read_text().splitlines()
        for line in reversed(lines[-500:]):
            line = line.strip()
            if not line:
                continue
            # Format: 2026-05-10 21:30:00 | TICKER | STATUS | REGIME: label | reason ...
            parts = [p.strip() for p in line.split("|")]
            if len(parts) >= 3:
                entries.append({
                    "ts":      parts[0] if len(parts) > 0 else "",
                    "ticker":  parts[1] if len(parts) > 1 else "",
                    "verdict": parts[2] if len(parts) > 2 else "",
                    "detail":  " | ".join(parts[3:]) if len(parts) > 3 else "",
                })
            else:
                entries.append({"ts": "", "ticker": "", "verdict": "", "detail": line})
            if len(entries) >= n:
                break
    except Exception:
        pass
    return entries


# ---------------------------------------------------------------------------
# API routes
# ---------------------------------------------------------------------------

@app.get("/api/stats")
def api_stats():
    trades = load_trades()
    resolved = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
    open_pos  = [t for t in trades if t.get("outcome") == "OPEN"]

    stats = compute_all(resolved) if resolved else {}

    today = datetime.now().date().isoformat()
    today_trades = [t for t in resolved if t.get("entry_time", "")[:10] == today]
    pnl_today = sum(t.get("pnl_usd", 0.0) for t in today_trades)

    return {
        "total_trades":    len(resolved),
        "open_positions":  len(open_pos),
        "wins":            sum(1 for t in resolved if t["outcome"] == "WIN"),
        "losses":          sum(1 for t in resolved if t["outcome"] == "LOSS"),
        "win_rate":        _safe_float(stats.get("win_rate",         0.0)),
        "net_pnl":         _safe_float(stats.get("net_pnl",          0.0)),
        "total_return_pct":_safe_float(stats.get("total_return_pct", 0.0)),
        "sharpe":          _safe_float(stats.get("sharpe",           0.0)),
        "sortino":         _safe_float(stats.get("sortino",          0.0)),
        "max_dd_pct":      _safe_float(stats.get("max_dd_pct",       0.0)),
        "final_equity":    _safe_float(stats.get("final_equity",     STARTING_CAPITAL)),
        "pnl_today":       _safe_float(pnl_today),
        "as_of":           datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    }


@app.get("/api/equity")
def api_equity():
    """Equity curve as list of {t, v} objects for Chart.js."""
    trades = load_trades()
    resolved = sorted(
        [t for t in trades if t.get("outcome") in ("WIN", "LOSS")],
        key=lambda t: t.get("entry_time", ""),
    )
    equity = STARTING_CAPITAL
    points = [{"t": "start", "v": equity}]
    for t in resolved:
        equity += t.get("pnl_usd", 0.0)
        points.append({
            "t": t.get("exit_time", t.get("entry_time", ""))[:16],
            "v": round(equity, 2),
        })
    return points


@app.get("/api/positions")
def api_positions():
    """Open positions (outcome=OPEN)."""
    trades = load_trades()
    open_pos = [t for t in trades if t.get("outcome") == "OPEN"]
    open_pos.sort(key=lambda t: t.get("entry_time", ""), reverse=True)
    return open_pos


@app.get("/api/orders")
def api_orders(n: int = Query(50, ge=1, le=500)):
    return _load_orders(n)


@app.get("/api/gate")
def api_gate(n: int = Query(50, ge=1, le=500)):
    return _load_gate_log(n)


# ---------------------------------------------------------------------------
# HTML
# ---------------------------------------------------------------------------

@app.get("/", response_class=HTMLResponse)
def dashboard():
    html = (_STATIC / "index.html").read_text()
    return HTMLResponse(content=html)


@app.get("/api/regime")
def api_regime():
    """Current market regime from snapshot."""
    regime_path = Path("logs/regime_snapshot.json")
    if not regime_path.exists():
        return {"label": "UNKNOWN", "score": 0, "vix": 0}
    try:
        data = json.loads(regime_path.read_text())
        # Calculate age in minutes
        fetched = datetime.fromisoformat(data.get("fetched_at", ""))
        age_min = (datetime.now() - fetched).total_seconds() / 60
        data["age_minutes"] = round(age_min, 1)
        data["stale"] = age_min > 90
        return data
    except Exception:
        return {"label": "ERROR", "score": 0, "vix": 0}


@app.get("/api/scan")
def api_last_scan():
    """Parse last scan cycle from heartbeat log."""
    log_path = Path("logs/heartbeat.log")
    if not log_path.exists():
        return {"session": "unknown", "results": []}
    try:
        lines = log_path.read_text().splitlines()
        # Find last scan block (starts with ═══)
        scan_start = -1
        scan_end = -1
        for i in range(len(lines) - 1, -1, -1):
            if lines[i].startswith("──────") and scan_end == -1:
                scan_end = i
            if "═══" in lines[i] and scan_end != -1:
                scan_start = i
                break
        if scan_start == -1:
            return {"session": "unknown", "results": []}
        block = lines[scan_start:scan_end + 1]
        session = block[0] if block else ""
        regime_line = block[1] if len(block) > 1 else ""
        scan_line = block[2] if len(block) > 2 else ""
        results = []
        for line in block[4:]:
            line = line.strip()
            if not line or line.startswith("──"):
                continue
            if "others: no FVG" in line:
                results.append({"type": "summary", "text": line.strip("── ")})
                continue
            results.append({"type": "detail", "text": line})
        return {
            "session": session,
            "regime": regime_line,
            "scan_summary": scan_line,
            "results": results,
        }
    except Exception as e:
        return {"error": str(e)}
