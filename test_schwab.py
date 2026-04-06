import os, schwabdev
from dotenv import load_dotenv

load_dotenv()
client = schwabdev.Client(os.getenv("SCHWAB_APP_KEY"), os.getenv("SCHWAB_APP_SECRET"), os.getenv("SCHWAB_CALLBACK_URL"))

# 1. Identify all your accounts
print("\n🔍 PROBING SCHWAB ACCOUNTS...")
try:
    acc_res = client.linked_accounts().json()
    for i, acc in enumerate(acc_res):
        print(f"Account {i}: {acc.get('accountNumber')} | Hash: {acc.get('hashValue')}")
    
    # 2. Test a "Handshake" with Account 0
    target_hash = acc_res[0].get('hashValue')
    print(f"\n🧪 TESTING HANDSHAKE ON HASH: {target_hash}")
    
    # We send a "dummy" order for 1 share of URA just to see the response
    test_order = {
        "orderType": "MARKET",
        "session": "NORMAL",
        "duration": "DAY",
        "orderStrategyType": "SINGLE",
        "orderLegCollection": [
            {"instruction": "BUY", "quantity": 1, "instrument": {"symbol": "URA", "assetType": "EQUITY"}}
        ]
    }
    
    response = client.order_place(target_hash, test_order)
    print(f"📡 RESPONSE CODE: {response.status_code}")
    
    if response.status_code == 201:
        print("✅ SUCCESS! The execution bridge is open.")
    else:
        print(f"❌ ERROR FROM SCHWAB: {response.text}")

except Exception as e:
    print(f"❌ CONNECTION FAILED: {e}")