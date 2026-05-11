"""
OuroborosSentiment — now backed by MarketRegimeDetector.

Replaces the old VADER news-sentiment approach with objective market signals:
  VIX volatility index + SPY 200-day moving average.

The class name and is_safe() signature are preserved so validator.py
and any other callers need no changes.
"""

from src.sentiment.regime import MarketRegimeDetector


class OuroborosSentiment:
    def __init__(self):
        self._detector = MarketRegimeDetector()

    def is_safe(self, ticker: str) -> tuple[bool, float]:
        """
        Returns (safe, score) where score is the regime score (1.0 to -1.0).
        Blocks only in CRISIS (VIX > 35).
        ticker is accepted for interface compatibility but regime is market-wide.
        """
        return self._detector.is_safe(ticker)

    def get_regime(self):
        """Expose the full RegimeSnapshot for use in logs / LLM prompts."""
        return self._detector.detect()
