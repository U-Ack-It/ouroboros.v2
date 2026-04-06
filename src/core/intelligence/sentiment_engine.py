from vaderSentiment.vaderSentiment import SentimentIntensityAnalyzer
import yfinance as yf

class OuroborosSentiment:
    def __init__(self):
        self.analyzer = SentimentIntensityAnalyzer()

    def get_ticker_sentiment(self, ticker):
        try:
            stock = yf.Ticker(ticker)
            news = stock.news
            if not news: return 0.0 
            
            scores = []
            for n in news[:5]:
                # 2026 Fallback: Use 'summary' if 'title' is missing
                text = n.get('title') or n.get('summary') or ""
                if text:
                    scores.append(self.analyzer.polarity_scores(text)['compound'])
            
            return round(sum(scores) / len(scores), 2) if scores else 0.0
        except Exception as e:
            print(f"Sentiment Logic Error: {e}")
            return 0.0

    def is_safe(self, ticker):
        score = self.get_ticker_sentiment(ticker)
        # Veto if sentiment is below -0.2 (Negative Bias)
        return (True, score) if score > -0.2 else (False, score)