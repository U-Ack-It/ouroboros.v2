# Ouroboros v2

An algorithmic trading system built on Smart Money Concepts (SMC). Scans for Fair Value Gaps across equities and crypto, routes approved signals through a 4-gate validation pipeline (ethics → market regime → FVG → LLM reasoning), executes bracket orders via Schwab, and tracks everything through a live risk dashboard.

---

## Architecture

```
heartbeat.py  ──►  async_scanner   (parallel FVG scan, all tickers)
                        │
                        ▼
                   TradeValidator  (Gate 1: ethics / boycott list)
                        │
                        ▼
                   MarketRegime    (Gate 2: VIX + SPY 200MA — blocks in CRISIS)
                        │
                        ▼
                   FVG check       (Gate 3: requires confirmed Fair Value Gap)
                        │
                        ▼
                   QuantAgent      (Gate 4: Claude LLM reasoning, prompt-cached)
                        │
                        ▼
                   TradeExecutor   (bracket order → Schwab API or dry-run)
                        │
                        ▼
                   portfolio/ledger + Telegram alerts + Risk Dashboard
```

---

## Features

**Core**
- 3-candle Fair Value Gap detection (BULL_FVG / BEAR_FVG)
- Session-aware scanning: London (3–5 AM ET), NY Power Hour (8–11 AM ET), Asia (8–11 PM ET)
- Parallel multi-symbol scan via `ThreadPoolExecutor`

**Gate pipeline**
- Gate 1: Boycott list (ethics)
- Gate 2: Market regime — VIX + SPY 200-day MA (replaces VADER sentiment)
- Gate 3: FVG presence check
- Gate 4: Claude LLM reasoning with Anthropic prompt caching

**Execution**
- Schwab bracket orders: TRIGGER (MARKET entry) → OCO (LIMIT TP + STOP SL)
- Dry-run mode (default) — simulates without submitting
- Pre-flight guards: direction, price sanity, daily trade limit, daily loss limit

**Portfolio**
- Persistent ledger (`data/trades_ledger.json`)
- Sharpe, Sortino, max drawdown, asset-class breakdown
- Position manager: TP/SL sync, EOD auto-flatten at 3:55 PM ET

**Analysis**
- Backtester: replay FVG signals on historical OHLC
- Walk-forward optimizer: rolling IS/OOS windows, parameter stability scoring

**Crypto**
- BTC, ETH, SOL, BNB, ADA, AVAX, LINK, AAVE, UNI, DOT via Binance REST
- yfinance fallback when Binance is unreachable
- Separate `CryptoQuantAgent` with crypto-specific LLM prompt

**Options**
- Black-Scholes pricing (pure Python, no scipy)
- Covered call + protective put screener
- Greeks, implied volatility via bisection

**Dashboard**
- FastAPI + Chart.js single-page app
- Live equity curve, open positions, orders log, gate decisions
- Auto-refreshes every 30 seconds

**Alerts**
- Telegram: signal fired, order placed, daily P&L digest, startup/shutdown

---

## Quick start

```bash
git clone git@github.com:U-Ack-It/ouroboros.v2.git
cd ouroboros.v2
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt
cp config/.env.template .env   # fill in API keys
```

### Run the scanner loop
```bash
python heartbeat.py
```

### Run the risk dashboard
```bash
python -m uvicorn dashboard.app:app --port 8765
# open http://localhost:8765
```

### Backtest
```bash
python backtest_report.py
python backtest_report.py --tickers TLT GLD CCJ --save reports/my_backtest.json
```

### Walk-forward optimization
```bash
python wf_report.py --segments 4 --save reports/wf_result.json
python wf_report.py --file reports/wf_result.json   # reload saved result
```

### Portfolio report
```bash
python portfolio_report.py
python portfolio_report.py --import-latest          # import most recent backtest
```

### Position manager
```bash
python portfolio/position_manager.py --status       # open positions + daily P&L
python portfolio/position_manager.py --sync         # check TP/SL fills
python portfolio/position_manager.py --flatten      # force-close all positions
```

### Daily summary (Telegram)
```bash
python daily_summary.py
python daily_summary.py --print-only                # stdout only
python daily_summary.py --test                      # verify Telegram bot
```

---

## Configuration

| File | Purpose |
|------|---------|
| `config/execution_config.json` | `dry_run`, SL/TP %, daily limits, EOD flatten time |
| `config/risk_policy.json` | watchlist, boycott list, position sizing |
| `config/backtest_config.json` | date range, tickers, sessions, interval |
| `config/crypto_config.json` | crypto watchlist, Binance settings |
| `.env` | API keys (Anthropic, Schwab, Telegram) |

### .env keys
```
ANTHROPIC_API_KEY=
SCHWAB_APP_KEY=
SCHWAB_APP_SECRET=
SCHWAB_ACCOUNT_NUMBER=
TELEGRAM_BOT_TOKEN=
TELEGRAM_CHAT_ID=
```

### Key execution settings
```json
{
  "dry_run": true,
  "long_only": true,
  "stop_loss_pct": 0.01,
  "take_profit_pct": 0.02,
  "max_daily_trades": 6,
  "daily_loss_limit_usd": -150.0,
  "eod_flatten_hour": 15,
  "eod_flatten_minute": 55
}
```

---

## Project structure

```
ouroboros.v2/
├── heartbeat.py              # main loop
├── daily_summary.py          # cron EOD digest
├── crypto_monitor.py         # standalone crypto loop
├── backtester.py             # FVG replay engine
├── walk_forward.py           # IS/OOS optimizer
├── wf_report.py              # walk-forward CLI report
├── portfolio_report.py       # portfolio analytics CLI
├── options_report.py         # options screener CLI
│
├── config/                   # JSON configs + .env template
├── portfolio/
│   ├── ledger.py             # persistent trade store
│   ├── analytics.py          # Sharpe, Sortino, drawdown
│   └── position_manager.py  # sync, loss limit, EOD flatten
│
├── dashboard/
│   ├── app.py                # FastAPI backend
│   └── static/index.html     # Chart.js dashboard
│
└── src/
    ├── core/
    │   ├── scanner/          # async parallel FVG scanner
    │   ├── gatekeeper/       # 4-gate trade validator
    │   ├── intelligence/     # sentiment engine (regime detector)
    │   ├── broker/           # Schwab client + order types
    │   └── execution/        # trade executor pipeline
    ├── llm_agent/            # QuantAgent + prompt templates
    ├── crypto/               # Binance feed + crypto scanner
    ├── options/              # Black-Scholes + chain loader
    ├── sentiment/            # VIX + SPY 200MA regime detector
    └── notifications/        # Telegram notifier + formatters
```

---

## Asset universe

**Equities** — TLT, CCJ, ZIM, GLD, BDRY, VALE, XAUUSD, XAGUSD, D05.SI, 8035.T

**Crypto** — BTCUSDT, ETHUSDT, SOLUSDT, BNBUSDT, ADAUSDT, AVAXUSDT, LINKUSDT, AAVEUSDT, UNIUSDT, DOTUSDT

---

## Walk-forward results (sample — 3 segments, Jun 2024 → Jun 2025)

| Metric | Value |
|--------|-------|
| Best params | 1% SL / 2% TP / no session filter |
| Stability | 100% of OOS windows profitable |
| OOS trades | 235 |
| OOS win rate | 73.6% |
| OOS net P&L | +$1,278 |
| OOS Sharpe | 14.48 |
| Max drawdown | 2.83% |
| Verdict | ✅ ROBUST |

---

## Requirements

- Python 3.11+
- See `requirements.txt` for full dependency list
- Schwab developer account for live execution
- Anthropic API key for Gate 4 LLM reasoning
- Telegram bot token for alerts (optional)
