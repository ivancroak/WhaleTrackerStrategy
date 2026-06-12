# Whale Copy Trading Strategy on Polymarket — Optimized v2

Source: @seelffff X thread (February 2026), with parameter optimization and logic improvements.

---

## Why Copy Trading Works on Polymarket

Polymarket runs on Polygon. Every trade settles on-chain — every buy, sell, wallet address, position size, and entry price is permanently public. The CLOB is hybrid-decentralized: orders matched off-chain, settled on-chain via EIP-712 signatures. This transparency is the exploit.

**Critical Feb 2026 update:** The 500ms taker order delay was removed on Feb 18, 2026. Taker orders now execute instantly. Dynamic taker fees now apply on crypto markets (up to ~1.56% at 50% probability). This makes MAKER orders (limit) strongly preferred over TAKER orders (market) for copy trading — maker orders earn rebates while taker orders pay fees.

---

## Whale Discovery Methods

### Method A: Polymarket Analytics
polymarketanalytics.com — study trade history, win rate, category focus. Manual but thorough.

### Method B: Polymarket Data API
Fetch recent trades programmatically, filter by trade size and wallet history.

### Method C: WebSocket Real-Time Feed
Subscribe to live CLOB trade events. Alert on whale-size transactions from pre-scored wallets. This is the primary detection mechanism for the Rust engine.

### Method D: Polymarket Data API — Leaderboard + Holders
Use free Polymarket Data API endpoints (no auth required):
- `GET /v1/leaderboard?orderBy=VOL&timePeriod=MONTH` — discover top traders by volume/PNL
- `GET /holders?market=X` — find top holders per specific market
- `GET /closed-positions?user=X` — fetch settled P&L for win rate calculation
- Polygonscan ERC-1155 transfers for on-chain verification if needed

---

## Wallet Scoring System

### Metric 1: Sharpe Ratio (Risk-Adjusted Returns)

```
Sharpe = (avg_return - risk_free_rate) / std_dev_of_returns
```

- avg_return = mean profit per trade
- risk_free_rate ≈ 0 for Polymarket
- std_dev = volatility of trade-by-trade returns

**Threshold: Sharpe > 1.5** → wallet has consistent alpha. Below 1.0 → skip.

### Metric 2: Kelly Criterion (Position Sizing)

For prediction market structure, use the edge/odds formulation:

```
f = edge / odds
```

Where:
- edge = estimated_probability - market_price (e.g., whale's implied WR minus current market price)
- odds = (1 - market_price) / market_price (what you stand to gain per dollar risked)

Example: Whale's implied probability is 70%, market price is $0.60
- edge = 0.70 - 0.60 = 0.10
- odds = 0.40 / 0.60 = 0.667
- f = 0.10 / 0.667 = 15%
- Apply half-Kelly: 15% × 0.5 = **7.5% of bankroll**

**ALWAYS use fractional Kelly.** Default: half-Kelly (0.5 multiplier). For low-confidence signals, use quarter-Kelly (0.25).

### Metric 3: Rolling Performance (Decay Detection)

Don't just look at all-time stats. Track if the whale is still sharp.

- Calculate 30-trade rolling win rate
- **STOP copying if rolling WR drops below 52%** (was 55% in v1 — lowered because a 52% WR with favorable odds is still +EV; the key is whether odds are good, not just raw WR)
- **STOP copying if rolling WR < 75% of all-time WR** (performance decay indicator)
- Re-score all tracked wallets weekly; auto-remove wallets below thresholds

### Metric 4: Expected Value (EV) per Trade

- Only copy wallets with **EV > 1% of your average position size** (was fixed $50 — now scales to your bankroll)
- Account for 1-3% slippage per copy trade
- Account for taker fees if using market orders (~1.5% on crypto markets near 50% probability)

### Minimum History Requirements

- **Minimum 6 months of activity** (was 4 months — too short to distinguish skill from luck)
- **Minimum 50 trades** (statistical significance)
- Exclude wallets created < 6 months ago with < 50 trades

---

## Wallet Baskets Strategy

The smartest approach isn't following one whale. It's building topic-based wallet baskets.

### How to Build Baskets
1. Pick a category: geopolitics, crypto, sports, economics, weather
2. Find 5-10 wallets per category with >60% win rate and >6 months history
3. **Filter out bots:** >100 trades/month = probably automated (exclude)
4. **Filter out insiders:** new accounts + <10 trades + huge sizes (exclude)
5. **Filter out wash traders:** high volume but near-zero PnL, rapid open/close, trades at extreme prices <$0.01 (exclude — ~25% of Polymarket volume may be fake)

### Category-Specific Parameters

| Category     | Time Window | Min Basket Size | Notes |
|-------------|-------------|-----------------|-------|
| Sports      | 6-12 hours  | 5 wallets       | Fast resolution, act quickly |
| Crypto      | 12-24 hours | 7 wallets       | Moderate speed, watch for fee impact |
| Geopolitics | 24-72 hours | 8 wallets       | Slow-moving, higher confidence needed |
| Economics   | 24-48 hours | 7 wallets       | Data-driven, check for event calendar alignment |
| Weather     | 12-24 hours | 5 wallets       | NOAA data crosscheck possible |

### Dynamic Basket Rebalancing (Weekly)
- Re-score all wallets in every basket
- Auto-remove wallets whose rolling WR drops below threshold
- Auto-add newly discovered wallets that meet all criteria
- Log all changes with reasoning

---

## Signal System — Tiered Consensus

**Key improvement over v1:** Instead of a single 80% threshold, use a tiered confidence system that generates more signals while scaling position size to conviction level.

### Entry Signals

| Consensus Level | Confidence | Kelly Multiplier | Description |
|----------------|------------|-----------------|-------------|
| 60-69% of basket | LOW | 0.25 (quarter-Kelly) | Emerging signal, small position |
| 70-79% of basket | MEDIUM | 0.50 (half-Kelly) | Strong signal, standard position |
| 80%+ of basket | HIGH | 0.75 (three-quarter Kelly) | Very strong signal, larger position |

**Additional entry conditions (ALL must be true):**
- Purchases happen within the category-specific time window
- Market spread is still favorable: **>5¢ from resolution price**
- Slippage gate: current price has NOT moved >3% from the first whale's entry
- Risk manager approves (position limits, exposure caps, daily loss check)

### Exit Signals (NEW in v2)

The v1 strategy only covered entries. Knowing when to exit is equally important.

| Exit Trigger | Action |
|-------------|--------|
| >40% of basket wallets exit the same position | Generate EXIT signal, review position |
| >60% of basket wallets exit | Strong exit — close position |
| Position has not moved >2% in either direction for 5 days | Flag for review (idle capital) |
| Rolling WR of the most-weighted whale in basket drops below threshold | Review all positions sourced from that whale |
| Market approaches resolution (<24h to close) | Tighten stop or close to lock in gains |

### Anti-Chase Rule
If you detect a whale entry but the market has already moved >3% toward the whale's direction:
- Do NOT chase. The edge from copy-trading diminishes rapidly with price movement.
- Log the missed signal for backtesting purposes.
- Each second of delay costs 0.5-2% worse entry — set latency monitoring.

---

## Risk Management — Five Layers

### Layer 1: Per-Trade Limit
- **Max 3% of portfolio per trade** (was 5% — tighter because we allow more positions)
- Scaled by tiered Kelly: low-confidence signal → even smaller position

### Layer 2: Per-Category Limit
- **Max 3 positions per category** (NEW)
- Prevents overconcentration in one domain even with good signals

### Layer 3: Total Exposure Cap
- **Max 25% of portfolio in open positions at any time**
- With 8 max positions at 3% each = 24% max theoretical exposure → within cap

### Layer 4: Daily Loss Stop
- **3% of portfolio** (was 5% — tighter because more positions means cascading risk)
- If total portfolio loses 3% in a single day → halt ALL trading until next day
- Bot cancels all open orders immediately

### Layer 5: Drawdown Circuit Breaker
- **10% from peak equity** → full shutdown
- Not for a day — indefinitely, until manual review
- Diagnose: was it model decay, correlated losses, or market regime change?

### Position Limits Summary

| Parameter | Value | Rationale |
|-----------|-------|-----------|
| Max positions | 8 | Capital efficiency: avoid idle capital |
| Max per category | 3 | Diversification: no single-domain blowup |
| Max per trade | 3% of portfolio | Survivability: any single trade can't cripple you |
| Total exposure | 25% of portfolio | 75% always safe in USDC |
| Daily loss stop | 3% | Forces review before cascading losses |
| Drawdown breaker | 10% from peak | "Something is broken" alarm |
| Min spread to enter | >5¢ from resolution | Avoid near-resolved markets |
| Max slippage | 3% from whale entry | Don't chase |

---

## Execution Pipeline

```
CLOB WebSocket → Rust whale_filter → Signal check → Kelly sizing → Risk validate → CLOB execution
```

### Key Architecture Decisions

1. **Rust engine** handles the hot path: WebSocket → filter → sign → execute. Target: <5ms tick-to-trade.
2. **Python analysis** runs on schedule (hourly/daily): scoring, basket rebalancing, config generation.
3. **Maker orders preferred:** Post limit orders at the whale's entry price or slightly better. Earn rebates. Only use taker/market orders for high-confidence (80%+) time-sensitive signals.
4. **VPS location:** Netherlands or Ireland (eu-west-1) for sub-1ms to Polygon infrastructure.
5. **Latency monitoring:** Track fill price vs whale entry price for every trade. If avg slippage >2%, investigate.

### Order Type Selection

| Signal Confidence | Market Type | Order Type | Reasoning |
|------------------|-------------|------------|-----------|
| HIGH (80%+) | Fast-moving (crypto, sports) | Limit aggressive (1¢ above best ask) | Speed matters, small slippage acceptable |
| HIGH (80%+) | Slow-moving (politics) | Limit at whale price | Time to wait for fill |
| MEDIUM (70-79%) | Any | Limit at whale price | Patient entry, earn maker rebate |
| LOW (60-69%) | Any | Limit below whale price | Maximum patience, best price or skip |

---

## Post-Trade Tracking and Calibration

For every trade, log and track:
- What the basket consensus percentage was
- What the tiered Kelly recommended
- What position size was actually taken
- Fill price vs whale entry price (slippage measurement)
- Trade outcome (P&L at resolution)
- Time from whale trade to our execution (latency measurement)

### Monthly Review Checklist
- Overall win rate by category
- Average slippage by order type (maker vs taker)
- Signal quality: do higher-consensus trades actually win more often?
- Whale alpha decay: are top-performing wallets still performing?
- Edge decay: is average edge shrinking over time?
- Adjust parameters based on live data (this document is a living strategy)

---

## Edge Decay Warning

The Polymarket copy trading meta evolves fast. Whales are adapting:
- Splitting across multiple wallets to dilute tracking
- Swapping handles and using dormant accounts
- Counter-traders specifically fade known whale positions

The edge is NOT in having the fastest bot alone. It's in having the **smartest wallet selection, best scoring, tiered confidence signals, and knowing when to stop copying a fading signal.**
