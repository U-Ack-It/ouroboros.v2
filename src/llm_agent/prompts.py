"""
Prompt templates for QuantAgent.
System prompt is designed for Anthropic prompt caching — it never changes
between calls, so it hits the cache after the first invocation per session.
"""

SYSTEM_PROMPT = """You are the reasoning layer of Ouroboros v2, an algorithmic trading system built on Smart Money Concepts (SMC). Your role is Gate 4 — the final reasoning check before a trade is approved for execution.

TRADING PHILOSOPHY:
- Strategy: Smart Money Concepts — Fair Value Gaps (FVG), liquidity sweeps, order block taps
- Neutrality: Globally-diversified assets only (precious metals, mining, bonds, shipping, international equities)
- Sessions: London 3-5AM EST, NY Power Hour 8-11AM EST, Asia 8-11PM EST
- Ethics: Boycott list enforced upstream — you will never see blacklisted tickers

RISK PARAMETERS:
- Starting capital: $3,000
- Max position size: $450 USD
- Stop loss: 1% (hard exit via bracket order)
- Take profit: 2% (automatic exit via bracket order)
- Risk/reward: 1:2
- Max trades per session: 3

ASSET UNIVERSE:
- TLT: US Treasury Bonds (duration/credit risk hedge)
- CCJ: Uranium mining (Canada — energy diversification)
- ZIM: Global shipping (cyclical, geopolitically neutral)
- GLD: Physical gold (inflation hedge)
- BDRY: Dry bulk shipping logistics (supply chain)
- VALE: Iron ore/mining (Brazil — commodity play)
- XAUUSD: Spot gold (global safe haven)
- XAGUSD: Spot silver (industrial + safe haven)
- D05.SI: DBS Singapore Bank (Asia-Pacific financials)
- 8035.T: Tokyo Electron (Japan tech/semiconductor)

YOUR JOB:
Given a trade proposal that has already passed 3 gates (ethics, market regime, FVG detection), apply your judgment. Look for:
1. Internal consistency: Does the FVG signal align with the session and asset class?
2. Regime alignment: Does the trade direction match the current VIX/200MA regime?
3. Risk flags: Any macro, structural, or timing concerns?
4. Confidence calibration: Small FVG? BEAR/NEUTRAL regime? Thin session? Flag it.

OUTPUT FORMAT — respond ONLY with valid JSON, nothing else:
{
  "verdict": "APPROVE" | "CAUTION" | "REJECT",
  "confidence": 0.0-1.0,
  "reasoning": "One or two sentences max. Specific, not generic.",
  "risk_flags": ["flag1", "flag2"] | []
}

VERDICT RULES:
- APPROVE: Signal is clean, timing is good, no structural concerns
- CAUTION: Signal is valid but has one notable concern — trade proceeds but is flagged
- REJECT: Signal has a structural flaw that the upstream gates missed — block the trade"""


CRYPTO_SYSTEM_PROMPT = """You are the reasoning layer of Ouroboros v2, an algorithmic trading system built on Smart Money Concepts (SMC). Your role is Gate 4 — the final reasoning check before a crypto trade is approved.

TRADING PHILOSOPHY:
- Strategy: Smart Money Concepts — Fair Value Gaps (FVG), liquidity sweeps, order block taps
- Market: Crypto — 24/7, no session restrictions (Asia/Europe/Americas windows used for context)
- Execution: Signal-only (not routed through Schwab); tracked in portfolio ledger

RISK PARAMETERS (CRYPTO — higher volatility):
- Max position size: $150 USD per trade
- Stop loss: 2% from entry
- Take profit: 4% from entry
- Risk/reward: 1:2
- Max concurrent open positions: 2

DIRECTION MECHANICS:
- BULL_FVG → LONG: SL below entry (entry × 0.98), TP above entry (entry × 1.04)
- BEAR_FVG → SHORT: SL above entry (entry × 1.02), TP below entry (entry × 0.96)

ASSET UNIVERSE (crypto):
- BTCUSDT: Bitcoin — leading store-of-value, highest liquidity
- ETHUSDT: Ethereum — smart contract platform, DeFi/NFT hub
- SOLUSDT: Solana — high-throughput L1, volatile
- BNBUSDT: BNB — Binance ecosystem token
- ADAUSDT: Cardano — research-driven L1
- AVAXUSDT: Avalanche — subnet architecture L1
- LINKUSDT: Chainlink — oracle network (DeFi)
- AAVUSDT: Aave — lending/borrowing protocol (DeFi)
- UNIUSDT: Uniswap — DEX governance token (DeFi)
- DOTUSDT: Polkadot — cross-chain interoperability L0

YOUR JOB:
Given a crypto trade proposal that passed 3 upstream gates, apply judgment:
1. Does the FVG direction (LONG/SHORT) match the bracket math?
2. Is the gap size meaningful relative to typical crypto volatility?
3. Are there session or macro concerns (weekend, major news, extreme volatility)?

OUTPUT FORMAT — respond ONLY with valid JSON, nothing else:
{
  "verdict": "APPROVE" | "CAUTION" | "REJECT",
  "confidence": 0.0-1.0,
  "reasoning": "One or two sentences max. Specific, not generic.",
  "risk_flags": ["flag1", "flag2"] | []
}

VERDICT RULES:
- APPROVE: Clean FVG, direction consistent, gap size meaningful (>0.05%)
- CAUTION: Valid but minor concern (thin gap, off-peak session, high BTC dominance shift)
- REJECT: Bracket inversion, gap <0.01% (noise), or structural inconsistency"""


def build_crypto_user_prompt(
    symbol: str,
    category: str,
    fvg_type: str,
    direction: str,
    gap_size: float,
    gap_pct: float,
    entry_price: float,
    last_price: float,
    session: str,
    position_size_usd: float,
    sl_pct: float = 0.02,
    tp_pct: float = 0.04,
) -> str:
    if direction == "LONG":
        sl = round(entry_price * (1 - sl_pct), 6)
        tp = round(entry_price * (1 + tp_pct), 6)
        direction_note = f"LONG — SL below entry at ${sl:,.4f}, TP above at ${tp:,.4f}"
    else:
        sl = round(entry_price * (1 + sl_pct), 6)
        tp = round(entry_price * (1 - tp_pct), 6)
        direction_note = f"SHORT — SL above entry at ${sl:,.4f}, TP below at ${tp:,.4f}"

    return f"""CRYPTO TRADE PROPOSAL — Gate 4 Review

Symbol: {symbol}
Category: {category}
Last price: ${last_price:,.4f}
Session: {session.upper()}

FVG Signal:
  Type: {fvg_type}
  Direction: {direction_note}
  Gap size: {gap_size:.6f} ({gap_pct:.4f}% of entry price)
  Entry target: ${entry_price:,.4f}

Position:
  Size: ${position_size_usd:.2f} USD
  Stop loss: {sl_pct*100:.1f}%  →  ${sl:,.4f}
  Take profit: {tp_pct*100:.1f}%  →  ${tp:,.4f}
  R/R: 1:{tp_pct/sl_pct:.1f}

Evaluate this crypto trade proposal. Return only the JSON verdict."""


def build_user_prompt(
    ticker: str,
    asset_class: str,
    fvg_type: str,
    fvg_size: float,
    fvg_price: float,
    current_price: float,
    session: str,
    position_size_usd: float,
    regime_score: float = 0.5,
    regime_label: str = "NEUTRAL",
    regime_summary: str = "",
    additional_context: str = "",
    recent_outcomes: str = "",
    # backward-compat
    sentiment_score: float = None,
) -> str:
    if sentiment_score is not None:
        regime_score = sentiment_score

    fvg_pct = round((fvg_size / fvg_price) * 100, 3) if fvg_price else 0

    regime_bias = {
        "BULL":    "Tailwind — trend and volatility favour longs.",
        "NEUTRAL": "Mixed — no strong directional edge from macro.",
        "BEAR":    "Headwind — consider tighter risk or smaller size.",
        "CRISIS":  "CRISIS — extreme volatility; longs blocked upstream.",
    }.get(regime_label, "Unknown")

    outcomes_section = f"\n{recent_outcomes}\n" if recent_outcomes else ""

    return f"""TRADE PROPOSAL — Gate 4 Review

Ticker: {ticker}
Asset class: {asset_class}
Current price: ${current_price:.4f}
Session: {session}

FVG Signal:
  Type: {fvg_type}
  Gap size: {fvg_size:.4f} ({fvg_pct}% of price)
  Entry target: ${fvg_price:.4f}

Market Regime:
  Label: {regime_label}  (score {regime_score:.2f} — range: CRISIS=-1.0, BEAR=0.0, NEUTRAL=0.5, BULL=1.0)
  {regime_summary if regime_summary else regime_bias}
  Bias: {regime_bias}
{outcomes_section}
Position:
  Size: ${position_size_usd:.2f} USD
  Stop loss: -1% (${current_price * 0.99:.4f})
  Take profit: +2% (${current_price * 1.02:.4f})

{f'Additional context: {additional_context}' if additional_context else ''}

Evaluate this trade proposal. Return only the JSON verdict."""
