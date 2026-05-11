"""
Order data models for Schwab execution.
"""

from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from typing import Optional
import json


@dataclass
class BracketOrder:
    """Full record of a bracket order before submission."""
    ticker:         str
    direction:      str        # LONG | SHORT
    quantity:       int
    entry_price:    float      # estimated fill (used for bracket calc)
    stop_loss:      float
    take_profit:    float
    sl_pct:         float
    tp_pct:         float
    position_usd:   float
    session:        str
    fvg_type:       str
    dry_run:        bool
    created_at:     str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class OrderResult:
    """Result returned after attempting to place an order."""
    order: BracketOrder
    success:    bool
    order_id:   Optional[str]  # Schwab order ID from response header
    http_status: Optional[int]
    message:    str
    raw_response: Optional[dict]
    submitted_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        d = asdict(self)
        # raw_response can be large — truncate for log
        if d.get("raw_response") and len(str(d["raw_response"])) > 500:
            d["raw_response"] = str(d["raw_response"])[:500] + "…"
        return d

    @property
    def icon(self) -> str:
        if self.order.dry_run:
            return "🧪"
        return "✅" if self.success else "❌"
