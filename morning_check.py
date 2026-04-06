import os
import schwabdev
from dotenv import load_dotenv

load_dotenv()

def morning_check():
    client = schwabdev.Client(
        os.getenv("SCHWAB_APP_KEY"),
        os.getenv("SCHWAB_APP_SECRET"),
        os.getenv("SCHWAB_CALLBACK_URL")
    )
    
    print("\n" + "="*50)
    print("OUROBOROS.V2: ACCOUNT VISION STABILIZED")
    print("="*50)
    
    try:
        # STEP 1: Use the exact command from your debug list
        print("🔗 Connecting via 'linked_accounts'...")
        response = client.linked_accounts()
        accounts = response.json()
        
        # STEP 2: Get the hashValue
        my_hash = accounts[0].get('hashValue')
        
        # STEP 3: Use the exact command 'account_details' from your list
        details_response = client.account_details(my_hash, fields='currentBalances')
        data = details_response.json()
        
        # STEP 4: Navigate to the cash
        sec_account = data.get('securitiesAccount', {})
        balances = sec_account.get('currentBalances', {})
        
        cash = balances.get('totalCash', 0.0)
        buying_power = balances.get('buyingPower', 0.0)
        
        print(f"\n✅ API HANDSHAKE: SUCCESS")
        print(f"💰 LIVE CASH: ${cash:,.2f}")
        print(f"🚀 BUYING POWER: ${buying_power:,.2f}")
        
        if cash >= 2000:
            print("\n🌟 STATUS: $2,000 DETECTED. SYSTEM ARMED.")
        else:
            print(f"\n🕒 STATUS: Deposit of $2,000 is still traveling through the pipes...")

    except Exception as e:
        print(f"❌ FINAL ERROR: {e}")

if __name__ == "__main__":
    morning_check()