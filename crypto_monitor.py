"""
Ouroboros v2 — Crypto/DeFi Monitor

Parallel SMC FVG scanner for crypto assets via Binance public API.
Runs the same 3-gate validation pipeline as the equity scanner
(Ethics → Sentiment → FVG → LLM Gate 4) with crypto-adjusted risk params.

Usage:
    python crypto_monitor.py                    # one-shot scan
    python crypto_monitor.py --watch            # live loop (Ctrl-C to stop)
    python crypto_monitor.py --interval 4h      # change OHLC timeframe
    python crypto_monitor.py --symbols BTC ETH  # subset (short names)
    python crypto_monitor.py --min-gap 0.2      # tighter noise filter (%)
"""

import asyncio
import json
import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.getcwd())

from src.crypto.feed import binance_available, get_last_price
from src.crypto.scanner import scan_symbol, crypto_session, CryptoFVG
from src.llm_agent.agent import CryptoQuantAgent


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

CRYPTO_CONFIG_PATH = "config/crypto_config.json"


def load_config() -> dict:
    with open(CRYPTO_CONFIG_PATH) as f:
        return json.load(f)


def build_symbol_map(config: dict) -> dict[str, dict]:
    """Returns {BTCUSDT: {name, category}, ...}"""
    return {s["binance"]: s for s in config["symbols"]}


# ---------------------------------------------------------------------------
# Async parallel scan
# ---------------------------------------------------------------------------

_EXECUTOR = ThreadPoolExecutor(max_workers=16, thread_name_prefix="crypto_scan")


def _scan_one(symbol: str, symbol_info: dict, interval: str, min_gap_pct: float) -> dict:
    import time as _time
    t0 = _time.monotonic()
    try:
        fvg = scan_symbol(symbol, interval=interval, min_gap_pct=min_gap_pct)
        return {
            "symbol":   symbol,
            "name":     symbol_info.get("name", symbol),
            "category": symbol_info.get("category", "—"),
            "fvg":      fvg,
            "error":    None,
            "ms":       round((_time.monotonic() - t0) * 1000, 1),
        }
    except Exception as exc:
        return {
            "symbol":   symbol,
            "name":     symbol_info.get("name", symbol),
            "category": symbol_info.get("category", "—"),
            "fvg":      None,
            "error":    str(exc),
            "ms":       round((_time.monotonic() - t0) * 1000, 1),
        }


async def scan_all_async(
    symbol_map: dict[str, dict],
    interval: str,
    min_gap_pct: float,
    timeout: float = 20.0,
) -> list[dict]:
    loop = asyncio.get_event_loop()
    coros = [
        loop.run_in_executor(_EXECUTOR, _scan_one, sym, info, interval, min_gap_pct)
        for sym, info in symbol_map.items()
    ]
    results = await asyncio.wait_for(asyncio.gather(*coros), timeout=timeout)
    return list(results)


# ---------------------------------------------------------------------------
# Validation pipeline
# ---------------------------------------------------------------------------

def validate_crypto_signal(
    result: dict,
    agent: CryptoQuantAgent,
    config: dict,
    session: str,
) -> tuple[bool, str]:
    """Run FVG through crypto Gate 4 (LLM only — no equity gates for crypto assets)."""
    fvg: CryptoFVG = result["fvg"]
    risk = config["risk"]

    verdict = agent.analyze_crypto_trade(
        symbol=result["symbol"],
        category=result["category"],
        fvg_type=fvg.fvg_type,
        direction=fvg.direction,
        gap_size=fvg.gap_size,
        gap_pct=fvg.gap_pct,
        entry_price=fvg.entry_price,
        last_price=fvg.last_price,
        session=session,
        position_size_usd=risk["position_size_usd"],
        sl_pct=risk["stop_loss_pct"],
        tp_pct=risk["take_profit_pct"],
    )

    if verdict.is_blocked():
        return False, f"REJECTED (Gate 4): {verdict.reasoning}"

    flag_note = f" ⚠️  {', '.join(verdict.risk_flags)}" if verdict.risk_flags else ""
    status = "CAUTION" if verdict.is_flagged() else "APPROVED"
    return True, f"{status} (conf={verdict.confidence:.2f}): {verdict.reasoning}{flag_note}"


# ---------------------------------------------------------------------------
# Report printer
# ---------------------------------------------------------------------------

def print_scan(results: list[dict], elapsed: float, interval: str, session: str):
    sep  = "=" * 72
    dash = "-" * 72
    ts   = datetime.now().strftime("%H:%M:%S")

    fvg_hits = [r for r in results if r["fvg"] is not None]
    errors   = [r for r in results if r["error"]]
    avg_ms   = round(sum(r["ms"] for r in results) / len(results), 0) if results else 0

    print(f"\n{sep}")
    print(f"  OUROBOROS v2 — CRYPTO SCANNER  [{ts}]  session={session.upper()}")
    print(f"  Interval: {interval}  |  {elapsed:.2f}s wall  |  avg {avg_ms:.0f}ms/symbol")
    print(f"{sep}")
    print(f"  {'Symbol':<12} {'Name':<12} {'Cat':<6} {'FVG':<10} {'Gap%':>6} {'Price':>12}  {'ms':>5}")
    print(f"  {'-'*12} {'-'*12} {'-'*6} {'-'*10} {'------':>6} {'------------':>12}  {'-----':>5}")

    for r in sorted(results, key=lambda x: (x["fvg"] is None, x["symbol"])):
        if r["error"]:
            print(f"  {r['symbol']:<12} {r['name']:<12} {r['category']:<6} {'ERROR':<10} {'—':>6} {'—':>12}  {r['ms']:>5.0f}  ⚠️")
            continue
        fvg: CryptoFVG = r["fvg"]
        if fvg:
            tag  = fvg.fvg_type
            gp   = f"{fvg.gap_pct:.3f}%"
            pr   = f"${fvg.last_price:,.4f}"
            icon = "🔥"
        else:
            tag  = "—"
            gp   = "—"
            pr   = "—"
            icon = "  "
        print(f"  {r['symbol']:<12} {r['name']:<12} {r['category']:<6} {tag:<10} {gp:>6} {pr:>12}  {r['ms']:>5.0f}  {icon}")

    print(f"{dash}")
    ok_count = len(results) - len(errors)
    print(f"  {ok_count}/{len(results)} ok | {len(fvg_hits)} FVG signal(s) | {len(errors)} error(s)")
    if fvg_hits:
        print(f"\n  SIGNALS:")
        for r in fvg_hits:
            fvg = r["fvg"]
            tp  = round(fvg.entry_price * (1 + 0.04 if fvg.direction == "LONG" else 1 - 0.04), 4)
            sl  = round(fvg.entry_price * (1 - 0.02 if fvg.direction == "LONG" else 1 + 0.02), 4)
            print(
                f"    {'🟢' if fvg.direction == 'LONG' else '🔴'} {r['symbol']:<10}"
                f"  {fvg.direction:<5}  entry≈${fvg.entry_price:,.4f}"
                f"  TP=${tp:,.4f}  SL=${sl:,.4f}  gap={fvg.gap_pct:.3f}%"
            )
    print(f"{sep}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

# Short-name aliases → Binance symbol
_ALIASES = {
    "BTC": "BTCUSDT", "ETH": "ETHUSDT", "SOL": "SOLUSDT",
    "BNB": "BNBUSDT", "ADA": "ADAUSDT", "AVAX": "AVAXUSDT",
    "LINK": "LINKUSDT", "AAVE": "AAVUSDT", "UNI": "UNIUSDT",
    "DOT": "DOTUSDT",
}


def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 Crypto Monitor")
    p.add_argument("--symbols", nargs="+", help="Short names (BTC ETH SOL) or Binance symbols")
    p.add_argument("--interval", default="1h", help="OHLC interval (default 1h)")
    p.add_argument("--min-gap", type=float, default=0.0, dest="min_gap",
                   help="Minimum FVG gap %% (default 0 = no filter)")
    p.add_argument("--watch", action="store_true", help="Continuous loop (Ctrl-C to stop)")
    p.add_argument("--cycle", type=int, default=300, help="Seconds between cycles in --watch (default 300)")
    p.add_argument("--no-validate", action="store_true", help="Skip 4-gate validation, show raw FVGs only")
    return p.parse_args()


async def _run(symbol_map, interval, min_gap, agent, config, validate):
    session = crypto_session()
    t0 = time.monotonic()
    results = await scan_all_async(symbol_map, interval, min_gap)
    elapsed = time.monotonic() - t0
    print_scan(results, elapsed, interval, session)

    if validate and agent is not None:
        fvg_hits = [r for r in results if r["fvg"] is not None]
        if fvg_hits:
            print(f"  GATE 4 VALIDATION ({len(fvg_hits)} signal(s)):")
            for r in fvg_hits:
                ok, msg = validate_crypto_signal(r, agent, config, session)
                icon = "✅" if ok else "🚫"
                print(f"  {icon} {r['symbol']:<12} {msg}")
            print()


if __name__ == "__main__":
    args = parse_args()

    # Connectivity check
    if not binance_available():
        print("⚠️  Binance API unreachable — will use yfinance fallback")

    config = load_config()
    symbol_map = build_symbol_map(config)

    if args.symbols:
        resolved = {_ALIASES.get(s.upper(), s.upper()): symbol_map.get(
            _ALIASES.get(s.upper(), s.upper()),
            {"name": s, "category": "—"},
        ) for s in args.symbols}
        symbol_map = resolved

    agent = None if args.no_validate else CryptoQuantAgent()

    if args.watch:
        print(f"Starting crypto monitor — {len(symbol_map)} symbols every {args.cycle}s  (Ctrl-C to stop)")
        try:
            while True:
                asyncio.run(_run(symbol_map, args.interval, args.min_gap, agent, config, not args.no_validate))
                time.sleep(args.cycle)
        except KeyboardInterrupt:
            print("\nStopped.")
    else:
        asyncio.run(_run(symbol_map, args.interval, args.min_gap, agent, config, not args.no_validate))
