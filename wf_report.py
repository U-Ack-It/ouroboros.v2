"""
Ouroboros v2 — Walk-Forward Report

Runs (or loads) the walk-forward optimizer and prints a full analysis:
segment table, IS vs OOS comparison, param stability, recommended config.

Usage:
    python wf_report.py                         # run optimizer then report
    python wf_report.py --file wf_result.json   # load saved result
    python wf_report.py --segments 6            # pass-through to optimizer
"""

import argparse
import json
import os
import sys
from dataclasses import asdict

sys.path.insert(0, os.getcwd())

from walk_forward import WalkForwardOptimizer, WFResult, WFSegment, SegmentStats


# ---------------------------------------------------------------------------
# Printer
# ---------------------------------------------------------------------------

SEP  = "=" * 72
DASH = "-" * 72


def _sparkline_from_list(values: list[float], width: int = 30) -> str:
    bars = "▁▂▃▄▅▆▇█"
    if len(values) < 2:
        return "—"
    step = max(1, len(values) // width)
    sampled = values[::step]
    lo, hi  = min(sampled), max(sampled)
    if hi == lo:
        return bars[3] * len(sampled)
    norm = [(v - lo) / (hi - lo) for v in sampled]
    return "".join(bars[int(v * (len(bars) - 1))] for v in norm)


def _fmt_stats(s: SegmentStats) -> str:
    pf = f"{s.pf:.2f}x" if s.pf != float("inf") else "  ∞"
    return (
        f"trades={s.trades:>4}  wr={s.win_rate:>5.1f}%  "
        f"P&L=${s.net_pnl:>+8.2f}  sharpe={s.sharpe:>6.3f}  "
        f"pf={pf}  dd={s.max_dd_pct:.2f}%"
    )


def print_report(result: WFResult):
    print(f"\n{SEP}")
    print(f"  OUROBOROS v2 — WALK-FORWARD ANALYSIS")
    print(f"  Range  : {result.full_start} → {result.full_end}")
    print(f"  Windows: {result.n_segments}  |  OOS ratio: {result.oos_ratio:.0%}")
    print(f"  Tickers: {', '.join(result.tickers)}")
    print(f"  Run at : {result.run_date[:16]}")
    print(f"{SEP}\n")

    # ---- Per-segment table ----
    print(f"SEGMENT RESULTS")
    print(f"{DASH}")
    print(f"  {'Seg':<4} {'IS window':<24} {'OOS window':<24} {'Best params':<14} {'Eff':>5}")
    print(f"  {'---':<4} {'------------------------':<24} {'------------------------':<24} {'-'*14:<14} {'-----':>5}")

    for s in result.segments:
        print(f"  {s.idx:<4} {s.is_start}→{s.is_end}  {s.oos_start}→{s.oos_end}  "
              f"{s.best_label:<14} {s.efficiency:>5.2f}")
    print()

    # ---- IS vs OOS detail per segment ----
    print(f"IN-SAMPLE vs OUT-OF-SAMPLE BREAKDOWN")
    print(f"{DASH}")
    for s in result.segments:
        eff_icon = "✅" if s.efficiency >= 0.5 else ("⚠️ " if s.efficiency >= 0.2 else "❌")
        print(f"  Segment {s.idx}  [{s.best_label}]  efficiency={s.efficiency:.2f} {eff_icon}")
        print(f"    IS : {_fmt_stats(s.is_stats)}")
        print(f"    OOS: {_fmt_stats(s.oos_stats)}")
    print()

    # ---- Param stability ----
    print(f"PARAMETER STABILITY")
    print(f"{DASH}")
    counts: dict[str, int] = {}
    for s in result.segments:
        counts[s.best_label] = counts.get(s.best_label, 0) + 1
    for label, cnt in sorted(counts.items(), key=lambda x: -x[1]):
        bar = "█" * cnt + "░" * (result.n_segments - cnt)
        pct = cnt / result.n_segments * 100
        print(f"  {label:<16} {bar}  {cnt}/{result.n_segments} ({pct:.0f}%)")
    print()

    # ---- OOS combined ----
    c = result.oos_combined
    print(f"OOS COMBINED  (honest backtest — IS params applied to unseen data)")
    print(f"{DASH}")
    print(f"  Total trades : {c.trades}")
    print(f"  Win rate     : {c.win_rate:.1f}%")
    print(f"  Net P&L      : ${c.net_pnl:+,.2f}")
    pf_str = f"{c.pf:.2f}x" if c.pf != float("inf") else "∞"
    print(f"  Profit factor: {pf_str}")
    print(f"  Sharpe       : {c.sharpe:.4f}")
    print(f"  Sortino      : {c.sortino:.4f}")
    print(f"  Max drawdown : {c.max_dd_pct:.2f}%")
    print()

    # ---- Recommendation ----
    rec = result.recommended_params
    print(f"RECOMMENDATION")
    print(f"{DASH}")
    print(f"  Best params across segments: [{rec.get('label','?')}]")
    print(f"    SL: {rec.get('sl_pct', 0)*100:.1f}%   TP: {rec.get('tp_pct', 0)*100:.1f}%   "
          f"Session filter: {rec.get('session_filter', True)}")
    print(f"  Stability score : {result.stability_pct:.0f}% of OOS windows profitable")

    avg_eff = sum(s.efficiency for s in result.segments) / len(result.segments) \
              if result.segments else 0.0
    print(f"  Avg efficiency  : {avg_eff:.2f}  (target ≥ 0.50)")
    print()

    # ---- Verdict ----
    print(f"VERDICT")
    print(f"{DASH}")
    if result.is_overfit:
        print(f"  ❌ OVERFIT — avg efficiency {avg_eff:.2f} < 0.25")
        print(f"     IS performance does not transfer to OOS. Strategy needs redesign.")
    elif result.stability_pct >= 75 and avg_eff >= 0.50:
        print(f"  ✅ ROBUST — {result.stability_pct:.0f}% OOS profitable, "
              f"avg efficiency {avg_eff:.2f}")
        print(f"     Deploy with: SL={rec.get('sl_pct',0)*100:.1f}%  "
              f"TP={rec.get('tp_pct',0)*100:.1f}%  "
              f"Session filter={rec.get('session_filter',True)}")
    else:
        print(f"  ⚠️  MARGINAL — {result.stability_pct:.0f}% OOS profitable, "
              f"avg efficiency {avg_eff:.2f}")
        print(f"     Acceptable but monitor drawdown closely. Consider tighter param grid.")

    print(f"\n  Suggested backtest_config.json update:")
    print(f'    "stop_loss_pct":   {rec.get("sl_pct", 0.01)},')
    print(f'    "take_profit_pct": {rec.get("tp_pct", 0.02)},')
    print(f'    "session_filter":  {str(rec.get("session_filter", True)).lower()}')
    print(f"\n{SEP}\n")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 Walk-Forward Report")
    p.add_argument("--file",      default=None, help="Load a saved WF JSON instead of re-running")
    p.add_argument("--segments",  type=int,   default=4)
    p.add_argument("--oos-ratio", type=float, default=0.25, dest="oos_ratio")
    p.add_argument("--tickers",   nargs="+")
    p.add_argument("--save",      default=None, help="Save result to JSON file")
    return p.parse_args()


def _load_result(path: str) -> WFResult:
    with open(path) as f:
        d = json.load(f)

    def _ss(x):
        return SegmentStats(**x)

    def _seg(x):
        return WFSegment(
            idx=x["idx"],
            is_start=x["is_start"], is_end=x["is_end"],
            oos_start=x["oos_start"], oos_end=x["oos_end"],
            best_params=x["best_params"], best_label=x["best_label"],
            is_stats=_ss(x["is_stats"]),
            oos_stats=_ss(x["oos_stats"]),
            efficiency=x["efficiency"],
        )

    return WFResult(
        run_date=d["run_date"],
        full_start=d["full_start"], full_end=d["full_end"],
        n_segments=d["n_segments"], oos_ratio=d["oos_ratio"],
        tickers=d["tickers"], param_grid=d["param_grid"],
        segments=[_seg(s) for s in d["segments"]],
        recommended_params=d["recommended_params"],
        oos_combined=_ss(d["oos_combined"]),
        stability_pct=d["stability_pct"],
        is_overfit=d["is_overfit"],
    )


if __name__ == "__main__":
    args = parse_args()

    if args.file:
        result = _load_result(args.file)
    else:
        opt = WalkForwardOptimizer(
            n_segments=args.segments,
            oos_ratio=args.oos_ratio,
            tickers=args.tickers,
            verbose=True,
        )
        result = opt.run()

        if args.save:
            import math as _math

            class _Enc(json.JSONEncoder):
                def default(self, o):
                    if isinstance(o, bool):
                        return int(o)
                    if isinstance(o, float) and not _math.isfinite(o):
                        return None
                    return super().default(o)

            with open(args.save, "w") as f:
                json.dump(asdict(result), f, indent=2, cls=_Enc)
            print(f"Result saved → {args.save}")

    print_report(result)
