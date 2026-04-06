import datetime
import os

def log_decision(ticker, status, reason, sentiment_score):
    log_path = "logs/trading_decisions.log"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    
    log_entry = (
        f"[{timestamp}] TICKER: {ticker: <6} | "
        f"RESULT: {status: <6} | "
        f"SENTIMENT: {sentiment_score: >5} | "
        f"REASON: {reason}\n"
    )
    
    if not os.path.exists("logs"):
        os.makedirs("logs")
            
    with open(log_path, "a") as f:
        f.write(log_entry)