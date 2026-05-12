"""
Ouroboros Supervisor — joint L2 + L3 entry point.

L2 scans every 15 minutes.
L3 triggers automatically on any HIGH severity escalation from L2.
MEDIUM escalations are Telegram-only (human decides whether to call L3).
LOW escalations are logged only.

Usage:
    venv/bin/python3.14 supervisor/run_supervisor.py

Run alongside ouroboros_live.py (separate terminal or background process):
    venv/bin/python3.14 supervisor/run_supervisor.py &
"""

import sys
import os
import time
from datetime import datetime

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..")))

from supervisor.l2_ouroboros import run_scan
from supervisor.l3_fixer import run_l3

POLL_INTERVAL_SEC    = 900   # 15 min
L3_AUTO_SEVERITIES   = {"HIGH"}   # L3 triggers automatically for these


def run():
    print("[Supervisor] Starting — L2 every 15 min, L3 on HIGH escalations")
    while True:
        print(f"\n[Supervisor] Scan at {datetime.now().strftime('%H:%M:%S')}")

        try:
            new_escalations = run_scan()
        except Exception as exc:
            print(f"[Supervisor] L2 scan error: {exc}")
            new_escalations = []

        for esc in new_escalations:
            if esc["severity"] in L3_AUTO_SEVERITIES:
                print(f"[Supervisor] Routing {esc['id']} ({esc['severity']}) → L3 Fixer")
                try:
                    result = run_l3(esc)
                    status = "FIXED" if result["fixed"] else "PROPOSED"
                    print(f"[Supervisor] L3 {status}: {esc['id']}")
                except Exception as exc:
                    print(f"[Supervisor] L3 error on {esc['id']}: {exc}")
            else:
                print(f"[Supervisor] {esc['id']} ({esc['severity']}) — Telegram alert sent, awaiting human review")

        time.sleep(POLL_INTERVAL_SEC)


if __name__ == "__main__":
    run()
