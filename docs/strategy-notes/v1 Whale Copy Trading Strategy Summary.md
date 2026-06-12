# Whale Copy Trading Strategy

The Strategy (extracted from @seelffff's thread) - https://x.com/seelffff/status/2026412651786518916?s=20

**Core concept:** Whale copy trading on Polymarket using on-chain transparency.

**Key components from the thread:**

1. **Whale Discovery** — Find wallets via polymarketanalytics.com, Polymarket Data API, or WebSocket real-time feed
2. **Wallet Scoring System** — 4 metrics:
   - Sharpe Ratio >1.5 (risk-adjusted returns)
   - Kelly Criterion for position sizing: `f = (p × b - q) / b`
   - 30-trade rolling win rate (stop if <55% or <80% of all-time WR)
   - Expected Value >$50 per trade (accounting for 1-3% slippage)
3. **Wallet Baskets** — Don't follow one whale. Build topic-based baskets (5-10 wallets per category: geopolitics, crypto, sports). Filter out bots (>100 trades/month) and insider wallets (<10 trades, new accounts, huge sizes)
4. **Entry Signal** — When >80% of basket wallets enter the same outcome within 24-48 hours, and market spread is still >5¢ from resolution
5. **Execution Pipeline** — Polygon RPC → WebSocket listener → Whale scorer → Kelly sizing → CLOB execution
6. **Architecture:** Data Ingestion (CLOB WebSocket) → Whale Filter → Size Calculator (Kelly) → Execution (py-clob-client) → Risk Manager (max 5% portfolio per trade, max 2 open positions)
7. **Latency** — Netherlands VPS for sub-1ms to Polygon. Every second late costs 0.5-2% worse entry.
