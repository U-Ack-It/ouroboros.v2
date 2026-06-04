"""Ouroboros v2 — Quick Status"""
import sys, os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)) + "/..")
import json
import subprocess
from pathlib import Path
from datetime import datetime

# Process check
result = subprocess.run(
    ["pgrep", "-fa", "ouroboros_live"],
    capture_output=True, text=True
)
if result.stdout.strip():
    pid = result.stdout.strip().split()[0]
    print(f"  🟢 Process running (PID {pid})")
else:
    print(f"  🔴 Process NOT running")

# Alpaca account
try:
    from src.core.broker.alpaca_client import AlpacaClient
    c = AlpacaClient()
    c.connect()
    resp = c._session.get(f"{c._base}/v2/account")
    if resp.status_code == 200:
        a = resp.json()
        cash = float(a["cash"])
        equity = float(a["portfolio_value"])
        pnl = float(a["equity"]) - float(a["last_equity"])
        print(f"  💰 Cash: ${cash:,.2f} | Portfolio: ${equity:,.2f} | Today: ${pnl:+,.2f}")
except Exception as e:
    print(f"  ⚠️  Alpaca: {e}")

# Open positions
try:
    positions = c.get_open_positions()
    if positions:
        print(f"  📊 {len(positions)} open positions:")
        for p in positions:
            sym = p["symbol"]
            qty = p["qty"]
            side = "LONG" if int(qty) > 0 else "SHORT"
            pnl = float(p.get("unrealized_pl", 0))
            icon = "🟢" if pnl >= 0 else "🔴"
            print(f"     {icon} {sym:<8} {side} {abs(int(qty))}x | PnL ${pnl:+.2f}")
    else:
        print(f"  📊 No open positions")
except Exception:
    pass

# Regime
regime_path = Path("logs/regime_snapshot.json")
if regime_path.exists():
    r = json.loads(regime_path.read_text())
    age = (datetime.now() - datetime.fromisoformat(r["fetched_at"])).total_seconds() / 60
    stale = "⚠️ STALE" if age > 90 else ""
    print(f"  📈 Regime: {r['label']} | VIX={r.get('vix',0):.1f} | SPY=${r.get('spy_price',0):.2f} | {int(age)}m ago {stale}")

# Last scan
log_path = Path("logs/heartbeat.log")
if log_path.exists():
    lines = log_path.read_text().splitlines()
    for line in reversed(lines):
        if "FVG detected" in line or "ZOMBIE" in line or "═══" in line:
            print(f"  🔍 Last: {line.strip()[:80]}")
            break

# Escalations
esc_path = Path("logs/escalations.json")
if esc_path.exists():
    data = json.loads(esc_path.read_text())
    open_esc = [e for e in data if e["status"] == "OPEN"]
    if open_esc:
        print(f"  ⚠️  {len(open_esc)} open escalations")
    else:
        print(f"  ✅ No open escalations")
