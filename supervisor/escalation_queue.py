"""
Escalation queue — shared between L2 (writer) and L3 (reader).

Format: logs/escalations.json  — append-only list of escalation dicts.
Each escalation:
  {
    "id":           "esc-20260512-001",
    "timestamp":    "2026-05-12T14:00:00",
    "project":      "ouroboros.v2",
    "type":         "PNL_COLLAPSE",
    "severity":     "HIGH",          # HIGH | MEDIUM | LOW
    "detail":       "...",
    "hypothesis":   "...",
    "files_to_check": [...],
    "status":       "OPEN",          # OPEN | IN_PROGRESS | RESOLVED | DISMISSED
    "resolution":   null
  }
"""

import json
import os
from datetime import datetime, timezone
from typing import Optional

QUEUE_PATH = "logs/escalations.json"


def _load() -> list:
    if not os.path.exists(QUEUE_PATH):
        return []
    try:
        with open(QUEUE_PATH) as f:
            return json.load(f)
    except Exception:
        return []


def _save(records: list) -> None:
    os.makedirs(os.path.dirname(QUEUE_PATH), exist_ok=True)
    with open(QUEUE_PATH, "w") as f:
        json.dump(records, f, indent=2)


def push(
    anomaly_type:   str,
    severity:       str,
    detail:         str,
    hypothesis:     str,
    files_to_check: list,
    project:        str = "ouroboros.v2",
) -> dict:
    records = _load()
    esc_id  = f"esc-{datetime.now().strftime('%Y%m%d')}-{len(records)+1:03d}"
    record  = {
        "id":             esc_id,
        "timestamp":      datetime.now(timezone.utc).isoformat(),
        "project":        project,
        "type":           anomaly_type,
        "severity":       severity,
        "detail":         detail,
        "hypothesis":     hypothesis,
        "files_to_check": files_to_check,
        "status":         "OPEN",
        "resolution":     None,
    }
    records.append(record)
    _save(records)
    return record


def get_open(severity: Optional[str] = None) -> list:
    records = _load()
    open_esc = [r for r in records if r.get("status") == "OPEN"]
    if severity:
        open_esc = [r for r in open_esc if r.get("severity") == severity]
    return open_esc


def resolve(esc_id: str, resolution: str) -> bool:
    records = _load()
    for r in records:
        if r["id"] == esc_id:
            r["status"]     = "RESOLVED"
            r["resolution"] = resolution
            r["resolved_at"] = datetime.now(timezone.utc).isoformat()
            _save(records)
            return True
    return False


def dismiss(esc_id: str, reason: str) -> bool:
    records = _load()
    for r in records:
        if r["id"] == esc_id:
            r["status"]     = "DISMISSED"
            r["resolution"] = reason
            _save(records)
            return True
    return False


def already_open(anomaly_type: str, within_hours: int = 24) -> bool:
    """Prevent duplicate escalations for the same anomaly type."""
    from datetime import timedelta
    records = _load()
    cutoff  = datetime.now(timezone.utc) - timedelta(hours=within_hours)
    for r in records:
        if r.get("type") != anomaly_type:
            continue
        if r.get("status") not in ("OPEN", "IN_PROGRESS"):
            continue
        ts = r.get("timestamp", "")
        try:
            rec_time = datetime.fromisoformat(ts)
            if rec_time.tzinfo is None:
                from datetime import timezone as tz
                rec_time = rec_time.replace(tzinfo=tz.utc)
            if rec_time > cutoff:
                return True
        except Exception:
            pass
    return False
