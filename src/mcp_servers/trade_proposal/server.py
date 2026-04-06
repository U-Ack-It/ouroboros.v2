"""
Trade Proposal MCP Server
Receives trade intentions from the LLM, but delegates to Gatekeeper.
"""

def propose_trade(asset: str, amount_usd: float, action: str) -> dict:
    """
    This tool is exposed to the LLM. It generates a signal.
    It DOES NOT execute the trade directly.
    """
    signal = {
        "asset": asset,
        "amount_usd": amount_usd,
        "action": action
    }
    print(f"Received trade proposal from LLM: {signal}")
    return {"status": "proposed", "signal": signal}
