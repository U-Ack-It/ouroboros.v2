import os
import schwabdev
from dotenv import load_dotenv

load_dotenv()

def manual_handshake():
    # Initialize the client
    client = schwabdev.Client(
        os.getenv("SCHWAB_APP_KEY"),
        os.getenv("SCHWAB_APP_SECRET"),
        os.getenv("SCHWAB_CALLBACK_URL")
    )
    
    print("\n" + "="*50)
    print("OUROBOROS.V2: LIVE KEY EXCHANGE")
    print("="*50)
    
    # 1. We skip the browser open since you already have the link!
    print("Paste the FULL URL from the browser (the https://127.0.0.1... one) below:")
    redirect_url = input("> ").strip()
    
    # 2. Tell the library to use THIS specific URL to get your tokens
    try:
        client.update_tokens(redirect_url)
        print("\n✅ SUCCESS: Tokens saved to tokens.json!")
        
        # 3. Verify the connection immediately
        accounts = client.account_numbers().json()
        print(f"Verified: Ouroboros can see {len(accounts)} Schwab accounts.")
        
    except Exception as e:
        print(f"\n❌ ERROR: {e}")
        print("This code might have expired. If so, run the script again and get a NEW link.")

if __name__ == "__main__":
    manual_handshake()