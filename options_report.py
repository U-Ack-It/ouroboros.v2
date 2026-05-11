"""
Ouroboros v2 — Options Report

Screens the equity watchlist for covered call and protective put opportunities.
Uses real options chain data from yfinance + Black-Scholes for delta/IV.

Usage:
    python options_report.py                         # screen full watchlist
    python options_report.py --ticker GLD CCJ VALE   # specific tickers
    python options_report.py --strategy calls        # covered calls only
    python options_report.py --strategy puts         # protective puts only
    python options_report.py --dte 30                # target 30 DTE
    python options_report.py --delta 0.25            # target 25-delta strikes
    python options_report.py --min-yield 10          # filter calls by ann. yield
    python options_report.py --max-cost 2.0          # filter puts by cost %
"""

import argparse
import json
import os
import sys
from datetime import datetime

sys.path.insert(0, os.getcwd())

from src.options.strategies import (
    screen_covered_calls, screen_protective_puts,
    build_covered_call, build_protective_put,
    CoveredCallIdea, ProtectivePutIdea,
)
from src.options.pricing import greeks, bs_call, bs_put


# ---------------------------------------------------------------------------
# Printer helpers
# ---------------------------------------------------------------------------

SEP  = "=" * 72
DASH = "-" * 72


def print_covered_calls(ideas: list[CoveredCallIdea]):
    print(f"\nCOVERED CALLS  (sell OTM call, collect premium)")
    print(DASH)
    if not ideas:
        print("  No qualifying ideas found.")
        return

    print(f"  {'Ticker':<7} {'Price':>8} {'Strike':>8} {'Expiry':<12} {'DTE':>4} "
          f"{'Delta':>7} {'IV':>7} {'Mid':>7} {'Yield%':>7} {'Ann%':>7} {'MaxP%':>7} {'B/E':>8}")
    print(f"  {'-'*7} {'--------':>8} {'--------':>8} {'------------':<12} {'----':>4} "
          f"{'-------':>7} {'-------':>7} {'-------':>7} {'-------':>7} {'-------':>7} {'-------':>7} {'--------':>8}")

    for idea in ideas:
        print(
            f"  {idea.ticker:<7} {idea.underlying_price:>8.2f} {idea.strike:>8.2f} "
            f"{idea.expiry:<12} {idea.dte:>4} "
            f"{idea.bs_delta:>7.3f} {idea.bs_iv*100:>6.1f}% "
            f"{idea.mid:>7.3f} {idea.premium_yield_pct:>6.2f}% "
            f"{idea.annualised_yield_pct:>6.1f}% {idea.max_profit_pct:>6.2f}% "
            f"{idea.breakeven:>8.3f}"
        )

    print()
    print(f"  BEST IDEA: {ideas[0].ticker} — Sell {ideas[0].expiry} ${ideas[0].strike:.0f}c")
    print(f"    Collect ${ideas[0].premium_received:.2f} premium per contract ({ideas[0].premium_yield_pct:.2f}%)")
    print(f"    Annualised yield: {ideas[0].annualised_yield_pct:.1f}%  |  Max profit: {ideas[0].max_profit_pct:.2f}%")
    print(f"    Breakeven: ${ideas[0].breakeven:.3f}  |  Called away if > ${ideas[0].called_away_price:.2f}")
    print(f"    Delta {ideas[0].bs_delta:.3f} → {abs(ideas[0].bs_delta)*100:.0f}% chance of being called away")


def print_protective_puts(ideas: list[ProtectivePutIdea]):
    print(f"\nPROTECTIVE PUTS  (buy OTM put, insure downside)")
    print(DASH)
    if not ideas:
        print("  No qualifying ideas found.")
        return

    print(f"  {'Ticker':<7} {'Price':>8} {'Strike':>8} {'Expiry':<12} {'DTE':>4} "
          f"{'Delta':>7} {'IV':>7} {'Mid':>7} {'Cost%':>7} {'MaxL%':>7} {'B/E↑':>8} {'Protect<':>10}")
    print(f"  {'-'*7} {'--------':>8} {'--------':>8} {'------------':<12} {'----':>4} "
          f"{'-------':>7} {'-------':>7} {'-------':>7} {'-------':>7} {'-------':>7} {'--------':>8} {'----------':>10}")

    for idea in ideas:
        print(
            f"  {idea.ticker:<7} {idea.underlying_price:>8.2f} {idea.strike:>8.2f} "
            f"{idea.expiry:<12} {idea.dte:>4} "
            f"{idea.bs_delta:>7.3f} {idea.bs_iv*100:>6.1f}% "
            f"{idea.mid:>7.3f} {idea.cost_pct:>6.2f}% "
            f"{idea.max_loss_pct:>6.2f}% {idea.breakeven_up:>8.3f} "
            f"${idea.protected_below:>9.2f}"
        )

    print()
    print(f"  BEST IDEA: {ideas[0].ticker} — Buy {ideas[0].expiry} ${ideas[0].strike:.0f}p")
    print(f"    Cost: ${ideas[0].cost:.2f} per contract ({ideas[0].cost_pct:.2f}% of position)")
    print(f"    Protected below: ${ideas[0].protected_below:.2f}")
    print(f"    Max loss capped at: ${ideas[0].max_loss:.2f} ({ideas[0].max_loss_pct:.2f}%)")
    print(f"    Stock needs to reach ${ideas[0].breakeven_up:.3f} to recover put cost")
    print(f"    Delta {ideas[0].bs_delta:.3f} → {abs(ideas[0].bs_delta)*100:.0f}% chance put finishes ITM")


def print_bs_summary(ideas_calls, ideas_puts):
    """Quick Black-Scholes sanity table — theoretical vs market mid for the best idea from each."""
    from src.options.chain import _dte, RISK_FREE_RATE
    from src.options.pricing import implied_vol

    print(f"\nBLACK-SCHOLES VALIDATION")
    print(DASH)
    print(f"  {'Ticker':<7} {'Type':<5} {'Strike':>8} {'S':>8} {'Market':>8} {'BS Theo':>8} {'IV':>7} {'Delta':>7} {'Theta/d':>9} {'Vega/1%':>9}")
    print(f"  {'-'*7} {'-----':<5} {'--------':>8} {'--------':>8} {'--------':>8} {'--------':>8} {'-------':>7} {'-------':>7} {'--------':>9} {'--------':>9}")

    for flag, idea in [("call", ideas_calls[0] if ideas_calls else None),
                       ("put",  ideas_puts[0]  if ideas_puts  else None)]:
        if not idea:
            continue
        S   = idea.underlying_price
        K   = idea.strike
        T   = idea.dte / 365.0
        iv  = idea.bs_iv if idea.bs_iv > 0 else 0.25
        try:
            theo = round(bs_call(S, K, T, RISK_FREE_RATE, iv) if flag == "call"
                         else bs_put(S, K, T, RISK_FREE_RATE, iv), 4)
            g    = greeks(S, K, T, RISK_FREE_RATE, iv, flag=flag)
            print(
                f"  {idea.ticker:<7} {flag:<5} {K:>8.2f} {S:>8.2f} "
                f"{idea.mid:>8.4f} {theo:>8.4f} {iv*100:>6.1f}% "
                f"{g['delta']:>7.4f} {g['theta']:>9.5f} {g['vega']:>9.5f}"
            )
        except Exception as e:
            print(f"  {idea.ticker:<7} {flag:<5} {K:>8.2f}  BS error: {e}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def load_watchlist() -> list[str]:
    try:
        with open("config/risk_policy.json") as f:
            policy = json.load(f)
        return list(policy.get("neutrality_priority", {}).get("asset_mapping", {}).keys())
    except Exception:
        return ["TLT", "GLD", "CCJ", "ZIM", "VALE", "BDRY"]


def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 Options Report")
    p.add_argument("--ticker", nargs="+", help="Specific tickers to screen")
    p.add_argument("--strategy", choices=["calls", "puts", "both"], default="both")
    p.add_argument("--dte", type=int, default=45, help="Target days to expiry (default 45)")
    p.add_argument("--delta", type=float, default=0.30, help="Target delta for strike selection (default 0.30)")
    p.add_argument("--min-yield", type=float, default=0.0, dest="min_yield",
                   help="Min annualised yield %% for covered calls (default 0)")
    p.add_argument("--max-cost", type=float, default=5.0, dest="max_cost",
                   help="Max cost %% for protective puts (default 5)")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    tickers = args.ticker or load_watchlist()

    # Filter to US-listed tickers only (yfinance options work for these)
    US_TICKERS = {"TLT", "GLD", "CCJ", "ZIM", "VALE", "BDRY"}
    tickers = [t for t in tickers if t in US_TICKERS]

    if not tickers:
        print("No US-listed options-eligible tickers found. Use --ticker TLT GLD CCJ VALE ZIM")
        sys.exit(0)

    print(f"\n{SEP}")
    print(f"  OUROBOROS v2 — OPTIONS SCREENER")
    print(f"  Tickers: {', '.join(tickers)}   |   Target DTE: {args.dte}   |   Target Δ: {args.delta}")
    print(f"  Generated: {datetime.now().strftime('%Y-%m-%d %H:%M')}")
    print(f"{SEP}")

    calls_ideas = []
    puts_ideas  = []

    if args.strategy in ("calls", "both"):
        print(f"\n  Scanning {len(tickers)} tickers for covered call opportunities...")
        calls_ideas = screen_covered_calls(
            tickers,
            target_delta=args.delta,
            target_dte=args.dte,
            min_annualised_yield=args.min_yield,
        )
        print_covered_calls(calls_ideas)

    if args.strategy in ("puts", "both"):
        print(f"\n  Scanning {len(tickers)} tickers for protective put opportunities...")
        puts_ideas = screen_protective_puts(
            tickers,
            target_delta=args.delta,
            target_dte=args.dte,
            max_cost_pct=args.max_cost,
        )
        print_protective_puts(puts_ideas)

    if calls_ideas or puts_ideas:
        print_bs_summary(calls_ideas, puts_ideas)

    print(f"\n{SEP}\n")
