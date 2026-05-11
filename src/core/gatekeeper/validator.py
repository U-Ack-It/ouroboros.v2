import json
import os
from src.core.intelligence.sentiment_engine import OuroborosSentiment
from src.core.intelligence.logger import log_decision
from src.llm_agent.agent import QuantAgent


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
        )

        if verdict.is_blocked():
            log_decision(
                ticker, "BLOCK", f"LLM Gate Rejected",
                regime.label, verdict.verdict, verdict.reasoning, verdict.confidence
            )
            return False, f"REJECTED (Gate 4): {verdict.reasoning}"

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