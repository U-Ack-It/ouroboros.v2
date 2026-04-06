import os, time, threading, telebot, schwabdev
from dotenv import load_dotenv

# 1. Load Credentials
load_dotenv()
MY_CHAT_ID = os.getenv("MY_CHAT_ID")
bot = telebot.TeleBot(os.getenv("TELEGRAM_BOT_TOKEN"))
client = schwabdev.Client(
    os.getenv("SCHWAB_APP_KEY"), 
    os.getenv("SCHWAB_APP_SECRET"), 
    os.getenv("SCHWAB_CALLBACK_URL")
)

# 2. Setup
ACCOUNT_HASH = "3C9A775BEF629AA23D84605D5574E8994091F9D17DA47B04340B5437E699700B"
MAX_TRADES = 3
trade_count = 0

def get_smc_data(ticker):
    return {"gates_cleared": True, "reason": "Liquidity Sweep + OB Tap", "liq_swept": True}

def get_qualitative_bias(ticker):
    return 0.85 

# --- THE UPGRADED EXECUTION ARM (WITH BRACKETS) ---
def place_schwab_order(ticker, quantity=1):
    global trade_count
    try:
        # Get Current Price to set the safety brackets
        quote_res = client.price_history(ticker, periodType='day', period=1, frequencyType='minute', frequency=1).json()
        last_price = quote_res['candles'][-1]['close']
        
        # Calculate Exit Levels (1% Stop, 2% Profit)
        stop_price = round(last_price * 0.99, 2)
        profit_price = round(last_price * 1.02, 2)

        # Construct the "Bracket" Order (Entry -> SL/TP)
        order_payload = {
            "orderStrategyType": "TRIGGER",
            "orderType": "MARKET",
            "session": "NORMAL",
            "duration": "DAY",
            "orderLegCollection": [
                {
                    "instruction": "BUY",
                    "quantity": quantity,
                    "instrument": {"symbol": ticker, "assetType": "EQUITY"}
                }
            ],
            "childOrderStrategies": [
                {
                    "orderStrategyType": "OCO",
                    "childOrderStrategies": [
                        {
                            "orderType": "LIMIT",
                            "price": profit_price,
                            "instruction": "SELL",
                            "quantity": quantity,
                            "instrument": {"symbol": ticker, "assetType": "EQUITY"}
                        },
                        {
                            "orderType": "STOP",
                            "stopPrice": stop_price,
                            "instruction": "SELL",
                            "quantity": quantity,
                            "instrument": {"symbol": ticker, "assetType": "EQUITY"}
                        }
                    ]
                }
            ]
        }
        
        client.place_order(ACCOUNT_HASH, order_payload)
        trade_count += 1
        return True, f"✅ BRACKET ACTIVE: {ticker}\n🎯 Take Profit: ${profit_price}\n🛑 Stop Loss: ${stop_price}"
    except Exception as e:
        return False, f"❌ Bracket Failed: {str(e)}"

# --- THE HUNTING ENGINE ---
def predator_loop():
    global trade_count
    print(f"🎯 PREDATOR LIVE: Brackets Armed. (SL/TP Active)")
    
    while True:
        try:
            if trade_count >= MAX_TRADES:
                print("🏁 Session Limit Reached.")
                break

            for ticker in ["URA", "NVDA"]: # Staying with Equities for the bracket test
                bias = get_qualitative_bias(ticker)
                smc = get_smc_data(ticker)

                if bias > 0.7 and smc["gates_cleared"] and smc["liq_swept"]:
                    success, result_msg = place_schwab_order(ticker, quantity=1)
                    
                    full_msg = (f"🚨 <b>PREDATOR STRIKE: {ticker}</b>\n"
                                f"🛡️ {result_msg}")
                    
                    bot.send_message(MY_CHAT_ID, full_msg, parse_mode='HTML')
                    
                    if success:
                        time.sleep(300)

                print(f"[SCAN] {ticker} | Trades: {trade_count}/{MAX_TRADES} | Bias: {bias:.2f}")
            time.sleep(60)
        except Exception as e:
            print(f"⚠️ Loop Error: {e}")
            time.sleep(10)

if __name__ == "__main__":
    threading.Thread(target=predator_loop, daemon=True).start()
    bot.infinity_polling()