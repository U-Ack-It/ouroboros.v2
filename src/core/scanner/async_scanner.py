"""
Async Multi-Symbol Scanner (SMC-wired).

Runs the full SMC pipeline (bar_aggregator -> fvg_detector -> trend_engine
-> gate_pipeline -> position_sizer) for every EQUITY watchlist symbol
concurrently via a thread-pool (Alpaca REST is blocking).

Symbols with asset_class starting with "Crypto" are routed to the legacy
crypto scanner path (src/crypto/scanner.py) which still uses its own feed.

Typical speedup: 10 symbols sequential ~10s → parallel ~1-2s.

Usage (standalone):
    python src/core/scanner/async_scanner.py
    python src/core/scanner/async_scanner.py --tickers GLD SPY QQQ
    python src/core/scanner/async_scanner.py --watch          # live loop

From code:
    from src.core.scanner.async_scanner import scan_all, scan_all_sync
    results = asyncio.run(scan_all(watchlist_dict))
"""
import asyncio
import json
import os
import sys
import time
import argparse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

sys.path.insert(0, os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", "..")))

from src.smc.scanner import scan_symbol as _scan_equity_symbol
from src.core.scanner.scan_result import ScanResult

# One shared executor — reused across scan cycles to avoid thread-spawn overhead
_EXECUTOR = ThreadPoolExecutor(max_workers=20, thread_name_prefix="ouroboros_scan")


# ---------------------------------------------------------------------------
# Single-symbol worker (runs in thread pool)
# ---------------------------------------------------------------------------
def _scan_one(ticker: str, asset_class: str) -> ScanResult:
    """
    Route to the correct scanner based on asset_class:
      - "Crypto*"  -> legacy crypto scanner (src/crypto/scanner.py)
      - everything else -> SMC pipeline via src/smc/scanner.py

    Preserves the ScanResult contract used by heartbeat.py, telegram_bot.py,
    portfolio_report.py, and the ledger.
    """
    t0 = time.monotonic()
    ac = (asset_class or "").strip()

    # Crypto path: unchanged, uses its own feed / scanner
    if ac.lower().startswith("crypto"):
        try:
            # TODO: replace with live crypto feed via Alpaca crypto data API
            # once cost-freeze allows it. For now, delegate to existing scanner.
            from src.crypto.scanner import scan_symbol as _crypto_scan
            fvg = _crypto_scan(ticker, interval="15m")
            fvg_dict = None
            price = 0.0
            if fvg is not None:
                # CryptoFVG -> dict shim to preserve ScanResult contract
                fvg_dict = {
                    "type":  getattr(fvg, "type", "BULL_FVG"),
                    "size":  float(getattr(fvg, "size", 0.0)),
                    "price": float(getattr(fvg, "price", 0.0)),
                }
                price = float(getattr(fvg, "price", 0.0))
            return ScanResult(
                ticker=ticker, asset_class=asset_class,
                fvg=fvg_dict, price=price,
                scan_ms=round((time.monotonic() - t0) * 1000, 1),
                error=None,
            )
        except Exception as exc:
            return ScanResult(
                ticker=ticker, asset_class=asset_class,
                fvg=None, price=0.0,
                scan_ms=round((time.monotonic() - t0) * 1000, 1),
                error=f"crypto scan failed: {type(exc).__name__}: {exc}",
            )

    # Equity path: full SMC pipeline
    return _scan_equity_symbol(ticker, asset_class=asset_class)


# ---------------------------------------------------------------------------
# Async entry point
# ---------------------------------------------------------------------------
async def scan_all(
    watchlist: dict[str, str],
    timeout: float = 15.0,
) -> list[ScanResult]:
    """
    Fan out one scan per watchlist entry.

    watchlist: {ticker: asset_class}
    timeout:   per-batch wall-clock cap in seconds

    Returns list[ScanResult] in completion order.
    """
    loop = asyncio.get_running_loop()
    tasks = [
        loop.run_in_executor(_EXECUTOR, _scan_one, ticker, asset_class)
        for ticker, asset_class in watchlist.items()
    ]
    if not tasks:
        return []
    done, pending = await asyncio.wait(tasks, timeout=timeout)
    results: list[ScanResult] = []
    for t in done:
        try:
            results.append(t.result())
        except Exception as exc:
            results.append(ScanResult(
                ticker="?", asset_class="?", fvg=None, price=0.0,
                scan_ms=0.0, error=f"task crashed: {exc}",
            ))
    # Cancelled tasks -> record as timeouts
    for t in pending:
        t.cancel()
        results.append(ScanResult(
            ticker="?", asset_class="?", fvg=None, price=0.0,
            scan_ms=round(timeout * 1000, 1),
            error=f"scan timeout after {timeout}s",
        ))
    return results


# ---------------------------------------------------------------------------
# Batch helper — wraps scan_all for sync callers (heartbeat.py)
# ---------------------------------------------------------------------------
def scan_all_sync(watchlist: dict[str, str], timeout: float = 15.0) -> list[ScanResult]:
    """Blocking wrapper around scan_all for use from sync code (e.g. heartbeat.py)."""
    return asyncio.run(scan_all(watchlist, timeout=timeout))


# ---------------------------------------------------------------------------
# Watchlist loading (preserved)
# ---------------------------------------------------------------------------
def _load_watchlist() -> dict[str, str]:
    """Load {ticker: asset_class} from the neutrality policy JSON.
    Same source of truth as heartbeat.py (asset_mapping in the policy)."""
    root = os.path.normpath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
    candidates = [
        os.path.join(root, "config", "neutrality_policy.json"),
        os.path.join(root, "neutrality_policy.json"),
        os.path.join(root, "config", "watchlist.json"),
    ]
    for path in candidates:
        if os.path.isfile(path):
            try:
                with open(path, "r") as f:
                    data = json.load(f)
                mapping = (data.get("neutrality_priority", {})
                               .get("asset_mapping")) or data.get("asset_mapping") or data
                if isinstance(mapping, dict):
                    return {str(k): str(v) for k, v in mapping.items()}
            except Exception:
                continue
    # Fallback default: minimal universe, all Equity
    return {
        "SPY": "ETF-Equity", "QQQ": "ETF-Equity", "IWM": "ETF-Equity",
        "GLD": "ETF-Commodity", "TLT": "ETF-Bond",
    }


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _print_table(results: list[ScanResult]) -> None:
    print(f"\n{'Ticker':<10} {'AssetClass':<22} {'FVG':<12} {'Price':>10} {'ms':>6}")
    print("-" * 72)
    for r in results:
        if r.error:
            print(f"  {r.ticker:<10} {r.asset_class:<22} {'ERROR':<12} {'—':>10} {r.scan_ms:>6.0f}  ⚠️  {r.error}")
        else:
            fvg_tag = "-"
            icon = ""
            if r.fvg:
                fvg_tag = r.fvg.get("type", "?")
                icon = "🟢" if fvg_tag == "BULL_FVG" else "🔴"
            print(f"  {r.ticker:<10} {r.asset_class:<22} {fvg_tag:<12} {r.price:>10.4f} {r.scan_ms:>6.0f}  {icon}")


def _main() -> int:
    ap = argparse.ArgumentParser(description="Ouroboros async SMC scanner")
    ap.add_argument("--tickers", nargs="+", help="Override watchlist with these tickers")
    ap.add_argument("--asset-class", default="Equity",
                    help="Asset class label when --tickers is used (default Equity)")
    ap.add_argument("--timeout", type=float, default=15.0)
    ap.add_argument("--watch", action="store_true", help="Loop every 60s")
    args = ap.parse_args()

    watchlist = _load_watchlist()
    if args.tickers:
        watchlist = {t.upper(): args.asset_class for t in args.tickers}

    async def _once() -> list[ScanResult]:
        return await scan_all(watchlist, timeout=args.timeout)

    if args.watch:
        try:
            while True:
                t0 = time.monotonic()
                results = asyncio.run(_once())
                print(f"\n[{datetime.utcnow().isoformat(timespec='seconds')}Z] "
                      f"scanned {len(results)} in {(time.monotonic()-t0)*1000:.0f}ms")
                _print_table(results)
                time.sleep(max(0.0, 60.0 - (time.monotonic() - t0)))
        except KeyboardInterrupt:
            return 0
    else:
        results = asyncio.run(_once())
        _print_table(results)
        return 0


if __name__ == "__main__":
    sys.exit(_main())
