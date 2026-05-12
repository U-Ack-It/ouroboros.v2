"""
Paper Trading Appraisal — go-live readiness report.

Thresholds (all must pass):
  - min_trades:      20 resolved trades
  - win_rate:        ≥ 55%
  - profit_factor:   ≥ 1.3
  - max_dd_pct:      ≤ 8%
  - sharpe:          ≥ 0.5

Usage:
    python3 -m portfolio.appraisal          # print + email
    python3 -m portfolio.appraisal --print  # print only
"""

import base64
import os
import pickle
import sys
from datetime import datetime, timezone
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

from portfolio.ledger import load_trades
from portfolio.analytics import compute_all, rolling_metrics, asset_class_breakdown

THRESHOLDS = {
    "min_trades":    20,
    "win_rate":      55.0,
    "profit_factor": 1.3,
    "max_dd_pct":    8.0,
    "sharpe":        0.5,
}

RECIPIENT   = os.getenv("REPORT_EMAIL", "olivierboukli@gmail.com")
TOKEN_PATH  = Path.home() / "gallerysense/token.pickle"
PRINT_ONLY  = "--print" in sys.argv


# ---------------------------------------------------------------------------
# Verdict
# ---------------------------------------------------------------------------

def _check(stats: dict) -> tuple[bool, list[dict]]:
    checks = []

    def row(label, val, threshold, fmt=".2f", higher_better=True):
        ok = (val >= threshold) if higher_better else (val <= threshold)
        return {"label": label, "value": val, "threshold": threshold,
                "fmt": fmt, "pass": ok}

    checks.append({"label": "Resolved trades", "value": stats["total_resolved"],
                   "threshold": THRESHOLDS["min_trades"], "fmt": "d",
                   "pass": stats["total_resolved"] >= THRESHOLDS["min_trades"]})
    checks.append(row("Win rate %",      stats["win_rate"],      THRESHOLDS["win_rate"]))
    checks.append(row("Profit factor",   stats["profit_factor"], THRESHOLDS["profit_factor"]))
    checks.append(row("Sharpe ratio",    stats["sharpe"],        THRESHOLDS["sharpe"]))
    checks.append(row("Max drawdown %",  stats["max_dd_pct"],    THRESHOLDS["max_dd_pct"],
                      higher_better=False))

    go = all(c["pass"] for c in checks)
    return go, checks


# ---------------------------------------------------------------------------
# Report builder
# ---------------------------------------------------------------------------

def build_report(trades: list[dict]) -> str:
    stats   = compute_all(trades)
    rolling = rolling_metrics(trades)

    if "error" in stats:
        return f"NO DATA — {stats['error']}"

    go, checks = _check(stats)
    verdict    = "✅ GO LIVE" if go else "⏳ NOT YET"
    passed     = sum(1 for c in checks if c["pass"])
    total_chk  = len(checks)

    lines = [
        f"Ouroboros v2 — Paper Trading Appraisal",
        f"Generated: {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}",
        f"",
        f"VERDICT: {verdict}  ({passed}/{total_chk} checks passed)",
        f"{'=' * 50}",
        f"",
        f"GATE CHECKS",
        f"-" * 40,
    ]

    for c in checks:
        tick  = "PASS" if c["pass"] else "FAIL"
        fmt   = c["fmt"]
        val   = f"{c['value']:{fmt}}" if fmt != "d" else str(int(c["value"]))
        thr   = f"{c['threshold']:{fmt}}" if fmt != "d" else str(int(c["threshold"]))
        lines.append(f"  [{tick}] {c['label']:<22} {val:>8}  (need {'≥' if c.get('higher_better', True) else '≤'} {thr})")

    lines += [
        "",
        "OVERALL STATS",
        "-" * 40,
        f"  Resolved trades:   {stats['total_resolved']}  (open: {stats['open']})",
        f"  Wins / Losses:     {stats['wins']} / {stats['losses']}",
        f"  Net P&L:           ${stats['net_pnl']:+.2f}",
        f"  Total return:      {stats['total_return_pct']:+.2f}%",
        f"  Final equity:      ${stats['final_equity']:.2f}  (started ${stats['starting_capital']:.0f})",
        f"  Avg win:           ${stats['avg_win']:.2f}",
        f"  Avg loss:          ${stats['avg_loss']:.2f}",
        f"  Sortino ratio:     {stats['sortino']:.4f}",
        f"  Max DD:            ${stats['max_dd_usd']:.2f}  ({stats['max_dd_pct']:.2f}%)",
        f"  Avg recovery:      {stats['avg_recovery_bars']:.1f} trades",
        "",
        "ROLLING WINDOWS",
        "-" * 40,
    ]

    for days, rm in sorted(rolling.items()):
        lines.append(
            f"  {days:>3}d: {rm['trades']:>3} trades | "
            f"WR {rm['win_rate']:.1f}% | "
            f"P&L ${rm['net_pnl']:+.2f} | "
            f"Sharpe {rm['sharpe']:.3f} | "
            f"MDD {rm['max_dd_pct']:.2f}%"
        )

    ac = asset_class_breakdown(trades)
    if ac:
        lines += ["", "BY ASSET CLASS", "-" * 40]
        for name, m in ac.items():
            lines.append(
                f"  {name:<12} WR {m['win_rate']:.1f}% | "
                f"PF {m['pf']:.2f} | "
                f"Net ${m['net_pnl']:+.2f} | "
                f"{m['total']} trades"
            )

    if not go:
        failing = [c["label"] for c in checks if not c["pass"]]
        lines += ["", f"Blocking: {', '.join(failing)}"]
        lines.append("Run more paper sessions and check back.")
    else:
        lines += ["", "All gates passed. Ready to switch ALPACA_BASE_URL to live."]

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Email delivery
# ---------------------------------------------------------------------------

def _send_email(subject: str, body: str):
    with open(TOKEN_PATH, "rb") as f:
        creds = pickle.load(f)
    if creds.expired and creds.refresh_token:
        from google.auth.transport.requests import Request
        creds.refresh(Request())
        with open(TOKEN_PATH, "wb") as f:
            pickle.dump(creds, f)

    from googleapiclient.discovery import build
    service = build("gmail", "v1", credentials=creds)

    msg = MIMEMultipart()
    msg["to"]      = RECIPIENT
    msg["subject"] = subject
    msg.attach(MIMEText(body, "plain"))
    raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
    service.users().messages().send(userId="me", body={"raw": raw}).execute()


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main():
    trades = load_trades()
    report = build_report(trades)
    print(report)

    if PRINT_ONLY:
        return

    resolved = [t for t in trades if t.get("outcome") in ("WIN", "LOSS")]
    stats    = compute_all(trades) if resolved else {}
    go       = False
    if stats and "error" not in stats:
        go, _ = _check(stats)

    verdict_tag = "GO LIVE" if go else "NOT YET"
    subject     = f"[Ouroboros] Paper Appraisal — {verdict_tag} | {datetime.now().strftime('%Y-%m-%d')}"

    try:
        _send_email(subject, report)
        print(f"\n→ Report emailed to {RECIPIENT}")
    except Exception as e:
        print(f"\n→ Email failed: {e}")


if __name__ == "__main__":
    main()
