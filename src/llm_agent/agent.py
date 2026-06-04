"""
QuantAgent — LLM Reasoning Layer (Gate 4)
Uses Anthropic Claude with prompt caching for cost efficiency.
Runs after gates 1-3 (ethics, sentiment, FVG) have passed.
"""

import json
import os
from typing import Optional
from dotenv import load_dotenv

load_dotenv()

try:
    import anthropic
    _ANTHROPIC_AVAILABLE = True
except ImportError:
    _ANTHROPIC_AVAILABLE = False

from src.llm_agent.prompts import (
    SYSTEM_PROMPT, build_user_prompt,
    CRYPTO_SYSTEM_PROMPT, build_crypto_user_prompt,
)


class AgentVerdict:
    def __init__(self, verdict: str, confidence: float, reasoning: str, risk_flags: list):
        self.verdict = verdict          # APPROVE | CAUTION | REJECT
        self.confidence = confidence    # 0.0 - 1.0
        self.reasoning = reasoning      # 1-2 sentence narrative
        self.risk_flags = risk_flags    # list of flag strings

    def is_blocked(self) -> bool:
        return self.verdict == "REJECT"

    def is_flagged(self) -> bool:
        return self.verdict == "CAUTION"

    def __str__(self) -> str:
        flags = f" | Flags: {', '.join(self.risk_flags)}" if self.risk_flags else ""
        return f"[{self.verdict}] conf={self.confidence:.2f}{flags} — {self.reasoning}"

    @classmethod
    def fallback(cls, reason: str) -> "AgentVerdict":
        """Returns a CAUTION verdict when the LLM is unavailable."""
        return cls(
            verdict="CAUTION",
            confidence=0.5,
            reasoning=f"LLM gate unavailable ({reason}) — proceeding with caution.",
            risk_flags=["llm_gate_offline"]
        )


class QuantAgent:
    """
    Gate 4: LLM reasoning layer for trade proposals.
    Uses prompt caching — the system prompt is cached after the first call,
    reducing cost on subsequent calls within the same session window.
    """

    MODEL = "claude-sonnet-4-6"
    MAX_TOKENS = 256  # Verdict JSON is small — keep tight

    def __init__(self):
        api_key = os.getenv("ANTHROPIC_API_KEY", "").strip()
        if not api_key:
            print("⚠️  QuantAgent: ANTHROPIC_API_KEY not set — Gate 4 will run in fallback mode.")
            self._client = None
        elif not _ANTHROPIC_AVAILABLE:
            print("⚠️  QuantAgent: anthropic package not installed — Gate 4 in fallback mode.")
            self._client = None
        else:
            self._client = anthropic.Anthropic(api_key=api_key)

    def analyze_trade(
        self,
        ticker: str,
        asset_class: str,
        fvg_type: str,
        fvg_size: float,
        fvg_price: float,
        current_price: float,
        session: str,
        position_size_usd: float = 450.0,
        regime_score: float = 0.5,
        regime_label: str = "NEUTRAL",
        regime_summary: str = "",
        additional_context: str = "",
        recent_outcomes: str = "",
        # backward-compat alias
        sentiment_score: Optional[float] = None,
    ) -> AgentVerdict:
        """
        Runs Gate 4 LLM analysis on a trade proposal.
        Returns an AgentVerdict. Never raises — falls back to CAUTION on error.
        """
        if self._client is None:
            return AgentVerdict.fallback("client not initialized")

        if sentiment_score is not None:
            regime_score = sentiment_score  # legacy callers

        user_prompt = build_user_prompt(
            ticker=ticker,
            asset_class=asset_class,
            fvg_type=fvg_type,
            fvg_size=fvg_size,
            fvg_price=fvg_price,
            regime_score=regime_score,
            regime_label=regime_label,
            regime_summary=regime_summary,
            current_price=current_price,
            session=session,
            position_size_usd=position_size_usd,
            additional_context=additional_context,
            recent_outcomes=recent_outcomes,
        )

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=self.MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},  # Cache the system prompt
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}]
            )

            raw = response.content[0].text.strip()

            # Strip markdown code fences if present
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = json.loads(raw)

            return AgentVerdict(
                verdict=data.get("verdict", "CAUTION"),
                confidence=float(data.get("confidence", 0.5)),
                reasoning=data.get("reasoning", "No reasoning provided."),
                risk_flags=data.get("risk_flags", [])
            )

        except json.JSONDecodeError as e:
            return AgentVerdict.fallback(f"JSON parse error: {e}")
        except Exception as e:
            return AgentVerdict.fallback(str(e))

    def run_strategy(self):
        """Legacy stub entry point — kept for compatibility."""
        pass


class CryptoQuantAgent(QuantAgent):
    """
    Gate 4 for crypto trade proposals.
    Uses a separate system prompt with crypto asset universe, 24/7 sessions,
    and direction-aware bracket math (2% SL / 4% TP).
    """

    def analyze_crypto_trade(
        self,
        symbol: str,
        category: str,
        fvg_type: str,
        direction: str,
        gap_size: float,
        gap_pct: float,
        entry_price: float,
        last_price: float,
        session: str,
        position_size_usd: float = 150.0,
        sl_pct: float = 0.02,
        tp_pct: float = 0.04,
    ) -> AgentVerdict:
        """Crypto-specific Gate 4 analysis. Never raises."""
        if self._client is None:
            return AgentVerdict.fallback("client not initialized")

        user_prompt = build_crypto_user_prompt(
            symbol=symbol,
            category=category,
            fvg_type=fvg_type,
            direction=direction,
            gap_size=gap_size,
            gap_pct=gap_pct,
            entry_price=entry_price,
            last_price=last_price,
            session=session,
            position_size_usd=position_size_usd,
            sl_pct=sl_pct,
            tp_pct=tp_pct,
        )

        try:
            response = self._client.messages.create(
                model=self.MODEL,
                max_tokens=self.MAX_TOKENS,
                system=[
                    {
                        "type": "text",
                        "text": CRYPTO_SYSTEM_PROMPT,
                        "cache_control": {"type": "ephemeral"},
                    }
                ],
                messages=[{"role": "user", "content": user_prompt}]
            )

            raw = response.content[0].text.strip()
            if raw.startswith("```"):
                raw = raw.split("```")[1]
                if raw.startswith("json"):
                    raw = raw[4:]
                raw = raw.strip()

            data = json.loads(raw)
            return AgentVerdict(
                verdict=data.get("verdict", "CAUTION"),
                confidence=float(data.get("confidence", 0.5)),
                reasoning=data.get("reasoning", "No reasoning provided."),
                risk_flags=data.get("risk_flags", [])
            )
        except json.JSONDecodeError as e:
            return AgentVerdict.fallback(f"JSON parse error: {e}")
        except Exception as e:
            return AgentVerdict.fallback(str(e))
