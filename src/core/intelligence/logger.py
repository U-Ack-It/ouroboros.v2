import datetime
import os

def log_decision(ticker, status, reason, regime, llm_verdict=None, llm_reasoning=None, llm_confidence=None):
    log_path = "logs/trading_decisions.log"
    timestamp = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")

    log_entry = (
        f"{timestamp} | {ticker:<6} | {status:<6} | "
        f"REGIME: {regime} | {reason}"
    )

    if llm_verdict:
        log_entry += (
            f" | LLM: {llm_verdict}"
            f" (conf={llm_confidence:.2f})"
            f" — {llm_reasoning}"
        )

    log_entry += "\n"

    os.makedirs("logs", exist_ok=True)

    with open(log_path, "a") as f:
        f.write(log_entry)
