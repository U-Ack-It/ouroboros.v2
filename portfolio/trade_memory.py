"""
Trade outcome memory — persistent Markdown log that feeds Gate 4.

append_outcome(pos, result)  called by PositionManager when a trade closes.
get_recent_outcomes(ticker)  called by validator before Gate 4.
compact_if_needed()          auto-called after each append; compresses old entries.
compact()                    force full compaction (CLI or manual use).
"""

from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

MEMORY_PATH       = Path("memory/trade_outcomes.md")
ARCHIVE_PATH      = Path("memory/trade_outcomes_archive.md")
LOG_PATH          = Path("logs/trading_decisions.log")
COMPACT_THRESHOLD = 50   # raw entries before auto-compact triggers
KEEP_RECENT_DAYS  = 14   # days of raw entries kept after compaction


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def append_outcome(pos: dict, result: dict) -> None:
    MEMORY_PATH.parent.mkdir(exist_ok=True)
    ticker    = pos.get("ticker", "?")
    outcome   = result.get("outcome", "?")
    pnl_usd   = result.get("pnl_usd", 0.0)
    pnl_pct   = result.get("pnl_pct", 0.0)
    session   = pos.get("session", "?")
    reasoning = _get_gate_reasoning(ticker)

    entry = (
        f"\n## {ticker} | {outcome} | {datetime.now().strftime('%Y-%m-%d')}\n"
        f"- **Session**: {session}"
        f" | **Entry**: ${pos.get('entry_price', 0):.4f}"
        f" | **Exit**: ${result.get('exit_price', 0):.4f}\n"
        f"- **P&L**: ${pnl_usd:+.2f} ({pnl_pct:+.2f}%)\n"
        f"- **Gate reasoning**: {reasoning}\n"
    )
    with open(MEMORY_PATH, "a") as f:
        f.write(entry)

    compact_if_needed()


def get_recent_outcomes(ticker: str, n: int = 5) -> str:
    """Return pattern note + last N raw entries for ticker, formatted for Gate 4."""
    if not MEMORY_PATH.exists():
        return ""

    blocks = _parse_blocks()
    pattern = next((b for b in blocks if b[0] == f"## [PATTERN] {ticker}"), None)
    raw     = [b for b in blocks
               if b[0].startswith(f"## {ticker} | ") and "PATTERN" not in b[0]]

    parts = []

    if pattern:
        stats_text = "\n".join(
            line for line in pattern[1:]
            if "**Stored stats**" not in line  # hide internal accounting line
        )
        parts.append(f"Historical pattern — {ticker}:\n{stats_text}")

    recent = raw[-n:]
    if recent:
        summary_lines = []
        wins = 0
        for block in recent:
            bits     = block[0].replace("## ", "").split(" | ")
            outcome  = bits[1] if len(bits) > 1 else "?"
            date_str = bits[2] if len(bits) > 2 else "?"

            pnl_line = next((l for l in block if "**P&L**" in l), "")
            pnl_text = pnl_line.split("**P&L**: ")[1] if "**P&L**: " in pnl_line else ""

            sess_line = next((l for l in block if "**Session**" in l), "")
            sess_text = (sess_line.split("**Session**: ")[1].split(" |")[0]
                         if "**Session**: " in sess_line else "?")

            reas_line = next((l for l in block if "**Gate reasoning**" in l), "")
            reas_text = (reas_line.split("**Gate reasoning**: ")[1][:80]
                         if "**Gate reasoning**: " in reas_line else "")

            summary_lines.append(
                f"  {date_str}: {outcome} {pnl_text} | {sess_text} | {reas_text}"
            )
            if outcome == "WIN":
                wins += 1

        total = len(recent)
        parts.append(
            f"Recent trades — {ticker} (last {total}):\n"
            + "\n".join(summary_lines)
            + f"\nRecent rate: {wins}W / {total - wins}L"
        )

    return "\n\n".join(parts) if parts else ""


def compact_if_needed() -> bool:
    """Trigger compaction when raw entry count exceeds COMPACT_THRESHOLD."""
    if not MEMORY_PATH.exists():
        return False
    blocks    = _parse_blocks()
    raw_count = sum(1 for b in blocks if _is_raw(b))
    if raw_count >= COMPACT_THRESHOLD:
        compact()
        return True
    return False


def compact() -> None:
    """
    Move entries older than KEEP_RECENT_DAYS to archive.
    Update per-ticker [PATTERN] notes with the compressed stats.
    Rewrite memory file: header + patterns + recent raw entries.
    """
    if not MEMORY_PATH.exists():
        return

    cutoff  = (date.today() - timedelta(days=KEEP_RECENT_DAYS)).isoformat()
    blocks  = _parse_blocks()
    header  = _read_header()

    raw_blocks     = [b for b in blocks if _is_raw(b)]
    pattern_blocks = [b for b in blocks if _is_pattern(b)]

    old_raw    = [b for b in raw_blocks if _block_date(b) < cutoff]
    recent_raw = [b for b in raw_blocks if _block_date(b) >= cutoff]

    if not old_raw:
        return

    # Archive old raw entries
    ARCHIVE_PATH.parent.mkdir(exist_ok=True)
    with open(ARCHIVE_PATH, "a") as f:
        f.write(f"\n# Archived {date.today().isoformat()} — entries before {cutoff}\n")
        for b in old_raw:
            f.write("\n" + "\n".join(b) + "\n")

    # Build updated pattern notes
    new_patterns = _build_patterns(old_raw, pattern_blocks)

    # Rewrite main file
    with open(MEMORY_PATH, "w") as f:
        f.write(header)
        for p in new_patterns:
            f.write("\n" + "\n".join(p) + "\n")
        for b in recent_raw:
            f.write("\n" + "\n".join(b) + "\n")

    print(
        f"[trade_memory] Compacted: {len(old_raw)} archived, "
        f"{len(recent_raw)} kept, {len(new_patterns)} pattern(s) updated."
    )


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _parse_blocks() -> list[list[str]]:
    if not MEMORY_PATH.exists():
        return []
    lines   = MEMORY_PATH.read_text().splitlines()
    blocks: list[list[str]] = []
    current: list[str]      = []
    for line in lines:
        if line.startswith("## "):
            if current:
                blocks.append(current)
            current = [line]
        elif current:
            current.append(line)
    if current:
        blocks.append(current)
    return blocks


def _is_raw(block: list[str]) -> bool:
    return block[0].startswith("## ") and "PATTERN" not in block[0]


def _is_pattern(block: list[str]) -> bool:
    return block[0].startswith("## [PATTERN]")


def _block_date(block: list[str]) -> str:
    parts = block[0].split(" | ")
    return parts[2].strip() if len(parts) > 2 else "0000-00-00"


def _block_ticker(block: list[str]) -> str:
    return block[0].replace("## ", "").split(" | ")[0].strip()


def _read_header() -> str:
    if not MEMORY_PATH.exists():
        return "# Trade Outcomes Memory\n\n---\n"
    text = MEMORY_PATH.read_text()
    idx  = text.find("\n## ")
    return text[:idx] + "\n" if idx != -1 else text


def _build_patterns(
    old_blocks: list[list[str]],
    existing_patterns: list[list[str]],
) -> list[list[str]]:
    # Load accumulated stats from existing pattern notes
    prev_stats: dict[str, dict] = {}
    for p in existing_patterns:
        ticker = p[0].replace("## [PATTERN] ", "").strip()
        stats: dict = {"wins": 0, "losses": 0, "win_pnl_sum": 0.0, "loss_pnl_sum": 0.0,
                       "session_wins": defaultdict(int), "session_losses": defaultdict(int)}
        for line in p[1:]:
            if "**Stored stats**" in line:
                try:
                    stats["wins"]         = int(  line.split("wins=")[1].split(" ")[0])
                    stats["losses"]       = int(  line.split("losses=")[1].split(" ")[0])
                    stats["win_pnl_sum"]  = float(line.split("win_pnl_sum=")[1].split(" ")[0])
                    stats["loss_pnl_sum"] = float(line.split("loss_pnl_sum=")[1].split(" ")[0])
                except Exception:
                    pass
            if "**Sessions**" in line:
                # "NY: 6W/1L | London: 2W/2L"
                for seg in line.split("**Sessions**: ")[-1].split(" | "):
                    try:
                        sess, rest = seg.split(": ")
                        sw, sl = rest.split("W/")
                        sl = sl.rstrip("L").strip()
                        stats["session_wins"][sess.strip()]   += int(sw)
                        stats["session_losses"][sess.strip()] += int(sl)
                    except Exception:
                        pass
        prev_stats[ticker] = stats

    # Accumulate stats from new old_blocks
    by_ticker: dict[str, list] = defaultdict(list)
    for b in old_blocks:
        by_ticker[_block_ticker(b)].append(b)

    all_tickers = sorted(set(list(by_ticker.keys()) + list(prev_stats.keys())))

    return [
        _build_one_pattern(ticker, by_ticker.get(ticker, []), prev_stats.get(ticker, {}))
        for ticker in all_tickers
    ]


def _build_one_pattern(
    ticker: str,
    new_blocks: list[list[str]],
    prev: dict,
) -> list[str]:
    wins         = prev.get("wins", 0)
    losses       = prev.get("losses", 0)
    win_pnl_sum  = prev.get("win_pnl_sum", 0.0)
    loss_pnl_sum = prev.get("loss_pnl_sum", 0.0)
    sess_w: dict[str, int] = defaultdict(int, prev.get("session_wins", {}))
    sess_l: dict[str, int] = defaultdict(int, prev.get("session_losses", {}))

    for block in new_blocks:
        bits    = block[0].split(" | ")
        outcome = bits[1].strip() if len(bits) > 1 else "?"

        pnl_line = next((l for l in block if "**P&L**" in l), "")
        pnl_pct  = 0.0
        if "(" in pnl_line and "%" in pnl_line:
            try:
                pnl_pct = float(pnl_line.split("(")[1].split("%")[0].replace("+", ""))
            except Exception:
                pass

        sess_line = next((l for l in block if "**Session**" in l), "")
        session   = "?"
        if "**Session**: " in sess_line:
            session = sess_line.split("**Session**: ")[1].split(" |")[0].strip()

        if outcome == "WIN":
            wins += 1
            win_pnl_sum += pnl_pct
            sess_w[session] += 1
        else:
            losses += 1
            loss_pnl_sum += pnl_pct
            sess_l[session] += 1

    total    = wins + losses
    win_rate = f"{wins}W / {losses}L ({wins/total*100:.1f}%)" if total else "0W / 0L"
    avg_win  = f"{win_pnl_sum / wins:+.2f}%"   if wins   else "n/a"
    avg_loss = f"{loss_pnl_sum / losses:+.2f}%" if losses else "n/a"

    all_sessions = sorted(set(list(sess_w.keys()) + list(sess_l.keys())))
    session_str  = " | ".join(f"{s}: {sess_w[s]}W/{sess_l[s]}L" for s in all_sessions) \
                   or "no session data"

    return [
        f"## [PATTERN] {ticker}",
        f"- **Win rate**: {win_rate} overall",
        f"- **Sessions**: {session_str}",
        f"- **Avg P&L**: wins: {avg_win} | losses: {avg_loss}",
        f"- **Last updated**: {date.today().isoformat()}",
        # Internal line for accumulation across compaction cycles — hidden from Gate 4 prompt
        f"- **Stored stats**: wins={wins} losses={losses}"
        f" win_pnl_sum={win_pnl_sum:.4f} loss_pnl_sum={loss_pnl_sum:.4f}",
    ]


def _get_gate_reasoning(ticker: str) -> str:
    if not LOG_PATH.exists():
        return "no log"
    try:
        lines = LOG_PATH.read_text().splitlines()
        for line in reversed(lines):
            parts = [p.strip() for p in line.split(" | ")]
            if len(parts) < 3:
                continue
            if parts[1] != ticker:
                continue
            if parts[2] not in ("PASS", "CAUTION"):
                continue
            if "LLM: " in line:
                return line[line.index("LLM: ") + 5:][:120]
            return parts[-1][:120]
    except Exception:
        pass
    return "no reasoning found"
