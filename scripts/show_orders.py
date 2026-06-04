"""Show last N orders from the orders log."""
import json
import sys
from pathlib import Path

n = int(sys.argv[1]) if len(sys.argv) > 1 else 5
path = Path("logs/orders.json")
if not path.exists():
    print("No orders log found")
    sys.exit(0)

data = json.loads(path.read_text())
if not data:
    print("No orders yet")
    sys.exit(0)

for o in data[-n:]:
    order = o.get("order", {})
    ts = o.get("submitted_at", "")[:16]
    ticker = order.get("ticker", "?")
    direction = order.get("direction", "?")
    success = o.get("success", False)
    msg = o.get("message", "")[:80]
    icon = "✅" if success else "❌"
    print(f"  {icon} {ts} | {ticker:<8} {direction:<6} | {msg}")
