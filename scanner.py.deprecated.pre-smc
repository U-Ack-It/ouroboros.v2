import yfinance as yf

def find_imbalances(ticker):
    # Fetch recent 15m candles
    data = yf.Ticker(ticker).history(period="1d", interval="15m")
    if len(data) < 3: return None

    # SMC 3-Candle FVG Logic
    c1, c2, c3 = data.iloc[-3], data.iloc[-2], data.iloc[-1]
    
    # Check for Bullish Imbalance
    if c3['Low'] > c1['High']:
        gap = c3['Low'] - c1['High']
        return {"type": "BULL_FVG", "size": gap, "price": c2['Low']}
    
    # Check for Bearish Imbalance
    if c3['High'] < c1['Low']:
        gap = c1['Low'] - c3['High']
        return {"type": "BEAR_FVG", "size": gap, "price": c2['High']}

    return None

# Use this in your main loop to flag entries