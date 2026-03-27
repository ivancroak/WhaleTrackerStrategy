# Strategy Optimization Analysis: v1 → v2

## Language Architecture Decision

### Why Rust + Python Hybrid (not pure Python)

**The bottleneck analysis:**

| Pipeline Stage | Python Latency | Rust Latency | Matters? |
|---------------|---------------|-------------|----------|
| WebSocket message parse | ~5-10ms | ~0.1ms (simd-json) | YES — first to detect = best fill |
| Whale filter lookup | ~1-2ms | ~0.01ms (HashSet) | YES — cumulative on every trade |
| EIP-712 signing | ~5-15ms | ~0.5ms | YES — signing is CPU-bound crypto |
| HTTP POST to CLOB | ~20-50ms | ~20-50ms | NO — network is the bottleneck here |
| **Total hot path** | **~35-80ms** | **~21-51ms** | **Rust saves 15-30ms per trade** |

**Why not pure Rust?** Wallet scoring, Kelly calculations, pandas data analysis, backtesting — these are all "cold path" operations that run on schedule (hourly/daily), not per-trade. Python's rich data science ecosystem (pandas, numpy, scipy) makes this dramatically easier to write and iterate on. There's no latency requirement.

**Why not pure Python?** Three confirmed reasons from the Polymarket ecosystem:
1. **polyfill-rs** (Rust Polymarket client) benchmarks 21% faster than any Python client
2. **gamma-trade-lab's Rust copy trading bot** reports "measurable edge — especially during news-driven volatility when Python/TS bots lagged or dropped events" in live testing with real funds
3. **Feb 18, 2026 change:** The 500ms taker delay was removed. Every millisecond now matters more than before. The safety margin that Python bots enjoyed is gone.

**Why not C++ or Java?**
- C++ would give similar or better performance but: no official Polymarket SDK, much harder to vibecode, Claude Code is better at Rust than C++, memory safety bugs in financial code can be catastrophic
- Java has good performance but: GC pauses are unpredictable (exactly what you don't want on the hot path), no official Polymarket SDK, heavier runtime
- Rust gives C++-level performance WITH memory safety, has official Polymarket SDK, and Claude Code Opus 4.6 writes excellent Rust

---

## Trading Logic Improvements — 12 Changes

### Change 1: Max Open Positions — 2 → 8

| | v1 | v2 |
|---|---|---|
| Max positions | 2 | 8 |
| Per-category cap | none | 3 |

**Problem with v1:** With only 2 positions, 94-97% of your capital is idle at any given time. If you find valid signals across geopolitics, crypto, AND sports simultaneously, you can only act on 2. This massively reduces expected returns.

**Why 8:** With 4-5 categories and max 3 per category, 8 positions provides real diversification while staying within the 25% total exposure cap (8 × 3% = 24%). The per-category cap of 3 prevents overconcentration — you can't have 8 positions all in crypto.

**Why not 10+:** Monitoring difficulty increases. More positions = more exits to track, more signals to process. 8 is the sweet spot for a solo operator.

### Change 2: Signal Threshold — Single 80% → Tiered 60/70/80%

| | v1 | v2 |
|---|---|---|
| Signal trigger | >80% consensus (binary) | 60-69% = LOW, 70-79% = MEDIUM, 80%+ = HIGH |
| Kelly multiplier | flat 0.5 (half-Kelly) | 0.25 / 0.50 / 0.75 (scales with confidence) |
| Signals generated | very few | ~3x more |

**Problem with v1:** 80% means 8 of 10 wallets must agree. This rarely happens, generating very few signals. Most of your edge goes untraded.

**Why tiered, not just lowered:** Simply lowering to 55% would generate many signals but with more false positives and no way to distinguish weak from strong signals. The tiered system solves both problems: more signals (60% threshold is much easier to hit) AND proper sizing (weak signals get quarter-Kelly, strong signals get three-quarter Kelly).

**Why 60% not 55%:** 55% is barely a majority — essentially a coin flip plus one wallet. At 60%, you need 6 of 10 wallets to agree, which provides meaningful consensus. Below 60%, noise dominates signal.

### Change 3: Max Position Size — 5% → 3% per trade

**Rationale:** With 8 positions instead of 2, the per-trade limit must be tighter. 5% × 8 = 40% max exposure, which exceeds the 25% total cap and creates dangerous concentration. 3% × 8 = 24%, fitting cleanly within the 25% cap. Also, tiered Kelly means most positions will be well under 3% (quarter-Kelly on a LOW signal might be 1-2%).

### Change 4: Daily Loss Stop — 5% → 3%

**Rationale:** More open positions = higher cascading loss risk. If all 8 positions are correlated (which the per-category cap mitigates but doesn't eliminate), a bad day could blow through 5% before you react. 3% forces review earlier. For a $15,000 portfolio, 3% = $450 halt vs 5% = $750 halt. The extra conservatism is worth it early in live trading.

### Change 5: EXIT Signals (NEW)

**Problem with v1:** The strategy only covers when to enter. No guidance on when to exit. In live trading, this is a critical gap — you end up holding positions indefinitely or making emotional exit decisions.

**Exit triggers added:**
- >40% of basket exits same position → review signal
- >60% of basket exits → strong exit, close position
- Position idle for 5 days (no >2% movement) → flag for review
- Market approaching resolution (<24h) → tighten or close

### Change 6: Slippage Gate (NEW)

**Problem with v1:** No mechanism to prevent chasing. If a whale buys at $0.60 and the market has already moved to $0.65 by the time you detect it, your edge is significantly reduced or gone.

**Rule:** Skip any trade where current price has moved >3% from the whale's entry. Log as a missed signal for backtesting.

### Change 7: Category-Specific Time Windows (NEW)

**Problem with v1:** A flat 24-48 hour window for all categories. But sports markets resolve in hours while political markets take weeks.

**Updated:**
- Sports: 6-12h (act fast)
- Crypto: 12-24h
- Economics: 24-48h
- Geopolitics: 24-72h (whales often build positions over days)

### Change 8: Maker Order Preference (NEW)

**Context:** Feb 2026 Polymarket introduced dynamic taker fees on crypto markets (~1.56% max). Maker orders now get rebates. For copy trading, this changes the calculus:

- LOW/MEDIUM confidence signals → always use limit orders (maker) at whale's entry price
- HIGH confidence in fast markets → use aggressive limit order (1¢ above best ask) — still maker, but more likely to fill quickly

### Change 9: Minimum Wallet History — 4 months → 6 months + 50 trades

**Rationale:** 4 months with small sample size can't distinguish luck from skill. 6 months with 50+ trades provides meaningful statistical significance for Sharpe ratio and rolling WR calculations.

### Change 10: Rolling WR Threshold — 55% → 52%

**Counterintuitive but correct:** A 52% win rate IS profitable on prediction markets if the average odds are favorable. A whale who wins 52% of the time but buys at $0.30 (3.3:1 odds) is extremely profitable. The key metric is EV, not raw WR. The 52% threshold is the minimum where, combined with average Polymarket odds, expected value stays positive. Below 52%, even favorable odds can't overcome the slippage and fees.

### Change 11: EV Threshold — Fixed $50 → 1% of Position Size

**Problem with v1:** $50 EV is meaningless context. On a $500 bankroll, $50 is 10% — great. On a $50,000 bankroll, it's 0.1% — noise. Scaling EV to position size makes the threshold meaningful regardless of bankroll.

### Change 12: Weekly Basket Rebalancing (NEW)

**Problem with v1:** Static baskets. Once built, they're never updated. But whales decay, new whales emerge, and market categories shift.

**Rule:** Every Sunday, re-score all wallets, auto-remove underperformers, auto-add new qualifiers. Log all changes with reasoning. This keeps baskets fresh and responsive to edge decay.

---

## Summary of All Parameter Changes

| Parameter | v1 Value | v2 Value | Change Rationale |
|-----------|----------|----------|------------------|
| Language | Python only | Rust engine + Python analysis | 15-30ms latency improvement on hot path |
| Max open positions | 2 | 8 (max 3/category) | Capital efficiency |
| Signal threshold | >80% binary | Tiered: 60%/70%/80% | 3x more signals, sized by confidence |
| Kelly multiplier | flat 0.5 | tiered: 0.25/0.50/0.75 | Scales with signal quality |
| Max per trade | 5% | 3% | Fits 8 positions within 25% cap |
| Daily loss stop | 5% | 3% | Tighter for more positions |
| Exit signals | none | 40%/60% basket exit triggers | Complete strategy lifecycle |
| Slippage gate | none | >3% from whale entry → skip | Prevent chasing |
| Time windows | flat 24-48h | category-specific (6-72h) | Match market speed |
| Order preference | not specified | maker (limit) preferred | Feb 2026 fee structure |
| Min wallet history | 4 months | 6 months + 50 trades | Statistical significance |
| Rolling WR threshold | 55% | 52% | EV-based reasoning |
| EV threshold | $50 fixed | 1% of position size | Bankroll-scalable |
| Basket rebalancing | none | weekly auto-rebalance | Prevent stale baskets |
| Drawdown breaker | 10% | 10% (unchanged) | Already appropriate |
| Total exposure cap | 25% (implicit) | 25% (explicit) | Made explicit as hard limit |
