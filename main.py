"""
Ouroboros v2 — Entry Point

The live trading system is launched via ouroboros_live.py, not this file.
This script exists only as a redirect for anyone who runs main.py by habit.

Usage:
    python ouroboros_live.py          # full pre-flight + heartbeat
    python ouroboros_live.py --check  # pre-flight check only
"""

import sys

print("\n" + "=" * 50)
print("  OUROBOROS v2 — REDIRECTING")
print("=" * 50)
print("\n  main.py is deprecated.")
print("  Use: python ouroboros_live.py")
print("  Or:  python ouroboros_live.py --check")
print("\n" + "=" * 50 + "\n")

sys.exit(0)
