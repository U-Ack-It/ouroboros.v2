"""
Interactive Brokers (IBKR) Execution Client
"""
import os
from dotenv import load_dotenv

load_dotenv()

class IBKRClient:
    def __init__(self):
        self.host = os.environ.get("IB_HOST", "127.0.0.1")
        self.port = int(os.environ.get("IB_PORT", 7497))
        self.client_id = int(os.environ.get("IB_CLIENT_ID", 1))
        
        self.ib = None
    
    def connect(self):
        print(f"[IBKR] Connecting to IB Gateway/TWS at {self.host}:{self.port} (Client ID: {self.client_id})")
        try:
            import ib_insync
            self.ib = ib_insync.IB()
            self.ib.connect(self.host, self.port, clientId=self.client_id)
            print("[IBKR] Connection successful.")
            return True
        except ImportError:
            print("[IBKR] 'ib_insync' not found! Please install it for actual trading.")
            return False
        except Exception as e:
            print(f"[IBKR] Connection failed. Is TWS running? Error: {e}")
            return False

    def execute_trade(self, trade_signal: dict):
        """
        Executes a paper trade using ib_insync.
        Fires only if signal was approved by the gatekeeper.
        """
        asset = trade_signal.get('asset')
        price = trade_signal.get('price', 0.0)
        size_usd = trade_signal.get('size_usd', 100)
        
        print(f"[IBKR EXECUTION] Routing PAPER TRADE for {asset}... Size: ${size_usd}")
        
        if self.ib and self.ib.isConnected():
            try:
                from ib_insync import Stock, MarketOrder
                
                # Approximate shares to buy based on current price
                if price <= 0:
                    print("[IBKR ERROR] Invalid price for sizing.")
                    return False
                    
                shares = int(size_usd // price)
                if shares <= 0:
                    shares = 1

                contract = Stock(asset, 'SMART', 'USD')
                self.ib.qualifyContracts(contract)
                
                order = MarketOrder('BUY', shares)
                trade = self.ib.placeOrder(contract, order)
                print(f"[IBKR SUCCESS] Placed order for {shares} shares of {asset}.")
                return True
            except Exception as e:
                print(f"[IBKR ERROR] Failed to place trade for {asset}: {e}")
                return False
        else:
            print("[IBKR ERROR] Cannot execute trade. Not connected to Interactive Brokers.")
            return False
