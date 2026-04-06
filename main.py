import time
from scanner import find_imbalances
from src.core.gatekeeper.validator import TradeValidator
from src.core.broker.client import IBKRClient

def run_ouroboros():
    print("--- Ouroboros.v2 LIVE ORCHESTRATOR STARTING ---")
    validator = TradeValidator()
    broker = IBKRClient()
    broker.connect()
    
    # Your Neutrality Watchlist
    watchlist = ["VALE", "PBR", "BHP", "EQNR", "CCJ"]
    
    while True:
        for ticker in watchlist:
            print(f"Scanning {ticker} for Imbalance Motion...")
            signal = find_imbalances(ticker)
            
            if signal:
                print(f"ALERT: {signal['type']} detected for {ticker} at {signal['price']}")
                
                # Verify with the Safety Cage
                # Mocking current volatility at 2% for this loop
                smc_data = {"has_imbalance": True, "has_displacement": True}
                approved, msg = validator.validate_risk(ticker, signal['price'], 100, 0.02, smc_data=smc_data)
                
                if approved:
                    print(f">>> SIGNAL APPROVED: Executing Paper Trade for {ticker}. {msg}")
                    # Dispatch to IBKR
                    trade_signal = {
                        "asset": ticker,
                        "price": signal['price'],
                        "size_usd": 100,
                        "stop_loss_pct": 0.02
                    }
                    broker.execute_trade(trade_signal)
                else:
                    print(f">>> SIGNAL REJECTED: {msg}")
            
        print("Scan complete. Sleeping for 5 minutes...")
        time.sleep(300)

if __name__ == "__main__":
    run_ouroboros()