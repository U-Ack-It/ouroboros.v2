import json
import os
from src.core.intelligence.sentiment_engine import OuroborosSentiment
from src.core.intelligence.logger import log_decision

class TradeValidator:
    def __init__(self, policy_path="config/risk_policy.json"):
        self.policy = self._load_policy(policy_path)
        self.sentiment_engine = OuroborosSentiment()

    def is_ethical(self, ticker):
        boycott_list = self.policy.get("boycott_list", [])
        if ticker in boycott_list:
            return False, f"Ticker {ticker} is BLACKLISTED."
        return True, "Ethical check passed."

    def validate_risk(self, ticker, price, size, vol, smc_data=None):
        # 1. Ethics
        is_ethic, eth_msg = self.is_ethical(ticker)
        if not is_ethic:
            log_decision(ticker, "BLOCK", eth_msg, 0.0)
            return False, eth_msg

        # 2. Sentiment
        is_safe, score = self.sentiment_engine.is_safe(ticker)
        if not is_safe:
            log_decision(ticker, "BLOCK", "Toxic Sentiment", score)
            return False, f"REJECTED: Sentiment ({score})"

        # 3. SMC/Technical
        if smc_data and not smc_data.get("has_imbalance", False):
            log_decision(ticker, "BLOCK", "No FVG Found", score)
            return False, "REJECTED: No Fair Value Gap."

        log_decision(ticker, "PASS", "All Gates Cleared", score)
        return True, f"APPROVED: Sentiment ({score})"

    def _load_policy(self, path):
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}