"""
Cross-system credit stress signal.
Reads muni_scanner alert output and returns a stress flag + summary.
Falls back gracefully if muni_scanner is not installed or has no recent data.
"""

import json
from datetime import date, timedelta
from pathlib import Path

MUNI_OUTPUT = Path("../muni_scanner/output")


def get_credit_stress(lookback_days: int = 7) -> tuple[bool, str]:
    """
    Returns (stressed, summary).
    stressed=True when recent muni anomalies suggest credit market dislocation.
    Threshold: ≥2 bonds with spread >100bps in the lookback window.
    """
    if not MUNI_OUTPUT.exists():
        return False, ""

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    recent: list[dict] = []

    for p in sorted(MUNI_OUTPUT.glob("alerts_*.json")):
        try:
            alerts = json.loads(p.read_text())
            recent.extend(a for a in alerts if a.get("generated_at", "") >= cutoff)
        except (json.JSONDecodeError, OSError):
            pass

    if not recent:
        return False, ""

    high_spread = [a for a in recent if a.get("spread_bps", 0) >= 100]
    if len(high_spread) < 2:
        return False, ""

    issuers = "; ".join(
        f"{a['issuer'][:25]} ({a['state']}, +{a['spread_bps']}bps)"
        for a in high_spread[:3]
    )
    summary = f"Muni credit stress: {len(high_spread)} bonds >100bps wide — {issuers}"
    return True, summary
