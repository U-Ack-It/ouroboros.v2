"""Show open escalations."""
import json
from pathlib import Path

path = Path("logs/escalations.json")
if not path.exists():
    print("No escalations file")
    exit(0)

data = json.loads(path.read_text())
open_esc = [e for e in data if e["status"] == "OPEN"]

if not open_esc:
    print("  ✅ No open escalations")
    exit(0)

print(f"  ⚠️  {len(open_esc)} open escalations:")
for e in open_esc[:10]:
    ts = e.get("timestamp", "")[:16]
    etype = e.get("type", "?")
    detail = e.get("detail", "")[:60]
    severity = e.get("severity", "?")
    print(f"  [{severity}] {ts} | {etype}: {detail}")
