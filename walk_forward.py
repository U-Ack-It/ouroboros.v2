"""
Ouroboros v2 — Walk-Forward Optimizer

Splits the historical date range into N rolling windows, each with an
in-sample (IS) optimisation period and an out-of-sample (OOS) validation
period.  For every IS window it finds the best parameter combo (by Sharpe);
applies it to the OOS window; tracks whether that edge holds up.

Key design decisions:
  - OHLC data is fetched ONCE per ticker for the full range, then sliced.
    This avoids O(segments × params × tickers) yfinance requests.
  - Scoring on IS uses Sharpe as primary metric, profit-factor as tie-break.
  - OOS combined stats are the "honest" backtest results.
  - Efficiency ratio = OOS_sharpe / IS_sharpe — >0.5 = robust, <0.2 = overfit.

Usage:
    python walk_forward.py                          # uses config files
    python walk_forward.py --segments 4             # 4 rolling windows
    python walk_forward.py --oos-ratio 0.3          # 30% OOS per segment
    python walk_forward.py --no-cache               # skip cached data
    python walk_forward.py --save wf_result.json    # save output
"""

import argparse
import json
import math
import os
import sys
from dataclasses import dataclass, asdict, field
from datetime import datetime, date, timedelta
from typing import Optional

import pandas as pd
import yfinance as yf

sys.path.insert(0, os.getcwd())

from backtester import detect_fvg, simulate_trade, in_session, TradeResult
from portfolio.analytics import sharpe_ratio, sortino_ratio, profit_factor, max_drawdown


# ---------------------------------------------------------------------------
# Config & defaults
# ---------------------------------------------------------------------------

BT_CONFIG_PATH  = "config/backtest_config.json"
RISK_POLICY_PATH = "config/risk_policy.json"

DEFAULT_PARAM_GRID = [
    {"sl_pct": 0.010, "tp_pct": 0.020, "session_filter": True,  "label": "1/2 +sess"},
    {"sl_pct": 0.010, "tp_pct": 0.030, "session_filter": True,  "label": "1/3 +sess"},
    {"sl_pct": 0.015, "tp_pct": 0.030, "session_filter": True,  "label": "1.5/3 +sess"},
    {"sl_pct": 0.020, "tp_pct": 0.040, "session_filter": True,  "label": "2/4 +sess"},
    {"sl_pct": 0.010, "tp_pct": 0.020, "session_filter": False, "label": "1/2 nosess"},
    {"sl_pct": 0.010, "tp_pct": 0.030, "session_filter": False, "label": "1/3 nosess"},
]


# ---------------------------------------------------------------------------
# Result dataclasses
# ---------------------------------------------------------------------------

@dataclass
class SegmentStats:
    trades:     int
    wins:       int
    win_rate:   float
    net_pnl:    float
    sharpe:     float
    sortino:    float
    pf:         float
    max_dd_pct: float


@dataclass
class WFSegment:
    idx:        int
    is_start:   str
    is_end:     str
    oos_start:  str
    oos_end:    str
    best_params:  dict
    best_label:   str
    is_stats:   SegmentStats
    oos_stats:  SegmentStats
    efficiency: float   # oos_sharpe / is_sharpe


@dataclass
class WFResult:
    run_date:          str
    full_start:        str
    full_end:          str
    n_segments:        int
    oos_ratio:         float
    tickers:           list[str]
    param_grid:        list[dict]
    segments:          list[WFSegment]
    recommended_params: dict
    oos_combined:      SegmentStats
    stability_pct:     float   # % of OOS windows that were profitable
    is_overfit:        bool


# ---------------------------------------------------------------------------
# Metrics helper
# ---------------------------------------------------------------------------

def _stats(trades: list[TradeResult], position_size: float = 450.0) -> SegmentStats:
    resolved = [t for t in trades if t.outcome in ("WIN", "LOSS")]
    if not resolved:
        return SegmentStats(0, 0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0)

    wins   = [t for t in resolved if t.outcome == "WIN"]
    losses = [t for t in resolved if t.outcome == "LOSS"]
    rets   = [t.pnl_pct for t in resolved]
    eq     = [0.0]
    for t in resolved:
        eq.append(eq[-1] + t.pnl_usd)
    _, mdd = max_drawdown([position_size + v for v in eq])

    pf_raw   = profit_factor([t.pnl_usd for t in wins], [t.pnl_usd for t in losses])
    sor_raw  = sortino_ratio(rets)
    return SegmentStats(
        trades=len(resolved),
        wins=len(wins),
        win_rate=round(len(wins) / len(resolved) * 100, 2),
        net_pnl=round(sum(t.pnl_usd for t in resolved), 2),
        sharpe=sharpe_ratio(rets),
        sortino=99.99 if not math.isfinite(sor_raw) else sor_raw,
        pf=99.99 if math.isinf(pf_raw) else pf_raw,
        max_dd_pct=mdd,
    )


def _score(stats: SegmentStats) -> float:
    """Primary IS optimisation score: Sharpe × profit-factor clamp."""
    if stats.trades < 5:
        return -999.0
    pf_clamped = min(stats.pf, 5.0) if stats.pf != float("inf") else 5.0
    return stats.sharpe * pf_clamped


# ---------------------------------------------------------------------------
# OHLC cache (single fetch per ticker for full range)
# ---------------------------------------------------------------------------

def _fetch_ohlc(
    ticker: str,
    start: str,
    end: str,
    interval: str = "1h",
    cache: Optional[dict] = None,
) -> Optional[pd.DataFrame]:
    cache_key = (ticker, start, end, interval)
    if cache is not None and cache_key in cache:
        return cache[cache_key]

    try:
        # yfinance 15m limited to last 60 days — use 1h for longer ranges
        fetch_interval = "1h" if interval == "15m" else interval
        df = yf.Ticker(ticker).history(start=start, end=end, interval=fetch_interval)
        if df.empty or len(df) < 3:
            return None
        idx = pd.to_datetime(df.index)
        df.index = idx.tz_convert(None) if idx.tz is not None else idx
        if cache is not None:
            cache[cache_key] = df
        return df
    except Exception as exc:
        print(f"  ⚠️  {ticker}: fetch failed — {exc}")
        return None


# ---------------------------------------------------------------------------
# Run one params combo on a pre-sliced DataFrame
# ---------------------------------------------------------------------------

def _run_on_slice(
    df: pd.DataFrame,
    ticker: str,
    asset_class: str,
    params: dict,
    sessions_config: dict,
    max_per_session: int = 3,
) -> list[TradeResult]:
    sl_pct         = params["sl_pct"]
    tp_pct         = params["tp_pct"]
    session_filter = params.get("session_filter", True)
    position_size  = params.get("position_size_usd", 450.0)

    results = []
    daily_counts: dict[str, dict[str, int]] = {}
    i = 0

    while i < len(df) - 3:
        c1, c2, c3 = df.iloc[i], df.iloc[i + 1], df.iloc[i + 2]
        bar_time: datetime = df.index[i + 2].to_pydatetime()
        date_str = bar_time.strftime("%Y-%m-%d")

        session_name = in_session(bar_time, sessions_config)
        if session_filter and session_name is None:
            i += 1
            continue

        session_key = session_name or "all"
        day_counts  = daily_counts.setdefault(date_str, {})
        if day_counts.get(session_key, 0) >= max_per_session:
            i += 1
            continue

        fvg = detect_fvg(c1, c2, c3)
        if fvg is None:
            i += 1
            continue

        future = df.iloc[i + 3: i + 23]
        if future.empty:
            break

        trade = simulate_trade(
            ticker=ticker,
            asset_class=asset_class,
            session=session_key,
            fvg=fvg,
            entry_bar=c3,
            future_bars=future,
            position_size_usd=position_size,
            stop_loss_pct=sl_pct,
            take_profit_pct=tp_pct,
        )
        results.append(trade)
        day_counts[session_key] = day_counts.get(session_key, 0) + 1
        i += 3

    return results


# ---------------------------------------------------------------------------
# Walk-Forward engine
# ---------------------------------------------------------------------------

class WalkForwardOptimizer:
    def __init__(
        self,
        bt_config_path:   str = BT_CONFIG_PATH,
        risk_policy_path: str = RISK_POLICY_PATH,
        n_segments:       int = 4,
        oos_ratio:        float = 0.25,
        param_grid:       Optional[list[dict]] = None,
        tickers:          Optional[list[str]] = None,
        verbose:          bool = True,
    ):
        with open(bt_config_path) as f:
            self._bt_cfg = json.load(f)
        with open(risk_policy_path) as f:
            self._risk = json.load(f)

        self.n_segments  = n_segments
        self.oos_ratio   = oos_ratio
        self.param_grid  = param_grid or DEFAULT_PARAM_GRID
        self.verbose     = verbose
        self._ohlc_cache: dict = {}

        asset_map = self._risk.get("neutrality_priority", {}).get("asset_mapping", {})
        self.tickers = tickers or self._bt_cfg.get("tickers") or list(asset_map.keys())
        self.asset_map = asset_map
        self.sessions  = self._bt_cfg["sessions"]
        self.position_size = self._bt_cfg.get("position_size_usd", 450.0)
        self.interval  = self._bt_cfg.get("interval", "1h")

    def _date_windows(self) -> list[tuple[str, str, str, str]]:
        """
        Returns list of (is_start, is_end, oos_start, oos_end) date strings.
        Uses a rolling window: each segment shifts forward by one OOS period.
        """
        start = date.fromisoformat(self._bt_cfg["start_date"])
        end   = date.fromisoformat(self._bt_cfg["end_date"])
        total_days = (end - start).days

        segment_days = total_days // self.n_segments
        oos_days     = max(7, int(segment_days * self.oos_ratio))
        is_days      = segment_days - oos_days

        windows = []
        seg_start = start
        for i in range(self.n_segments):
            is_s  = seg_start
            is_e  = is_s  + timedelta(days=is_days)
            oos_s = is_e
            oos_e = oos_s + timedelta(days=oos_days)
            if oos_e > end:
                oos_e = end
            windows.append((
                is_s.isoformat(), is_e.isoformat(),
                oos_s.isoformat(), oos_e.isoformat(),
            ))
            seg_start = oos_s  # next segment starts at current OOS start (rolling)
        return windows

    def _prefetch_all(self, full_start: str, full_end: str):
        """Fetch OHLC once for every ticker across the full date range."""
        if self.verbose:
            print(f"\n  Pre-fetching OHLC ({len(self.tickers)} tickers, {full_start} → {full_end})...")
        for t in self.tickers:
            _fetch_ohlc(t, full_start, full_end, self.interval, self._ohlc_cache)
        if self.verbose:
            loaded = sum(1 for k in self._ohlc_cache if k[0] in self.tickers)
            print(f"  Cached {loaded}/{len(self.tickers)} tickers OK")

    def _run_segment_params(
        self,
        params: dict,
        start: str,
        end: str,
    ) -> list[TradeResult]:
        """Run all tickers on a date slice with given params (uses cache)."""
        params_with_size = {**params, "position_size_usd": self.position_size}
        all_results = []
        for ticker in self.tickers:
            key = (ticker, self._bt_cfg["start_date"], self._bt_cfg["end_date"], self.interval)
            full_df = self._ohlc_cache.get(key)
            if full_df is None:
                continue
            # Slice to window
            sliced = full_df[(full_df.index >= start) & (full_df.index < end)]
            if len(sliced) < 5:
                continue
            trades = _run_on_slice(
                sliced,
                ticker=ticker,
                asset_class=self.asset_map.get(ticker, "Unknown"),
                params=params_with_size,
                sessions_config=self.sessions,
                max_per_session=self._bt_cfg.get("max_trades_per_session", 3),
            )
            all_results.extend(trades)
        return all_results

    def run(self) -> WFResult:
        full_start = self._bt_cfg["start_date"]
        full_end   = self._bt_cfg["end_date"]
        windows    = self._date_windows()

        self._prefetch_all(full_start, full_end)

        sep = "=" * 64
        if self.verbose:
            print(f"\n{sep}")
            print(f"  WALK-FORWARD OPTIMISER — {len(windows)} segments")
            print(f"  Range: {full_start} → {full_end}")
            print(f"  OOS ratio: {self.oos_ratio:.0%}  |  Tickers: {', '.join(self.tickers)}")
            print(f"  Params grid: {len(self.param_grid)} combos")
            print(f"{sep}")

        segments: list[WFSegment] = []
        all_oos_trades: list[TradeResult] = []
        param_win_counts: dict[str, int] = {}

        for idx, (is_s, is_e, oos_s, oos_e) in enumerate(windows):
            if self.verbose:
                print(f"\n  Segment {idx+1}/{len(windows)}  IS:[{is_s}→{is_e}]  OOS:[{oos_s}→{oos_e}]")

            # IS optimisation — score every param combo
            best_score  = -999.0
            best_params = self.param_grid[0]
            best_label  = self.param_grid[0].get("label", "combo0")
            best_is_stats = SegmentStats(0,0,0,0,0,0,0,0)

            for p in self.param_grid:
                trades = self._run_segment_params(p, is_s, is_e)
                s = _stats(trades, self.position_size)
                score = _score(s)
                if self.verbose:
                    print(f"    IS [{p.get('label','?')}]  trades={s.trades}  "
                          f"wr={s.win_rate:.0f}%  sharpe={s.sharpe:.2f}  pf={s.pf:.2f}  "
                          f"score={score:.2f}")
                if score > best_score:
                    best_score  = score
                    best_params = p
                    best_label  = p.get("label", "?")
                    best_is_stats = s

            # OOS validation with best IS params
            oos_trades = self._run_segment_params(best_params, oos_s, oos_e)
            oos_stats  = _stats(oos_trades, self.position_size)
            all_oos_trades.extend(oos_trades)

            eff = round(oos_stats.sharpe / best_is_stats.sharpe, 3) \
                  if best_is_stats.sharpe > 0 else 0.0

            if self.verbose:
                print(f"  → Best: [{best_label}]  "
                      f"IS sharpe={best_is_stats.sharpe:.2f}  "
                      f"OOS sharpe={oos_stats.sharpe:.2f}  "
                      f"OOS P&L=${oos_stats.net_pnl:+.2f}  "
                      f"efficiency={eff:.2f}")

            param_win_counts[best_label] = param_win_counts.get(best_label, 0) + 1

            segments.append(WFSegment(
                idx=idx + 1,
                is_start=is_s, is_end=is_e,
                oos_start=oos_s, oos_end=oos_e,
                best_params=best_params,
                best_label=best_label,
                is_stats=best_is_stats,
                oos_stats=oos_stats,
                efficiency=eff,
            ))

        # Recommended params: most frequently "best" across segments
        rec_label  = max(param_win_counts, key=param_win_counts.get) \
                     if param_win_counts else best_label
        rec_params = next((p for p in self.param_grid if p.get("label") == rec_label), best_params)

        oos_combined = _stats(all_oos_trades, self.position_size)
        profitable_segs = sum(1 for s in segments if s.oos_stats.net_pnl > 0)
        stability = round(profitable_segs / len(segments) * 100, 1) if segments else 0.0
        avg_eff   = float(sum(s.efficiency for s in segments) / len(segments)) if segments else 0.0
        is_overfit = bool(avg_eff < 0.25)

        return WFResult(
            run_date=datetime.now().isoformat(),
            full_start=full_start,
            full_end=full_end,
            n_segments=len(segments),
            oos_ratio=self.oos_ratio,
            tickers=self.tickers,
            param_grid=self.param_grid,
            segments=segments,
            recommended_params=rec_params,
            oos_combined=oos_combined,
            stability_pct=stability,
            is_overfit=is_overfit,
        )


# ---------------------------------------------------------------------------
# Serialisation helper
# ---------------------------------------------------------------------------

def _to_serialisable(obj):
    if isinstance(obj, WFResult):
        d = asdict(obj)
        return d
    return str(obj)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def parse_args():
    p = argparse.ArgumentParser(description="Ouroboros v2 Walk-Forward Optimizer")
    p.add_argument("--segments",  type=int,   default=4,    help="Number of WF windows (default 4)")
    p.add_argument("--oos-ratio", type=float, default=0.25, dest="oos_ratio",
                   help="Fraction of each window reserved for OOS (default 0.25)")
    p.add_argument("--tickers",   nargs="+",  help="Override ticker list")
    p.add_argument("--save",      default=None, help="Save JSON result to path (e.g. wf_result.json)")
    p.add_argument("--quiet",     action="store_true")
    return p.parse_args()


if __name__ == "__main__":
    args = parse_args()
    opt  = WalkForwardOptimizer(
        n_segments=args.segments,
        oos_ratio=args.oos_ratio,
        tickers=args.tickers,
        verbose=not args.quiet,
    )
    result = opt.run()

    if args.save:
        import json as _json
        with open(args.save, "w") as f:
            _json.dump(asdict(result), f, indent=2)
        print(f"\nResult saved → {args.save}")

    print(f"\nRun `python wf_report.py` to see the full analysis.")
    if args.save:
        print(f"Or: `python wf_report.py --file {args.save}`")
