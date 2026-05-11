import json
import os
from src.core.intelligence.sentiment_engine import OuroborosSentiment
from src.core.intelligence.logger import log_decision
from src.llm_agent.agent import QuantAgent
from src.sentiment.credit_signal import get_credit_stress
from portfolio.trade_memory import get_recent_outcomes, get_win_rate


class TradeValidator:
    def __init__(self, policy_path="config/risk_policy.json"):
        self.policy = self._load_policy(policy_path)
        self.sentiment_engine = OuroborosSentiment()
        self.agent = QuantAgent()

    def is_ethical(self, ticker):
        boycott_list = self.policy.get("boycott_list", [])
        if ticker in boycott_list:
            return False, f"Ticker {ticker} is BLACKLISTED."
        return True, "Ethical check passed."

    def validate_risk(self, ticker, price, size, vol, smc_data=None, session="UNKNOWN"):
        # Gate 0: Equity universe whitelist
        asset_map = self.policy.get("neutrality_priority", {}).get("asset_mapping", {})
        if asset_map and ticker not in asset_map:
            return False, f"REJECTED: {ticker} not in approved equity universe."

        # Gate 1: Ethics
        is_ethic, eth_msg = self.is_ethical(ticker)
        if not is_ethic:
            log_decision(ticker, "BLOCK", eth_msg, 0.0)
            return False, eth_msg

        # Gate 2: Market regime
        is_safe, score = self.sentiment_engine.is_safe(ticker)
        regime = self.sentiment_engine.get_regime()
        if not is_safe:
            log_decision(ticker, "BLOCK", f"CRISIS regime (VIX={regime.vix:.1f})", score)
            return False, f"REJECTED: Market in CRISIS (VIX={regime.vix:.1f} > 35)"

        # Gate 3: SMC/Technical
        has_fvg = smc_data and smc_data.get("has_imbalance", False)
        if not has_fvg:
            log_decision(ticker, "BLOCK", "No FVG Found", score)
            return False, "REJECTED: No Fair Value Gap."

        # Gate 4: LLM Reasoning
        asset_map = self.policy.get("neutrality_priority", {}).get("asset_mapping", {})
        asset_class = asset_map.get(ticker, "Global-Unknown")
        risk_params = self.policy.get("risk_parameters", {})
        position_size = risk_params.get("max_position_size_usd", 450.0)

        recent_outcomes = get_recent_outcomes(ticker)

        credit_stressed, credit_summary = get_credit_stress()
        additional_context = credit_summary if credit_stressed else ""

        verdict = self.agent.analyze_trade(
            ticker=ticker,
            asset_class=asset_class,
            fvg_type=smc_data.get("type", "UNKNOWN"),
            fvg_size=float(smc_data.get("size", 0.0)),
            fvg_price=float(smc_data.get("price", price or 0.0)),
            regime_score=score,
            regime_label=regime.label,
            regime_summary=regime.summary(),
            current_price=float(price or 0.0),
            session=session,
            position_size_usd=position_size,
            recent_outcomes=recent_outcomes,
            additional_context=additional_context,
        )

        if verdict.is_blocked():
            log_decision(
                ticker, "BLOCK", f"LLM Gate Rejected",
                regime.label, verdict.verdict, verdict.reasoning, verdict.confidence
            )
            return False, f"REJECTED (Gate 4): {verdict.reasoning}"

        # Adaptive confidence: downgrade APPROVE → CAUTION if historical win rate is poor
        win_rate = get_win_rate(ticker)
        if win_rate is not None and win_rate < 0.35 and not verdict.is_flagged():
            verdict.verdict = "CAUTION"
            verdict.risk_flags = list(verdict.risk_flags) + [f"poor_win_rate_{win_rate:.0%}"]

        final_status = "CAUTION" if verdict.is_flagged() else "PASS"
        log_decision(
            ticker, final_status, "All Gates Cleared",
            regime.label, verdict.verdict, verdict.reasoning, verdict.confidence
        )

        flag_note = f" ⚠️  {', '.join(verdict.risk_flags)}" if verdict.risk_flags else ""
        return True, f"APPROVED (conf={verdict.confidence:.2f}): {verdict.reasoning}{flag_note}"

    def _load_policy(self, path):
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}