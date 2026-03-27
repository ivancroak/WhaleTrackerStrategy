# Initial Prompt for Claude Code Session — Rust + Python Hybrid

Read CLAUDE.md and all the other files in WebClaudeOpusAnalysis folder of the project thoroughly. This is a Rust + Python hybrid project: Rust handles the latency-critical execution engine, Python handles analysis and scoring. Build one module at a time, test it, then proceed to the next phase. STOP after each phase and wait for my approval.

**PHASE 1 — Project Skeleton + Shared Models**

1. Create the full directory structure from CLAUDE.md (engine/, analysis/, config/, data/, scripts/)
2. Create `engine/Cargo.toml` with these pinned dependencies:
   - tokio (full features), tokio-tungstenite, serde + serde_json, simd-json
   - rust_decimal + rust_decimal_macros, ethers (with "signing" feature)
   - clap (derive feature), tracing + tracing-subscriber, dotenvy
   - reqwest (rustls-tls), chrono, uuid, thiserror, anyhow
   - Note in a comment: polyfill-rs or rs-clob-client to be added once we verify the crate is published and compatible
3. Create `engine/src/models.rs` — Rust domain types:
   - `Wallet { address, category, sharpe, kelly_fraction, rolling_wr, ev_per_trade, last_scored }`
   - `Trade { wallet_address, market_id, token_id, side, amount, price, timestamp }`
   - `Signal { basket_category, consensus_pct, confidence_tier (Low/Medium/High), kelly_multiplier, recommended_size, wallets_agreeing, market_id, detected_at }`
   - `Position { market_id, token_id, side, entry_price, size, opened_at, source_signal }`
   - `RiskState { portfolio_value, peak_equity, daily_pnl, open_positions, total_exposure }`
   - All with `#[derive(Debug, Clone, Serialize, Deserialize)]`, Decimal for money fields
4. Create `engine/src/config.rs` — Load from TOML + env:
   - All thresholds from strategy doc: max_position_pct (0.03), max_open_positions (8), max_per_category (3), total_exposure_cap (0.25), daily_loss_stop_pct (0.03), drawdown_breaker_pct (0.10)
   - Tiered Kelly multipliers: low (0.25), medium (0.50), high (0.75)
   - Slippage gate: max_slippage_from_whale (0.03), min_spread_from_resolution (0.05)
   - Connection: clob_url, ws_url, chain_id (137)
   - Wallet: private_key (from env only, never config file)
5. Create `config/engine.toml` with all default values documented
6. Create `.env.example` with PRIVATE_KEY, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID placeholders
7. Create `analysis/requirements.txt` with pinned Python deps

**PHASE 2 — Rust Risk Manager (test first, it gates everything)**

8. Create `engine/src/risk_manager.rs`:
   - `RiskManager` struct holding RiskState + config thresholds
   - `validate(&self, signal: &Signal) -> Result<Approval, RiskDenial>` — checks ALL 5 layers:
     a. Per-trade size ≤ max_position_pct of portfolio
     b. Category position count < max_per_category
     c. Total open positions < max_open_positions
     d. Total exposure < total_exposure_cap
     e. Daily P&L > -daily_loss_stop_pct (not breached)
     f. Drawdown from peak < drawdown_breaker_pct (not breached)
   - `record_trade(&mut self, position: &Position)` — update state
   - `record_pnl(&mut self, amount: Decimal)` — update daily P&L
   - `kill_switch(&mut self)` — emergency halt, cancel all, log reason
   - Return `RiskDenial` with human-readable reason string
9. Create `engine/tests/test_risk_manager.rs` with cases:
   - Trade approved when within all limits
   - Trade denied when position size too large
   - Trade denied when category full (3 positions in same category)
   - Trade denied when daily loss stop breached
   - Kill switch triggers on drawdown breach

**PHASE 3 — Python Scoring Engine**

10. Create `analysis/src/whale_scorer.py`:
    - `calculate_sharpe(trades: list[Trade]) -> Decimal` — Sharpe ratio from trade history
    - `calculate_kelly(win_rate: Decimal, avg_odds: Decimal) -> Decimal` — edge/odds formulation
    - `calculate_rolling_wr(trades: list[Trade], window: int = 30) -> Decimal` — 30-trade rolling
    - `detect_decay(rolling_wr: Decimal, alltime_wr: Decimal) -> bool` — True if rolling < 75% of alltime
    - `calculate_ev(trades: list[Trade], avg_position_size: Decimal) -> Decimal` — EV as % of position
    - `score_wallet(trades: list[Trade]) -> WalletScore` — composite score combining all 4 metrics
11. Create `analysis/tests/test_scorer.py`:
    - Edge cases: 100% WR, 0 trades, negative EV, exactly-at-threshold values
    - Decay detection: wallet that was 70% WR now at 52%

**PHASE 4 — Python Signal Detection + Position Sizing**

12. Create `analysis/src/signal_detector.py`:
    - Load basket definitions from config/baskets.json
    - For each basket, check recent whale trades within time window
    - Calculate consensus percentage
    - Apply tiered confidence: 60-69% → LOW, 70-79% → MEDIUM, 80%+ → HIGH
    - Check additional conditions: spread >5¢ from resolution, slippage gate
    - Return list of Signal objects
13. Create `analysis/src/position_sizer.py`:
    - Kelly from whale's historical stats
    - Apply tiered multiplier (0.25 / 0.50 / 0.75) based on signal confidence
    - Correlation adjustment: divide by (1 + number of correlated open positions in same category)
    - Cap at max_position_pct from config
    - Return recommended dollar amount and share count
14. Create `analysis/src/config_generator.py`:
    - Read scored wallets, baskets, and generated signals
    - Write engine.toml, wallets.json, baskets.json that the Rust engine will read
15. Tests for signal detector and position sizer

**PHASE 5 — Rust WebSocket Listener + Whale Filter (hot path)**

16. Create `engine/src/websocket.rs`:
    - Connect to CLOB WebSocket with auto-reconnect
    - Parse incoming trade messages using simd-json (zero-copy where possible)
    - Filter trades: only process if wallet_address is in the loaded watchlist
    - On match: construct internal Trade event, forward to signal pipeline
17. Create `engine/src/whale_filter.rs`:
    - Load wallets.json into a HashSet<Address> for O(1) lookup
    - Hot reload: watch file for changes, reload without restart
    - Log matched trades with latency timestamp (time from on-chain to detection)

**PHASE 6 — Rust Executor (paper mode first)**

18. Create `engine/src/executor.rs`:
    - Paper mode: log would-be orders to trade_log.jsonl with full details
    - Live mode (behind --live flag): EIP-712 signing + POST to CLOB API
    - Order type selection logic from strategy doc (maker limit vs aggressive limit based on confidence)
    - Track: order sent timestamp, fill timestamp, fill price, slippage vs whale entry
19. Create `engine/src/main.rs`:
    - CLI with clap: --paper (default) / --live
    - Load config → start WebSocket listener → whale filter → risk manager → executor pipeline
    - Graceful shutdown on SIGINT/SIGTERM
    - Structured logging with tracing

**PHASE 7 — Python Trade Analyzer + Alerts**

20. Create `analysis/src/trade_analyzer.py`:
    - Read trade_log.jsonl written by Rust engine
    - Calculate: win rate by category, avg slippage, signal quality correlation, edge decay
    - Monthly review metrics from strategy doc
21. Create `engine/src/alerts.rs`:
    - Async Telegram notifications (non-blocking, never delays hot path)
    - Alert on: trade executed, daily P&L summary, risk limit breach, kill switch trigger

Start with Phase 1 now. Show me what you build and wait for my approval before Phase 2.
