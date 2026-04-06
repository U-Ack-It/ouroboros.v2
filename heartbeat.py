import time
import json
import sys
import os
from datetime import datetime

# Ensure project root is in path
sys.path.append(os.getcwd())
from src.core.gatekeeper.validator import TradeValidator

def is_market_active():
    """Checks if we are in a high-volume SMC Session (EST Time)."""
    now = datetime.now()
    hour = now.hour
    
    # 1. London/Global Chaos (3AM-5AM)
    # 2. NY Power Hour (8AM-11AM) 
    # 3. Asia/Neutral Open (8PM-11PM)
    if (3 <= hour <= 5) or (8 <= hour <= 11) or (20 <= hour <= 23):
        return True, "ACTIVE SESSION (High Volume)"
    return False, "ZOMBIE HOURS (SMC Inactive - Sleeping)"

def run_pulse():
    v = TradeValidator(policy_path="config/risk_policy.json")
    
    with open("config/risk_policy.json", 'r') as f:
        config = json.load(f)
        # Deep-dive into your 'neutrality_priority'
        asset_map = config.get("neutrality_priority", {}).get("asset_mapping", {})
        watchlist = list(asset_map.keys())

    print("\n" + "="*60)
    print("OUROBOROS.V2: SMC SESSION-AWARE SCANNER")
    print(f"Monitoring: {', '.join(watchlist)}")
    print("="*60 + "\n")

    while True:
        active, session_msg = is_market_active()
        current_time = datetime.now().strftime('%H:%M:%S')

        if not active:
            print(f"💤 [{current_time}] {session_msg}")
            time.sleep(900) # Sleep 15 mins during dead hours
            continue

        print(f"🔥 [{current_time}] {session_msg}")
        for ticker in watchlist:
            region = asset_map.get(ticker, "Global")
            # The 'True' here tells the bot an FVG exists
            status, msg = v.validate_risk(ticker, 0.0, 0, 0, smc_data={"has_imbalance": True})
            
            icon = "🌍" if status else "🚫"
            print(f"{icon} [{region: <10}] {ticker: <6} | {msg}")
        
        print("\n" + "-"*30)
        time.sleep(300) # Scan every 5 mins during sessions

if __name__ == "__main__":
    run_pulse()