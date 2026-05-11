"""
Trade outcome memory — persistent Markdown log that feeds Gate 4.

append_outcome(pos, result) — called by PositionManager when a trade closes.
get_recent_outcomes(ticker)  — called by validator before Gate 4.
"""

from datetime import datetime
from pathlib import Path

MEMORY_PATH = Path("memory/trade_outcomes.md")
LOG_PATH    = Path("logs/trading_decisions.log")


def append_outcome(pos: dict, result: dict) -> None:
    MEMORY_PATH.parent.mkdir(exist_ok=True)
    ticker   = pos.get("ticker", "?")
    outcome  = result.get("outcome", "?")
    pnl_usd  = result.get("pnl_usd", 0.0)
    pnl_pct  = result.get("pnl_pct", 0.0)
    session  = pos.get("session", "?")
    reasoning = _get_gate_reasoning(ticker)

    entry = (
        f"\n## {ticker} | {outcome} | {datetime.now().strftime('%Y-%m-%d')}\n"
        f"- **Session**: {session} | **Entry**: ${pos.get('entry_price', 0):.4f}"
        f" | **Exit**: ${result.get('exit_price', 0):.4f}\n"
        f"- **P&L**: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)\n"
        f"- **Gate reasoning**: {reasoning}\n"
    )
    with open(MEMORY_PATH, "a") as f:
        f.write(entry)


def get_recent_outcomes(ticker: str, n: int = 5) -> str:
    """Return a formatted summary of the last N closed trades for ticker."""
    if not MEMORY_PATH.exists():
        return ""

    lines  = MEMORY_PATH.read_text().splitlines()
    blocks: list[list[str]] = []
    current: list[str] = []

    for line in lines:
        if line.startswith("## "):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)

    ticker_blocks = [b for b in blocks if b[0].startswith(f"## {ticker} |")]
    recent = ticker_blocks[-n:]

    if not recent:
        return ""

    summary_lines = []
    wins = 0
    for block in recent:
        header = block[0]                          # ## GLD | WIN | 2026-05-11
        parts  = header.replace("## ", "").split(" | ")
        outcome  = parts[1] if len(parts) > 1 else "?"
        date_str = parts[2] if len(parts) > 2 else "?"

        pnl_line = next((l for l in block if "P&L" in l), "")
        pnl_text = pnl_line.split("**P&L**: ")[1] if "**P&L**: " in pnl_line else ""

        session_line = next((l for l in block if "Session" in l), "")
        session_text = session_line.split("**Session**: ")[1].split(" |")[0] if "**Session**: " in session_line else "?"

        reasoning_line = next((l for l in block if "Gate reasoning" in l), "")
        reasoning_text = reasoning_line.split("**Gate reasoning**: ")[1][:80] if "**Gate reasoning**: " in reasoning_line else ""

        summary_lines.append(
            f"  {date_str}: {outcome} {pnl_text} | {session_text} | {reasoning_text}"
        )
        if outcome == "WIN":
            wins += 1

    total = len(recent)
    return (
        f"Recent Trade History — {ticker} (last {total}):\n"
        + "\n".join(summary_lines)
        + f"\nOutcome rate: {wins}W / {total - wins}L on {ticker}"
    )


def _get_gate_reasoning(ticker: str) -> str:
    """Scan trading_decisions.log backwards for the last approved Gate 4 on this ticker."""
    if not LOG_PATH.exists():
        return "no log"
    try:
        lines = LOG_PATH.read_text().splitlines()
        for line in reversed(lines):
            parts = [p.strip() for p in line.split(" | ")]
            if len(parts) < 2:
                continue
            if parts[1] != ticker:
                continue
            status = parts[2] if len(parts) > 2 else ""
            if status not in ("PASS", "CAUTION"):
                continue
            # LLM reasoning is after "LLM: " if present
            if "LLM: " in line:
                idx = line.index("LLM: ")
                return line[idx + 5:][:120]
            # Fallback: last pipe segment
            return parts[-1][:120]
    except Exception:
        pass
    return "no reasoning found"
