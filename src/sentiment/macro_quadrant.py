"""
Macro Quadrant — Charles Gave / Institut des Libertés framework.

Classifies the economy into one of four quadrants based on the direction
of real growth and inflation, then derives:
  - asset rotation bias
  - muni spread adjustment (for bond_scanner)
  - real estate narrative angle (for GallerySense)
  - Gate 4 prior (injected into Ouroboros LLM context)

Quadrant map:
  Q1  Growth ↑  Inflation ↑  → Stagflation risk  — commodities, real assets, gold
  Q2  Growth ↑  Inflation ↓  → Goldilocks        — equities, growth, risk-on
  Q3  Growth ↓  Inflation ↑  → Stagflation       — gold, energy, short bonds
  Q4  Growth ↓  Inflation ↓  → Recession/Deflation — long bonds, defensives, cash

Proxies (all via yfinance, no data subscription needed):
  Growth:    SPY 3-month momentum  (positive = growth accelerating)
  Inflation: TIP/IEF ratio 3-month momentum  (positive = inflation rising)

Snapshot written to logs/macro_quadrant.json — shared across all three systems.
"""

import json
from dataclasses import dataclass, asdict
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import yfinance as yf

CACHE_PATH = Path("logs/macro_quadrant.json")
CACHE_TTL  = timedelta(hours=4)   # refresh every 4h — quadrant moves slowly


@dataclass
class QuadrantSnapshot:
    quadrant:      str    # "Q1" | "Q2" | "Q3" | "Q4"
    label:         str    # human label
    growth_up:     bool
    inflation_up:  bool
    growth_mom:    float  # 6-month SPY momentum %
    inflation_mom: float  # 6-month TIP/IEF ratio momentum %
    fetched_at:    str
    gave_signals:  str  = ""    # extended Gave indicator summary
    dollar_strong: bool = False
    curve_inverted: bool = False

    # ---- derived signals ------------------------------------------------

    @property
    def gate4_prior(self) -> str:
        """Gave/Gavekal macro prior for Gate 4 LLM context."""
        base = {
            "Q1": (
                "MACRO QUADRANT Q1 — Growth accelerating, inflation rising (Reflationary). "
                "Gave/Gavekal: favour real assets — gold, energy, commodities, EM. "
                "Bonds face headwind; late-Q1 cyclicals lag as margins compress."
            ),
            "Q2": (
                "MACRO QUADRANT Q2 — Goldilocks: growth up, inflation contained. "
                "Gave/Gavekal: best environment for equities and risk assets. "
                "Long duration bonds and defensives underperform."
            ),
            "Q3": (
                "MACRO QUADRANT Q3 — Stagflation: growth falling, inflation rising. "
                "Gave/Gavekal: most destructive quadrant — short nominal bonds, hold gold/energy. "
                "Equities and credit under severe pressure; cash destroying value."
            ),
            "Q4": (
                "MACRO QUADRANT Q4 — Deflation / Recession: growth and inflation both falling. "
                "Gave/Gavekal: long-duration bonds outperform; defensives and cash win. "
                "Avoid cyclicals and credit; spreads widen."
            ),
        }[self.quadrant.split(" ")[0]]  # strip override suffix

        extras = []
        if self.dollar_strong:
            extras.append("Strong USD — headwind for EM, commodities, and foreign equities.")
        if self.curve_inverted:
            extras.append("Inverted yield curve — late-cycle warning; recession probability elevated.")
        if self.gave_signals:
            extras.append(f"Gave indicators: {self.gave_signals}")

        return base + (" | " + " | ".join(extras) if extras else "")

    @property
    def muni_spread_adjustment_bps(self) -> float:
        """
        Adjust anomaly threshold for bond_scanner.
        Wider spreads are normal in Q3/Q4 — raise the bar to avoid false positives.
        """
        return {"Q1": 0.0, "Q2": -10.0, "Q3": 25.0, "Q4": 15.0}[self.quadrant]

    @property
    def real_estate_angle(self) -> str:
        """Narrative angle injected into GallerySense drip copy."""
        return {
            "Q1": (
                "inflation-hedge framing — real estate as a hard asset that "
                "preserves purchasing power when prices rise"
            ),
            "Q2": (
                "wealth-building framing — strong economy creates demand; "
                "now is when confident buyers move"
            ),
            "Q3": (
                "scarcity framing — stagflation erodes cash; "
                "tangible assets like real estate protect capital"
            ),
            "Q4": (
                "safe-haven framing — in uncertainty, prime real estate "
                "holds value better than financial assets"
            ),
        }[self.quadrant]

    def summary(self) -> str:
        g = "↑" if self.growth_up else "↓"
        i = "↑" if self.inflation_up else "↓"
        flags = []
        if self.dollar_strong:  flags.append("USD↑")
        if self.curve_inverted: flags.append("curve inv")
        flag_str = "  " + "/".join(flags) if flags else ""
        return (
            f"{self.quadrant} {self.label}  "
            f"growth{g}({self.growth_mom:+.1f}%)  "
            f"inflation{i}({self.inflation_mom:+.1f}%){flag_str}"
        )


class MacroQuadrantDetector:
    def __init__(self):
        self._cache: Optional[QuadrantSnapshot] = None

    def detect(self) -> QuadrantSnapshot:
        # Try loading from disk cache first (shared across processes/systems)
        if self._cache is None:
            self._cache = self._load_disk_cache()

        if self._cache is not None:
            age = datetime.now() - datetime.fromisoformat(self._cache.fetched_at)
            if age < CACHE_TTL:
                return self._cache

        snap = self._fetch()
        self._cache = snap
        self._write(snap)
        return snap

    def _fetch(self) -> QuadrantSnapshot:
        # --- Primary signals (same as before) ---
        growth_mom    = self._momentum("SPY", period="6mo")
        inflation_mom = self._inflation_momentum(period="6mo")

        growth_up    = growth_mom > 0.0
        inflation_up = inflation_mom > 0.0

        # --- Gave confirmation signals ---
        # 1. Gold/bond divergence: GLD rising + TLT falling = stagflation confirmation
        gld_mom = self._momentum("GLD", period="3mo")
        tlt_mom = self._momentum("TLT", period="3mo")
        gold_bond_diverge = gld_mom > 2.0 and tlt_mom < -2.0   # GLD up, bonds down

        # 2. Real yield proxy: TIP mom - IEF mom; negative = real rates falling = hard assets win
        tip_mom = self._momentum("TIP", period="3mo")
        real_yield_falling = (tip_mom - tlt_mom) > 1.0          # TIPS outpacing nominal

        # 3. Dollar direction: strong dollar = headwind for commodities/EM
        uup_mom = self._momentum("UUP", period="3mo")            # UUP = USD bull ETF
        dollar_strong = uup_mom > 1.5

        # 4. Yield curve: 2yr vs 10yr via ^IRX (13-week) and ^TNX (10yr)
        curve_inverted = self._yield_curve_inverted()

        # --- Quadrant with Gave overrides ---
        if growth_up and inflation_up:
            quadrant, label = "Q1", "Reflationary Growth"
        elif growth_up and not inflation_up:
            quadrant, label = "Q2", "Goldilocks"
        elif not growth_up and inflation_up:
            quadrant, label = "Q3", "Stagflation"
        else:
            quadrant, label = "Q4", "Deflation / Recession"

        # Gave override: if gold/bond diverge strongly, bias toward Q3 even if growth looks ok
        if gold_bond_diverge and quadrant == "Q1":
            quadrant, label = "Q3", "Stagflation (gold/bond diverge)"

        # Build extended context string for Gate 4
        gave_signals = (
            f"GLD{gld_mom:+.1f}% TLT{tlt_mom:+.1f}% "
            f"USD{uup_mom:+.1f}% "
            f"curve={'inverted' if curve_inverted else 'normal'} "
            f"gold/bond_diverge={gold_bond_diverge} real_yield_falling={real_yield_falling}"
        )

        return QuadrantSnapshot(
            quadrant=quadrant,
            label=label,
            growth_up=growth_up,
            inflation_up=inflation_up,
            growth_mom=round(growth_mom, 2),
            inflation_mom=round(inflation_mom, 2),
            fetched_at=datetime.now().isoformat(),
            gave_signals=gave_signals,
            dollar_strong=dollar_strong,
            curve_inverted=curve_inverted,
        )

    def _momentum(self, ticker: str, period: str = "6mo") -> float:
        """Returns % price change over the period. Positive = uptrend."""
        try:
            df = yf.Ticker(ticker).history(period=period, interval="1wk")
            if df.empty or len(df) < 4:
                return 0.0
            start = float(df["Close"].iloc[0])
            end   = float(df["Close"].iloc[-1])
            return (end - start) / start * 100 if start else 0.0
        except Exception:
            return 0.0

    def _yield_curve_inverted(self) -> bool:
        """2yr/10yr curve: inverted when short rates exceed long rates."""
        try:
            irx = yf.Ticker("^IRX").history(period="5d")["Close"]  # 13-week (proxy 2yr)
            tnx = yf.Ticker("^TNX").history(period="5d")["Close"]  # 10yr
            if irx.empty or tnx.empty:
                return False
            return float(irx.iloc[-1]) > float(tnx.iloc[-1])
        except Exception:
            return False

    def _inflation_momentum(self, period: str = "6mo") -> float:
        """
        TIP/IEF ratio momentum as inflation proxy.
        TIP = inflation-protected treasuries; IEF = nominal 7-10yr.
        Rising ratio = inflation expectations rising.
        """
        try:
            tip = yf.Ticker("TIP").history(period=period, interval="1wk")["Close"]
            ief = yf.Ticker("IEF").history(period=period, interval="1wk")["Close"]
            if tip.empty or ief.empty or len(tip) < 4:
                return 0.0
            ratio_start = float(tip.iloc[0])  / float(ief.iloc[0])
            ratio_end   = float(tip.iloc[-1]) / float(ief.iloc[-1])
            return (ratio_end - ratio_start) / ratio_start * 100 if ratio_start else 0.0
        except Exception:
            return 0.0

    def _write(self, snap: QuadrantSnapshot) -> None:
        CACHE_PATH.parent.mkdir(exist_ok=True)
        try:
            CACHE_PATH.write_text(json.dumps(asdict(snap), indent=2))
        except Exception:
            pass

    def _load_disk_cache(self) -> Optional[QuadrantSnapshot]:
        if not CACHE_PATH.exists():
            return None
        try:
            d = json.loads(CACHE_PATH.read_text())
            return QuadrantSnapshot(**d)
        except Exception:
            return None


# ---------------------------------------------------------------------------
# Convenience — shared snapshot for cross-system use
# ---------------------------------------------------------------------------

_detector: Optional[MacroQuadrantDetector] = None


def get_quadrant() -> QuadrantSnapshot:
    global _detector
    if _detector is None:
        _detector = MacroQuadrantDetector()
    return _detector.detect()


def get_gate4_prior() -> str:
    return get_quadrant().gate4_prior


def get_muni_threshold_adjustment() -> float:
    return get_quadrant().muni_spread_adjustment_bps


def get_real_estate_angle() -> str:
    return get_quadrant().real_estate_angle
