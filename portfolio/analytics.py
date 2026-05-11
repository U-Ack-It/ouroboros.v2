"""
Portfolio Analytics — pure computation layer.

All functions are stateless: pass in a list of trade dicts, get back metrics.
No file I/O here — that lives in ledger.py.
"""

import math
from collections import defaultdict


# ---------------------------------------------------------------------------
# Risk-adjusted return metrics
# ---------------------------------------------------------------------------

def sharpe_ratio(returns: list[float], ann_factor: float = 252.0) -> float:
    """Annualised Sharpe from per-trade return % values."""
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    variance = sum((r - mean) ** 2 for r in returns) / (n - 1)
    std = math.sqrt(variance) if variance > 0 else 0.0
    if std == 0:
        return 0.0
    return round(mean / std * math.sqrt(ann_factor), 4)


def sortino_ratio(returns: list[float], target: float = 0.0, ann_factor: float = 252.0) -> float:
    """
    Annualised Sortino ratio — penalises only downside deviation.
    target: minimum acceptable return (default 0).
    """
    n = len(returns)
    if n < 2:
        return 0.0
    mean = sum(returns) / n
    downside_sq = [(min(0.0, r - target) ** 2) for r in returns]
    downside_var = sum(downside_sq) / n
    downside_std = math.sqrt(downside_var) if downside_var > 0 else 0.0
    if downside_std == 0:
        return float("inf") if mean > target else 0.0
    return round((mean - target) / downside_std * math.sqrt(ann_factor), 4)


def profit_factor(wins_pnl: list[float], losses_pnl: list[float]) -> float:
    gross_profit = sum(wins_pnl)
    gross_loss = abs(sum(losses_pnl))
    return round(gross_profit / gross_loss, 4) if gross_loss else float("inf")


# ---------------------------------------------------------------------------
# Drawdown
# ---------------------------------------------------------------------------

def drawdown_series(equity_curve: list[float]) -> list[float]:
    """
    Returns per-bar drawdown as a percentage of the running peak.
    e.g. [0.0, 0.0, -1.2, -0.5, 0.0, ...]
    """
    if not equity_curve:
        return []
    peak = equity_curve[0]
    series = []
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = (v - peak) / peak * 100 if peak else 0.0
        series.append(round(dd, 4))
    return series


def max_drawdown(equity_curve: list[float]) -> tuple[float, float]:
    """Returns (max_dd_usd, max_dd_pct)."""
    if not equity_curve:
        return 0.0, 0.0
    peak = equity_curve[0]
    max_dd_usd = 0.0
    max_dd_pct = 0.0
    for v in equity_curve:
        if v > peak:
            peak = v
        dd = peak - v
        dd_pct = dd / peak * 100 if peak else 0.0
        if dd > max_dd_usd:
            max_dd_usd = dd
            max_dd_pct = dd_pct
    return round(max_dd_usd, 2), round(max_dd_pct, 4)


def average_recovery(equity_curve: list[float]) -> float:
    """
    Average bars to recover from a drawdown to previous peak.
    Returns 0 if no drawdowns occurred.
    """
    in_drawdown = False
    recovery_lengths = []
    bars_since_peak = 0
    peak = equity_curve[0] if equity_curve else 0.0

    for v in equity_curve:
        if v >= peak:
            if in_drawdown:
                recovery_lengths.append(bars_since_peak)
                in_drawdown = False
                bars_since_peak = 0
            peak = v
        else:
            in_drawdown = True
            bars_since_peak += 1

    return round(sum(recovery_lengths) / len(recovery_lengths), 1) if recovery_lengths else 0.0


# ---------------------------------------------------------------------------
# Asset class breakdown
# ---------------------------------------------------------------------------

def asset_class_breakdown(trades: list[dict]) -> dict[str, dict]:
    """
    Groups resolved trades by asset_class.
    Returns {asset_class: {wins, losses, total, win_rate, net_pnl, avg_win, avg_loss, pf}}
    """
    groups: dict[str, list] = defaultdict(list)
    for t in trades:
        if t.get("outcome") in ("WIN", "LOSS"):
            groups[t.get("asset_class", "Unknown")].append(t)

    result = {}
    for ac, group in sorted(groups.items()):
        wins   = [t for t in group if t["outcome"] == "WIN"]
        losses = [t for t in group if t["outcome"] == "LOSS"]
        total  = len(group)
        w_pnl  = [t["pnl_usd"] for t in wins]
        l_pnl  = [t["pnl_usd"] for t in losses]
        result[ac] = {
            "wins":      len(wins),
            "losses":    len(losses),
            "total":     total,
            "win_rate":  round(len(wins) / total * 100, 1) if total else 0.0,
            "net_pnl":   round(sum(t["pnl_usd"] for t in group), 2),
            "avg_win":   round(sum(w_pnl) / len(w_pnl), 4) if w_pnl else 0.0,
            "avg_loss":  round(sum(l_pnl) / len(l_pnl), 4) if l_pnl else 0.0,
            "pf":        profit_factor(w_pnl, l_pnl),
        }
    return result


# ---------------------------------------------------------------------------
# Rolling windows
# ---------------------------------------------------------------------------

def rolling_metrics(
    trades: list[dict],
    windows: list[int] = (7, 30, 90),
    starting_capital: float = 3000.0,
) -> dict[int, dict]:
    """
    Compute summary metrics for each lookback window (in calendar days).
    trades must have 'entry_time' as ISO string for date filtering.
    Returns {window_days: {trades, win_rate, net_pnl, sharpe, sortino, max_dd_pct}}
    """
    from datetime import datetime, timedelta

    now = datetime.utcnow()
    result = {}

    for days in windows:
        cutoff = (now - timedelta(days=days)).strftime("%Y-%m-%d")
        subset = [
            t for t in trades
            if t.get("outcome") in ("WIN", "LOSS")
            and t.get("entry_time", "") >= cutoff
        ]
        if not subset:
            result[days] = {
                "trades": 0, "win_rate": 0.0, "net_pnl": 0.0,
                "sharpe": 0.0, "sortino": 0.0, "max_dd_pct": 0.0,
            }
            continue

        wins  = [t for t in subset if t["outcome"] == "WIN"]
        rets  = [t["pnl_pct"] for t in subset]
        eq    = [starting_capital]
        for t in subset:
            eq.append(eq[-1] + t["pnl_usd"])

        _, mdd_pct = max_drawdown(eq)
        result[days] = {
            "trades":    len(subset),
            "win_rate":  round(len(wins) / len(subset) * 100, 1),
            "net_pnl":   round(sum(t["pnl_usd"] for t in subset), 2),
            "sharpe":    sharpe_ratio(rets),
            "sortino":   sortino_ratio(rets),
            "max_dd_pct": mdd_pct,
        }

    return result


# ---------------------------------------------------------------------------
# Full stats bundle
# ---------------------------------------------------------------------------

def compute_all(trades: list[dict], starting_capital: float = 3000.0) -> dict:
    """
    Compute the complete stats bundle from a list of resolved trades.
    Returns a flat dict suitable for printing or JSON serialisation.
    """
    resolved = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
    wins      = [t for t in resolved if t["outcome"] == "WIN"]
    losses    = [t for t in resolved if t["outcome"] == "LOSS"]
    opens     = [t for t in trades if t.get("outcome") == "OPEN"]

    total = len(resolved)
    if total == 0:
        return {"error": "no resolved trades"}

    w_pnl = [t["pnl_usd"] for t in wins]
    l_pnl = [t["pnl_usd"] for t in losses]
    rets  = [t["pnl_pct"] for t in resolved]

    eq = [starting_capital]
    for t in resolved:
        eq.append(eq[-1] + t["pnl_usd"])

    dd_series   = drawdown_series(eq)
    mdd_usd, mdd_pct = max_drawdown(eq)
    avg_rec     = average_recovery(eq)

    return {
        "total_resolved": total,
        "open":           len(opens),
        "wins":           len(wins),
        "losses":         len(losses),
        "win_rate":       round(len(wins) / total * 100, 2),
        "starting_capital": starting_capital,
        "final_equity":   round(eq[-1], 2),
        "net_pnl":        round(eq[-1] - starting_capital, 2),
        "total_return_pct": round((eq[-1] - starting_capital) / starting_capital * 100, 2),
        "peak_equity":    round(max(eq), 2),
        "min_equity":     round(min(eq), 2),
        "avg_win":        round(sum(w_pnl) / len(w_pnl), 4) if w_pnl else 0.0,
        "avg_loss":       round(sum(l_pnl) / len(l_pnl), 4) if l_pnl else 0.0,
        "profit_factor":  profit_factor(w_pnl, l_pnl),
        "sharpe":         sharpe_ratio(rets),
        "sortino":        sortino_ratio(rets),
        "max_dd_usd":     mdd_usd,
        "max_dd_pct":     mdd_pct,
        "avg_recovery_bars": avg_rec,
        "equity_curve":   eq,
        "dd_series":      dd_series,
        "asset_classes":  asset_class_breakdown(resolved),
    }
