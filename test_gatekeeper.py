import sys
import os

# Add project root to path
sys.path.append(os.getcwd())

from src.core.gatekeeper.validator import TradeValidator

def run_integrated_test():
    print("\n" + "="*50)
    print("OUROBOROS.V2: INTEGRATED TRIPLE-GATE TEST")
    print("="*50)
    
    # 1. Initialize Validator
    v = TradeValidator(policy_path="config/risk_policy.json")
    
    # 2. Define Mock SMC Data (Simulated 'Perfect' Setup)
    perfect_smc = {"has_imbalance": True, "has_displacement": True}

    # --- CASE A: The 'Non-Aligned' Star (VALE) ---
    print("\n[GATE 1 & 2] Testing VALE (Should check Ethics + Live News)...")
    status, msg = v.validate_risk("VALE", 15.00, 100, 0.02, smc_data=perfect_smc)
    print(f"RESULT: {'PASS' if status else 'BLOCK'} -> {msg}")

    # --- CASE B: The Boycotted Asset (TEVA) ---
    print("\n[GATE 1] Testing TEVA (Should block immediately)...")
    status, msg = v.validate_risk("TEVA", 10.00, 100, 0.02, smc_data=perfect_smc)
    print(f"RESULT: {'PASS' if status else 'BLOCK'} -> {msg}")

    # --- CASE C: The Technical Rejection (PBR with no FVG) ---
    print("\n[GATE 3] Testing PBR (Should block due to missing SMC Imbalance)...")
    bad_smc = {"has_imbalance": False}
    status, msg = v.validate_risk("PBR", 12.00, 100, 0.02, smc_data=bad_smc)
    print(f"RESULT: {'PASS' if status else 'BLOCK'} -> {msg}")

if __name__ == "__main__":
    run_integrated_test()