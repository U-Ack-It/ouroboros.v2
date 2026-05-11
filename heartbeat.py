import time
import json
import sys
import os
from datetime import datetime

sys.path.append(os.getcwd())
from src.core.gatekeeper.validator import TradeValidator
from src.core.scanner.async_scanner import scan_all_sync
from src.core.execution.executor import TradeExecutor
from src.notifications.telegram import TelegramNotifier
from portfolio.position_manager import PositionManager


def get_session_name(hour: int) -> str:
    if 3 <= hour <= 5:
        return "London/Global"
    if 8 <= hour <= 11:
        return "NY Power Hour"
    if 20 <= hour <= 23:
        return "Asia/Neutral"
    return "Zombie Hours"


def is_market_active():
    hour = datetime.now().hour
    if (3 <= hour <= 5) or (8 <= hour <= 11) or (20 <= hour <= 23):
        return True, "ACTIVE SESSION (High Volume)"
    return False, "ZOMBIE HOURS (SMC Inactive - Sleeping)"


def run_pulse():
    v        = TradeValidator(policy_path="config/risk_policy.json")
    executor = TradeExecutor()
    notifier = TelegramNotifier()
    pm       = PositionManager()

    import json as _json
    with open("config/risk_policy.json") as _f:
        _policy = _json.load(_f)
    _watchlist = list(_policy.get("neutrality_priority", {}).get("asset_mapping", {}).keys())
    _dry_run   = True  # reflects execution_config default; executor sets this
    try:
        import json as _j2
        with open("config/execution_config.json") as _f2:
            _dry_run = _j2.load(_f2).get("dry_run", True)
    except Exception:
        pass
    notifier.send_startup(_watchlist, _dry_run)

    with open("config/risk_policy.json", "r") as f:
        config = json.load(f)
    asset_map = config.get("neutrality_priority", {}).get("asset_mapping", {})
    watchlist = list(asset_map.keys())

    print("\n" + "=" * 60)
    print("OUROBOROS.V2: SMC SESSION-AWARE SCANNER + LLM GATE 4")
    print(f"Monitoring: {', '.join(watchlist)}")
    print("=" * 60 + "\n")

    while True:
        active, session_msg = is_market_active()
        current_time = datetime.now().strftime("%H:%M:%S")
        session = get_session_name(datetime.now().hour)

        if not active:
            print(f"💤 [{current_time}] {session_msg}")
            pm.eod_check()
            time.sleep(900)
            continue

        print(f"🔥 [{current_time}] {session_msg}")

        # Sync positions + EOD check at start of each active-session tick
        closed_n = pm.sync_positions()
        if closed_n:
            print(f"   📋 Position sync: {closed_n} position(s) closed")
        pm.eod_check()

        t0 = time.monotonic()
        scan_results = scan_all_sync(asset_map)
        elapsed = time.monotonic() - t0
        fvg_count = sum(1 for r in scan_results if r.has_fvg)
        print(f"   ⚡ Scanned {len(scan_results)} symbols in {elapsed:.2f}s | {fvg_count} FVG(s) detected")

        for r in scan_results:
            if not r.ok:
                print(f"⚠️  [{r.asset_class: <18}] {r.ticker: <8} [SCAN ERR]   | {r.error}")
                continue

            smc_data = {
                "has_imbalance": r.has_fvg,
                "type":  r.fvg_type,
                "size":  r.fvg_size,
                "price": r.fvg_price,
            }

            status, msg = v.validate_risk(
                r.ticker, r.price, 0, 0,
                smc_data=smc_data,
                session=session,
            )

            icon = "✅" if status else "🚫"
            fvg_tag = f"[{r.fvg_type}]" if r.has_fvg else "[NO FVG]"
            print(f"{icon} [{r.asset_class: <18}] {r.ticker: <8} {fvg_tag: <12} | {msg}")

            if status and r.has_fvg:
                # Parse verdict details from the message for the signal alert
                _conf  = 0.0
                _rsn   = msg
                _flags = []
                try:
                    import re as _re
                    _m = _re.search(r"conf=([\d.]+)", msg)
                    if _m:
                        _conf = float(_m.group(1))
                    _rsn = _re.sub(r"APPROVED \(conf=[\d.]+\): ?", "", msg)
                    _rsn = _re.sub(r"CAUTION \(conf=[\d.]+\): ?", "", _rsn)
                    _f = _re.search(r"⚠️\s+(.+)$", msg)
                    if _f:
                        _flags = [x.strip() for x in _f.group(1).split(",")]
                        _rsn   = _rsn[:_rsn.find("⚠️")].strip()
                except Exception:
                    pass

                notifier.send_signal(
                    ticker=r.ticker,
                    asset_class=r.asset_class,
                    fvg_type=r.fvg_type,
                    price=r.price,
                    session=session,
                    verdict="APPROVE",
                    confidence=_conf,
                    reasoning=_rsn,
                    risk_flags=_flags,
                )

                result = executor.execute(
                    ticker=r.ticker,
                    asset_class=r.asset_class,
                    fvg_type=r.fvg_type,
                    price=r.price,
                    session=session,
                )
                print(f"   {result.icon} {result.message}")

        print("\n" + "-" * 60)
        time.sleep(300)


if __name__ == "__main__":
    run_pulse()