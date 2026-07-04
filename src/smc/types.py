"""
src/smc/types.py — single source of truth for all shared SMC contracts.
Every other smc module imports from here. No cross-module type duplication.
"""
from __future__ import annotations
from dataclasses import dataclass, field
from enum import Enum
import datetime


# ---------------------------------------------------------------------------
# Shared enums
# ---------------------------------------------------------------------------

class FVGDirection(str, Enum):
    BULL = "BULL"
    BEAR = "BEAR"


class Bias(str, Enum):
    BULL   = "BULL"
    BEAR   = "BEAR"
    RANGE  = "RANGE"


class Alignment(str, Enum):
    WITH     = "WITH"
    AGAINST  = "AGAINST"
    NEUTRAL  = "NEUTRAL"


class TrendRegime(str, Enum):
    TRENDING  = "TRENDING"
    RANGING   = "RANGING"
    VOLATILE  = "VOLATILE"


class FVGRegime(str, Enum):
    """Regime names as Gate 2 emits them (BULL/NEUTRAL/BEAR/CRISIS)."""
    BULL    = "BULL"
    NEUTRAL = "NEUTRAL"
    BEAR    = "BEAR"
    CRISIS  = "CRISIS"


class LLMVerdict(str, Enum):
    APPROVE = "APPROVE"
    CAUTION = "CAUTION"
    REJECT  = "REJECT"


# ---------------------------------------------------------------------------
# Shared dataclasses
# ---------------------------------------------------------------------------

@dataclass
class FVGSignal:
    ticker:     str
    direction:  FVGDirection
    gap_pct:    float           # gap size as % of price
    entry_price: float
    timestamp:  datetime.datetime
    bar_indices: tuple[int, int, int]  # (i-1, i, i+1)
    reject_reason: str = ""


@dataclass
class FVGResult:
    """Raw detection output; FVGSignal is the wired integration form."""
    signal:   FVGSignal | None
    rejected: bool
    reason:   str = ""


@dataclass
class FVGBoundaries:
    gap_top:    float
    gap_bottom: float


@dataclass
class TrendContext:
    htf_bias:  Bias
    ltf_bias:  Bias
    alignment: Alignment
    choch:     bool
    momentum:  float   # -1.0 to 1.0, normalized
    regime:    TrendRegime


@dataclass
class GateResult:
    gate_id: int
    passed:  bool
    reason:  str


@dataclass
class Gate4Result:
    verdict:    LLMVerdict
    confidence: float   # 0.0-1.0; dry-run returns 0.0
    rationale:  str = ""


@dataclass
class PipelineResult:
    passed:       bool
    gate_results: list[GateResult] = field(default_factory=list)
    gate4:        Gate4Result | None = None
    reject_gate:  int | None = None   # first gate that rejected, or None


@dataclass
class PositionSpec:
    shares: int
    entry:  float
    tp:     float
    sl:     float
    dollar_size: float   # pre-CAUTION-discount dollar amount
