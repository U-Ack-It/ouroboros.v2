"""
Clean log formatter for heartbeat output.
Import and use instead of raw print statements.
"""

from datetime import datetime

# Box-drawing characters for clean output
HEAVY = "═"
LIGHT = "─"
WIDTH = 60

def session_header(time_str: str, session_name: str):
    label = f" [{time_str}] {session_name} "
    pad = WIDTH - len(label)
    left = 3
    right = pad - left
    print(f"\n{HEAVY*left}{label}{HEAVY*right}")

def regime_line(regime_data: dict):
    if not regime_data:
        print(f"REGIME: unknown (no snapshot)")
        return
    label = regime_data.get("label", "?")
    vix = regime_data.get("vix", "?")
    spy = regime_data.get("spy_price", "?")
    ma = regime_data.get("spy_ma200", "?")
    score = regime_data.get("score", "?")
    above = regime_data.get("above_ma200", False)
    trend = "↑" if above else "↓"
    print(f"REGIME: {label}  VIX={vix}  SPY=${spy} ({trend} 200MA=${ma})  score={score}")

def scan_summary(total: int, elapsed: float, fvg_count: int):
    print(f"SCAN:   {total} assets | {elapsed:.1f}s | {fvg_count} FVG detected")
    print(LIGHT * WIDTH)

def approved_line(ticker: str, fvg_type: str, fvg_pct: float, conf: float, result_msg: str):
    pct_str = f"{fvg_pct:.2%}" if fvg_pct else ""
    print(f"  ✅ {ticker:<8} {fvg_type:<10} {pct_str:<8} conf={conf:.2f}  → {result_msg}")

def rejected_fvg_line(ticker: str, fvg_type: str, fvg_pct: float, min_fvg: float, regime: str):
    print(f"  ⚠️  {ticker:<8} {fvg_type:<10} {fvg_pct:.4%}  rejected (below {min_fvg:.4%} {regime} threshold)")

def error_line(ticker: str, error: str):
    print(f"  ❗ {ticker:<8} SCAN ERR: {error[:60]}")

def no_fvg_summary(count: int):
    if count > 0:
        print(f"  {LIGHT*2} {count} others: no FVG {LIGHT*2}")

def execution_result(ticker: str, icon: str, message: str):
    print(f"     {icon} {message}")

def scan_footer():
    print(LIGHT * WIDTH)

def zombie_line(time_str: str, next_session: str = ""):
    # Only print every hour instead of every 15 min
    minute = int(time_str.split(":")[1])
    if minute == 0 or minute == 1:
        extra = f" | next: {next_session}" if next_session else ""
        print(f"💤 [{time_str}] Sleeping{extra}")

def session_end(session_label: str, wins: int = 0, losses: int = 0,
                net: float = 0.0, detail: str = ""):
    print(f"\n{'═'*WIDTH}")
    if wins == 0 and losses == 0:
        print(f"  [{session_label}] Session ended — no closed trades.")
    else:
        print(f"  [{session_label}] CLOSED  {wins}W / {losses}L  net ${net:+.2f}{detail}")
    print(f"{'═'*WIDTH}\n")

def get_next_session(hour: int) -> str:
    if hour < 3:
        return "London/Global @ 03:00"
    elif hour < 8:
        return "NY Power Hour @ 08:00"
    elif hour < 20:
        return "Asia/Neutral @ 20:00"
    else:
        return "London/Global @ 03:00"
