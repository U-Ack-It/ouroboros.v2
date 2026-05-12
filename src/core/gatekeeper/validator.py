import json
import os
from src.core.intelligence.sentiment_engine import OuroborosSentiment
from src.core.intelligence.logger import log_decision
from src.llm_agent.agent import QuantAgent
from src.sentiment.credit_signal import get_credit_stress
from portfolio.trade_memory import get_recent_outcomes, get_win_rate, get_consecutive_losses


class TradeValidator:
    def __init__(self, policy_path="config/risk_policy.json", config_path="config/execution_config.json"):
        self.policy = self._load_policy(policy_path)
        self._cfg   = self._load_policy(config_path)
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
            log_decision(ticker, "BLOCK", "Not in approved universe", "—")
            return False, f"REJECTED: {ticker} not in approved equity universe."

        # Gate 1: Ethics
        is_ethic, eth_msg = self.is_ethical(ticker)
        if not is_ethic:
            log_decision(ticker, "BLOCK", eth_msg, "—")
            return False, eth_msg

        # Gate 2: Market regime
        is_safe, score = self.sentiment_engine.is_safe(ticker)
        regime = self.sentiment_engine.get_regime()
        if not is_safe:
            log_decision(ticker, "BLOCK", f"CRISIS (VIX={regime.vix:.1f})", regime.label)
            return False, f"REJECTED: Market in CRISIS (VIX={regime.vix:.1f} > 35)"

        # Gate 3: SMC/Technical
        has_fvg = smc_data and smc_data.get("has_imbalance", False)
        if not has_fvg:
            log_decision(ticker, "BLOCK", "No FVG Found", regime.label)
            return False, "REJECTED: No Fair Value Gap."

        # Gate 3.5a: FVG size floor — skip micro-gaps before spending LLM tokens
        fvg_size  = float(smc_data.get("size", 0.0))
        fvg_price = float(smc_data.get("price", price or 1.0))
        fvg_pct   = fvg_size / fvg_price if fvg_price else 0.0
        min_fvg   = float(self._cfg.get("min_fvg_pct", 0.001))
        if fvg_pct < min_fvg:
            log_decision(ticker, "BLOCK", f"FVG too small ({fvg_pct:.4%} < {min_fvg:.4%})", regime.label)
            return False, f"REJECTED: FVG {fvg_pct:.4%} below minimum {min_fvg:.4%} — noise threshold."

        # Gate 3.5b: Consecutive loss circuit breaker
        halt_n = int(self._cfg.get("consecutive_loss_halt", 3))
        consec  = get_consecutive_losses(ticker)
        if consec >= halt_n:
            log_decision(ticker, "BLOCK", f"{consec} consecutive losses", regime.label)
            return False, f"REJECTED: {ticker} halted — {consec} consecutive losses (limit {halt_n})."

        # Gate 4: LLM Reasoning
        asset_map = self.policy.get("neutrality_priority", {}).get("asset_mapping", {})
        asset_class = asset_map.get(ticker, "Global-Unknown")
        risk_params = self.policy.get("risk_parameters", {})
        base_position = risk_params.get("max_position_size_usd", 450.0)
        regime_sizes  = self.policy.get("regime_position_sizing",
                                        {"BULL": 450, "NEUTRAL": 350, "BEAR": 250, "CRISIS": 0})
        position_size = regime_sizes.get(regime.label, base_position)

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

        conviction_pct = (
            100 if verdict.confidence >= 0.85 else
            80  if verdict.confidence >= 0.70 else
            60  if verdict.confidence >= 0.55 else 40
        )
        size_note = (
            f" [pos=${position_size:.0f}"
            + (f" ↓ {regime.label}" if position_size < base_position else "")
            + f" ×{conviction_pct}% conv]"
        )
        flag_note = f" ⚠️  {', '.join(verdict.risk_flags)}" if verdict.risk_flags else ""
        return True, f"APPROVED (conf={verdict.confidence:.2f}): {verdict.reasoning}{size_note}{flag_note}"

    def _load_policy(self, path):
        if os.path.exists(path):
            with open(path, 'r') as f:
                return json.load(f)
        return {}