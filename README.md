# Whale Tracker Strategy

[![CI](https://github.com/ivancroak/WhaleTrackerStrategy/actions/workflows/ci.yml/badge.svg)](https://github.com/ivancroak/WhaleTrackerStrategy/actions/workflows/ci.yml)

A high-performance Polymarket whale copy trading bot using a **Rust + Python hybrid architecture**. The Rust engine handles latency-critical execution (WebSocket listening, EIP-712 order signing, risk management), while the Python layer handles analysis (whale discovery, scoring, signal detection, position sizing) — including a **pandas/NumPy feature-engineering pipeline and an optional scikit-learn wallet-ranking model**.

## Architecture

```
┌─────────────────────────────────────────────────────────────┐
│                    Polymarket CLOB                           │
│         WebSocket Feed          REST API                    │
└──────────┬──────────────────────────┬───────────────────────┘
           │ real-time trades         │ order placement
           ▼                          ▼
┌─────────────────────────────────────────────────────────────┐
│                 Rust Engine (reflexes)                       │
│                                                             │
│  WebSocket ──► Whale Filter ──► Signal Aggregation          │
│                                      │                      │
│                              Risk Manager (5-layer gate)    │
│                                      │                      │
│                              EIP-712 Signing ──► Executor   │
│                                                             │
│  Telegram Bot ◄──── Alerts / Status / Control               │
└─────────────────────────┬───────────────────────────────────┘
                          │ config files (JSON/TOML)
                          │ trade logs (JSONL)
┌─────────────────────────▼───────────────────────────────────┐
│                Python Analysis (brain)                       │
│                                                             │
│  Leaderboard Scanner ──► Wallet Scorer (Sharpe, Kelly, WR)  │
│  FIFO Trade Matching ──► Auto-Removal (soft/hard failures)  │
│  Feature Engineering ──► ML Ranker (scikit-learn) ──► Rank  │
│  Consensus Detection ──► Position Sizing (Kelly Criterion)  │
│  Backtesting ──► Reporting ──► Telegram Alerts              │
└─────────────────────────────────────────────────────────────┘
```

## Features

- **Real-time whale detection** via Polymarket CLOB WebSocket feed
- **Automatic wallet discovery** from Polymarket leaderboard (no Arkham needed)
- **Multi-metric scoring**: Sharpe ratio, Kelly Criterion, rolling win rate, expected value
- **Feature engineering** (pandas / NumPy): per-wallet ROI, realized-PnL volatility, Sharpe-like ratio, win rate, and exponential recency weighting
- **ML wallet ranking** via a scikit-learn `GradientBoostingRegressor` pipeline, with a deterministic heuristic fallback
- **5-layer risk gate**: position size, open count, category limits, exposure cap, drawdown breaker
- **EIP-712 order signing** with alloy (successor to deprecated ethers-rs)
- **Strategy-agnostic design** via `SignalSource` trait (Rust) / ABC (Python)
- **Telegram bot** for live monitoring and control (feature-gated)
- **Precise arithmetic** throughout: `rust_decimal` in Rust, `decimal.Decimal` in Python

## Risk Management

All trades pass through a 5-layer validation gate before execution:

| Layer | Check | Default |
|-------|-------|---------|
| Position Size | Max fraction of portfolio per trade | 10% |
| Open Positions | Max concurrent positions | 10 |
| Category Limit | Max positions per market category | 3 |
| Exposure Cap | Total portfolio at risk | 90% |
| Daily Loss Stop | Halt trading after daily loss | 5% |
| Drawdown Breaker | Full shutdown from peak equity | 12% |

Kelly Criterion position sizing with configurable confidence tiers:
- **LOW** (60-69% consensus): Quarter-Kelly (0.25)
- **MEDIUM** (70-79%): Half-Kelly (0.50)
- **HIGH** (80%+): Three-quarter Kelly (0.75)

## Quantitative & Machine-Learning Layer

Beyond the rule-based scorer, the Python layer includes a quantitative feature-engineering pipeline and an optional learned ranking model.

**Feature engineering — [`analytics.py`](analysis/src/whale_tracker/analytics.py).** Turns scored wallets (and their matched round-trips / raw activity) into a one-row-per-wallet `pandas` feature matrix. NumPy computes the statistics genuinely: win rate (`mean(returns > 0)`), PnL volatility (`std`), a Sharpe-like reward-to-variability ratio (`mean / std`), and an exponential half-life recency weight (`exp(-ln2 · days / half_life)`). Missing round-trip detail is preserved as `NaN`, so "unknown" is never silently confused with a real zero.

**Wallet-ranking model — [`model.py`](analysis/src/whale_tracker/model.py).** `WhalePerformanceModel` wraps a scikit-learn `StandardScaler → GradientBoostingRegressor` pipeline that learns to predict a wallet's realized PnL from those features and ranks candidates accordingly. It is honest by design: a *runtime-trained* estimator (not a pre-trained black box, and it publishes no accuracy claims), with a deterministic z-scored heuristic fallback whenever there is too little labelled data to train responsibly. Enable it via `rank_candidates(..., use_model=True, model=...)`; the default ranking path is unchanged and model-free.

**Out-of-sample evaluation — [`evaluation.py`](analysis/src/whale_tracker/evaluation.py), [`metrics.md`](analysis/metrics.md).** The model is evaluated the way a ranking signal should be: a disjoint **train/test split** and a **Spearman rank-IC** (does it order held-out wallets correctly?), reported against the heuristic baseline, plus out-of-sample R² and a top-vs-bottom quintile lift. On a seeded synthetic corpus the trained model reaches a held-out **rank-IC ≈ 0.54**, edging the baseline. Reproduce it in one command:

```bash
cd analysis && uv run --extra dev python examples/evaluate_ranker.py
```

The corpus is synthetic and fully seeded (no live track record is implied); see [`metrics.md`](analysis/metrics.md) for the methodology and full results.

## Prerequisites

- **Rust** 2024 edition (1.85+)
- **Python** 3.11+
- **Polygon wallet** with USDC for trading
- **Polymarket CLOB API credentials** (key, secret, passphrase)

## Quick Start

### 1. Clone and configure

```bash
git clone https://github.com/ivancroak/WhaleTrackerStrategy.git
cd WhaleTrackerStrategy
cp .env.example .env
# Edit .env with your credentials
```

### 2. Build the Rust engine

```bash
cd engine
cargo build --release
```

With Telegram bot support:

```bash
cargo build --release --features telegram
```

### 3. Install the Python analysis layer

```bash
cd analysis
pip install -e ".[dev]"
```

### 4. Run

```bash
# Rust engine (execution)
cd engine && cargo run --release

# Python analysis (scoring & monitoring)
cd analysis && python -m whale_tracker
```

## Configuration

All strategy parameters are in [`config/engine.toml`](config/engine.toml). Credentials are loaded exclusively from environment variables (`.env`).

```toml
[risk]
max_position_pct = "0.10"          # 10% per trade
max_open_positions = 10
total_exposure_cap = "0.90"
daily_loss_stop_pct = "0.05"
drawdown_breaker_pct = "0.12"

[kelly]
low = "0.25"                       # Quarter-Kelly for LOW confidence
medium = "0.50"                    # Half-Kelly for MEDIUM
high = "0.75"                      # Three-quarter Kelly for HIGH

[execution]
max_slippage_from_whale = "0.03"   # Skip if price moved >3% from whale entry
min_spread_from_resolution = "0.05" # Don't enter within 5c of resolution

[scoring]
min_markets_traded = 10
min_total_pnl = "10000"            # $10k minimum PnL
max_tracked_wallets = 25
rescan_interval_hours = 24
```

## Data Sources

All whale tracking uses **free Polymarket APIs** (no Arkham or paid services required):

| API | Auth | Purpose |
|-----|------|---------|
| [Data API](https://data-api.polymarket.com) | None | Trade history, leaderboard, positions, P&L |
| [Gamma API](https://gamma-api.polymarket.com) | None | Market metadata, categories |
| [CLOB WebSocket](wss://ws-subscriptions-clob.polymarket.com/ws/) | None | Real-time trade events |
| [CLOB REST](https://clob.polymarket.com) | API key | Order placement |
| [Polygonscan](https://polygonscan.com) | Free key | On-chain verification |

## Testing

```bash
# Rust — 33 tests (unit + integration)
cd engine && cargo test

# Python — 71 tests (scoring, risk gate, analytics, ML model)
cd analysis && python -m pytest tests/ -v

# Linting
cd engine && cargo clippy -- -D warnings
cd analysis && ruff check src/ && mypy --strict src/
```

## Project Structure

```
WhaleTrackerStrategy/
├── engine/                          # Rust execution engine
│   ├── src/
│   │   ├── main.rs                  # CLI entry, Telegram bot init
│   │   ├── engine.rs                # Tokio orchestrator (select!)
│   │   ├── websocket.rs             # CLOB WebSocket listener
│   │   ├── executor.rs              # EIP-712 signing + order execution
│   │   ├── risk_manager.rs          # 5-layer validation gate
│   │   ├── whale_signal.rs          # Consensus window accumulation
│   │   ├── signal_source.rs         # SignalSource trait
│   │   ├── models.rs                # Domain types
│   │   ├── config.rs                # Config + env loading
│   │   ├── telegram.rs              # Teloxide bot (feature-gated)
│   │   ├── clob_types.rs            # CLOB market data structures
│   │   └── error.rs                 # Error types
│   └── tests/
│       └── test_risk_manager.rs
├── analysis/                        # Python analysis layer
│   ├── src/whale_tracker/
│   │   ├── scorer.py                # Wallet discovery & scoring pipeline
│   │   ├── analytics.py             # pandas/NumPy feature engineering
│   │   ├── model.py                 # scikit-learn wallet-ranking model
│   │   ├── bot.py                   # Orchestrator loop
│   │   ├── data_api.py              # Polymarket API client
│   │   ├── risk_gate.py             # Risk validation (mirrors Rust)
│   │   ├── monitor.py               # Whale polling & consensus
│   │   ├── executor.py              # CLOB order placement
│   │   ├── models.py                # Pydantic v2 models
│   │   ├── whale_signal.py          # SignalSource implementation
│   │   └── signal_source.py         # SignalSource ABC
│   └── tests/
│       ├── test_scorer.py           # 29 scoring tests
│       ├── test_risk_gate.py        # 16 risk gate tests
│       ├── test_analytics.py        # 16 feature-engineering tests
│       └── test_model.py            # 10 ML model tests
├── config/
│   └── engine.toml                  # Strategy parameters (source of truth)
├── docs/strategy-notes/             # Strategy reference docs
├── scripts/                         # Utility scripts
├── data/                            # Trade logs (gitignored)
├── .env.example                     # Credential template
└── CLAUDE.md                        # AI assistant project context
```

## Tech Stack

### Rust Engine
| Crate | Purpose |
|-------|---------|
| tokio | Async runtime |
| tokio-tungstenite | WebSocket client |
| alloy | EIP-712 signing (replaces ethers-rs) |
| rust_decimal | Precise monetary arithmetic |
| serde + simd-json | Zero-copy JSON serialization |
| teloxide | Telegram bot (optional) |
| reqwest | HTTP client |
| tracing | Structured logging |

### Python Analysis
| Package | Purpose |
|---------|---------|
| pydantic v2 | Type-safe data models |
| pandas + numpy | Feature engineering & analytics |
| scikit-learn + joblib | Wallet-performance ranking model |
| httpx | Async HTTP client |
| py-clob-client | Polymarket order API |
| pytest + ruff + mypy | Testing & quality |

## Strategy Reference

Detailed strategy documentation lives in [`docs/strategy-notes/`](docs/strategy-notes/):

- [v2 Strategy (current)](docs/strategy-notes/whale-copy-trading-optimized.md)
- [v1 to v2 Optimization Analysis](docs/strategy-notes/strategy-optimization-analysis.md)
- [Original v1 Strategy](docs/strategy-notes/v1%20Whale%20Copy%20Trading%20Strategy%20Summary.md)
- [Build Plan](docs/strategy-notes/INITIAL_PROMPT.md)
- [External Sources](docs/strategy-notes/SOURCES.md)

## License

MIT

## Disclaimer

This software is provided for educational and research purposes. Trading on prediction markets carries significant financial risk. This bot executes real trades with real money on Polymarket. Use at your own risk. The authors are not responsible for any financial losses incurred through the use of this software. Always review and understand the code before deploying with real funds.

## Author

**Ivan Rykovski** — [GitHub](https://github.com/ivancroak) · [LinkedIn](https://www.linkedin.com/in/ivan-rykovski)

Developed privately during 2025–2026; published as a snapshot release in March 2026.
