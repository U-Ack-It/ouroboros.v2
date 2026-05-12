# AGENTS.md — Ouroboros v2

## What this system does
Session-aware equity scanner that detects Fair Value Gaps (FVGs / Smart Money Concepts)
across a curated global watchlist, then routes each signal through a multi-gate risk
filter before executing a bracket order (live via Schwab or dry-run via yfinance).

The LLM role here is **Gate 4 reasoning**: it receives objective market data — regime,
FVG geometry, historical trade outcomes, credit stress context — and decides whether the
signal meets the bar. It does not generate trade ideas; the statistical scanner does that.

---

## Gate architecture

| Gate | Name | Block condition | Notes |
|------|------|----------------|-------|
| 0 | Universe whitelist | Ticker not in `asset_mapping` | Hard reject before any analysis |
| 1 | Ethics | Ticker in `boycott_list` | Hard reject |
| 2 | Market regime | VIX > 35 (CRISIS) | Blocks all longs; BEAR/NEUTRAL allowed with reduced size |
| 3 | SMC/FVG | No fair value gap detected | Requires `has_imbalance=True` from scanner |
| 4 | LLM reasoning | Agent verdict = BLOCK | Claude reviews signal + history + credit context |
| — | Adaptive confidence | Historical win rate < 35% | Downgrades APPROVE → CAUTION, adds risk flag |

---

## Key files

| File | Purpose |
|------|---------|
| `heartbeat.py` | Main loop — scans every 5 min during active sessions |
| `src/core/gatekeeper/validator.py` | Gate 0–4 pipeline, adaptive confidence |
| `src/core/scanner/async_scanner.py` | Parallel FVG detection across watchlist |
| `src/sentiment/regime.py` | VIX + SPY 200MA regime classifier; writes `logs/regime_snapshot.json` |
| `src/sentiment/credit_signal.py` | Reads muni_scanner output; signals credit stress to Gate 4 |
| `src/llm_agent/agent.py` | QuantAgent — wraps Claude API for Gate 4 |
| `src/llm_agent/prompts.py` | Gate 4 prompt builder — includes regime, FVG, outcomes, credit context |
| `src/core/execution/executor.py` | Sizes position by regime, builds bracket, submits to Schwab |
| `src/core/broker/schwab_client.py` | Schwab API wrapper (OAuth, orders, fills) |
| `src/notifications/telegram.py` | Telegram alerts for signals, fills, errors, daily summary |
| `portfolio/trade_memory.py` | WIN/LOSS log — appended on close, compacted every 50 entries |
| `portfolio/position_manager.py` | Syncs open positions, enforces EOD flatten, daily loss limit |
| `portfolio/ledger.py` | Persistent trade store (`data/trades_ledger.json`) |
| `portfolio/weekly_digest.py` | Sunday Telegram summary — 7-day W/L/PnL by ticker |
| `crypto_monitor.py` | Separate loop for crypto pairs via `CryptoQuantAgent` |
| `config/risk_policy.json` | Watchlist, boycott list, position sizing table |
| `config/execution_config.json` | Dry-run flag, TP/SL %, daily limits, EOD cutoff |
| `memory/trade_outcomes.md` | Raw trade log + compacted `[PATTERN]` blocks per ticker |
| `logs/regime_snapshot.json` | Latest regime label/VIX (read by muni_scanner for adaptive threshold) |
| `logs/trading_decisions.log` | Pipe-delimited gate decision log for every signal |

---

## How to run

```bash
# Main heartbeat (active sessions: London 03–05, NY 08–11, Asia 20–23 UTC-5)
python heartbeat.py

# Position manager — check open positions / force close / daily P&L
python portfolio/position_manager.py --status
python portfolio/position_manager.py --sync
python portfolio/position_manager.py --flatten
python portfolio/position_manager.py --daily-pnl

# Crypto monitor (runs independently of heartbeat)
python crypto_monitor.py

# Backtest
python backtester.py
python backtest_report.py

# Walk-forward validation
python walk_forward.py

# Morning briefing
python morning_check.py

# Force trade memory compaction
python -c "from portfolio.trade_memory import compact; compact()"

# Send weekly digest manually
python -c "from portfolio.weekly_digest import send_weekly_digest; send_weekly_digest()"
```

---

## Config guide

### `config/risk_policy.json`

| Key | Effect |
|-----|--------|
| `neutrality_priority.asset_mapping` | Approved ticker universe — any ticker not listed is rejected at Gate 0 |
| `boycott_list` | Tickers permanently blocked at Gate 1 |
| `risk_parameters.max_position_size_usd` | Base position size (used when regime snapshot is stale) |
| `regime_position_sizing` | Per-regime position caps: BULL=450, NEUTRAL=350, BEAR=250, CRISIS=0 |

### `config/execution_config.json`

| Key | Effect |
|-----|--------|
| `dry_run` | `true` → simulate fills via yfinance; `false` → live Schwab orders |
| `long_only` | `true` → BEAR_FVG signals are logged but not executed |
| `max_daily_trades` | Hard cap on total orders per calendar day |
| `stop_loss_pct` / `take_profit_pct` | Bracket legs as fraction of entry price (0.01 = 1%) |
| `daily_loss_limit_usd` | Trading halts for the day if realized P&L ≤ this value |
| `eod_flatten_hour/minute` | Force-close all open positions at this time (default 15:55 ET) |

---

## Memory system

Trade outcomes accumulate in `memory/trade_outcomes.md`:

```
## TICKER | WIN | YYYY-MM-DD
- Session: NY Power Hour | Entry: $123.40 | Exit: $125.87
- P&L: +$24.70 (+2.00%)
- Gate reasoning: Strong BULL regime, clean FVG at key support...
```

**Compaction** triggers when raw entry count ≥ 50. Entries older than 14 days are
archived to `memory/trade_outcomes_archive.md`. Per-ticker `[PATTERN]` blocks are
written with accumulated W/L stats, session breakdown, and avg P&L — these persist
across compaction cycles via a hidden `**Stored stats**` accounting line.

Gate 4 receives the last 5 raw entries + pattern summary for the ticker before deciding.
Adaptive confidence: if historical win rate < 35% (min 3 trades), APPROVE → CAUTION.

---

## Cross-system signals

```
muni_scanner output/alerts_YYYYMMDD.json
    ↓
src/sentiment/credit_signal.py
    → stressed=True when ≥2 bonds >100bps wide in past 7 days
    → credit_summary injected into Gate 4 additional_context

src/sentiment/regime.py
    → writes logs/regime_snapshot.json after every fresh fetch
    → muni_scanner reads this to set adaptive anomaly threshold
      (BULL=60bps, NEUTRAL=50bps, BEAR=40bps, CRISIS=35bps)
```

---

## Decision log format

`logs/trading_decisions.log` — one line per gate decision:

```
2026-05-11 09:32:01 | GLD | PASS | NEUTRAL | APPROVE | conf=0.82 — Clean BULL_FVG at $183.40...
2026-05-11 09:32:02 | ZIM | BLOCK | NEUTRAL | No FVG Found | —
```

Parse with `|` split: `[timestamp, ticker, status, regime_or_reason, ...]`

---

## Session windows (Eastern time)

| Session | Hours | Heartbeat interval |
|---------|-------|--------------------|
| London/Global | 03:00–05:59 | 5 min |
| NY Power Hour | 08:00–11:59 | 5 min |
| Asia/Neutral | 20:00–23:59 | 5 min |
| Zombie Hours | all other | 15 min (EOD check only) |

Session P&L summary prints to console when each active window closes.
Weekly digest fires on Sunday at the first active-session tick.
