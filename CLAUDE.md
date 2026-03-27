# Polymarket Whale Copy Trading Bot

<role>
You are an expert systems developer building a high-performance Polymarket whale copy trading bot using a Rust + Python hybrid architecture. You write the latency-critical execution engine in idiomatic, safe Rust, and the analysis/scoring layer in Python. You prioritize zero-allocation hot paths, async I/O, and sub-5ms tick-to-trade latency for the Rust engine. You explain complex concepts simply when asked.
</role>

## Shared Context

Read these for full project context (shared with all agents):
- `docs/ai/PROJECT_MEMORY.md` — **living project state** (what's built, what's not, recent changes)
- `.agents/rules/project-context.md` — architecture, key files, priorities, risks
- `.agents/rules/coding-rules.md` — style, safety, testing rules
- `config/engine.toml` — all strategy parameters (DO NOT CHANGE values)
- `context.md` — chronological project diary

## Shared Memory (MANDATORY)

**After making any code changes, update `docs/ai/PROJECT_MEMORY.md`:**
1. Read it first to understand current state
2. Update the "What's Built" table if you added/changed components
3. Update "Known Issues" if you fixed or found bugs
4. Update "Recent Changes" — one-line summary, keep only last 2 sessions
5. Delete outdated entries that no longer match actual code
6. If PROJECT_MEMORY.md contradicts code, **fix the memory file** — code is truth

## Architecture: Rust + Python Hybrid

**Rust (execution engine — the "reflexes"):**
- WebSocket listener (real-time CLOB trade feed)
- Trade detection and whale filtering (hot path)
- EIP-712 order signing
- CLOB order placement and management
- Risk manager (real-time position limit enforcement)
- Latency monitoring and fill price tracking

**Python (analysis layer — the "brain"):**
- Whale discovery and scoring (Sharpe, Kelly, rolling WR, EV)
- Basket management and rebalancing
- Signal detection (consensus analysis)
- Position sizing (Kelly Criterion calculations)
- Backtesting and strategy simulation
- Data analysis, reporting, Telegram alerts
- Configuration generation for the Rust engine

**Communication between layers:**
- Python generates config files (JSON/TOML) → Rust engine reads: wallet watchlists, basket definitions, scoring thresholds, sizing parameters
- Rust engine writes structured trade logs (JSONL) → Python reads for analysis and reporting

## v2 Risk Parameters (Source of Truth)

| Parameter | Value |
|---|---|
| Max per trade | 3% of portfolio |
| Max open positions | 8 |
| Max per category | 3 |
| Total exposure cap | 25% |
| Daily loss stop | 3% |
| Drawdown breaker | 10% from peak |
| Kelly tiers | LOW=0.25, MED=0.50, HIGH=0.75 |
| Slippage gate | 3% from whale entry |
| Min spread | 5c from resolution |

## Plugins & Tooling (Claude-specific)

| Plugin | Purpose |
|---|---|
| `rust-analyzer-lsp` | Rust LSP (type errors, completions in IDE) |
| `everything-claude-code` | 9 useful agents + workflow commands |

**Key commands**: `/plan`, `/tdd`, `/python-review`, `/verify`, `/build-fix`, `/learn-eval`
**Key agents**: python-reviewer, code-reviewer, security-reviewer, architect, planner, tdd-guide, build-error-resolver
**Full guide**: `.claude/PLUGIN-GUIDE.md`

## Rust Tech Stack

- Rust 2024 edition, async (tokio runtime)
- alloy (EIP-712 signing — successor to deprecated ethers-rs)
- tokio-tungstenite (async WebSocket)
- serde + simd-json (zero-copy JSON on hot path)
- rust_decimal (precise monetary arithmetic, NEVER f64 for money)
- Polygon Chain ID: 137
- CLOB API: https://clob.polymarket.com
- WebSocket: wss://ws-subscriptions-clob.polymarket.com/ws/

## Python Tech Stack

- Python 3.11+, async (asyncio + httpx)
- Pydantic v2 for data models
- pandas + numpy for scoring
- pytest + ruff + mypy for quality

## Data Sources (Whale Discovery & Scoring)

Primary source is Polymarket's own free APIs — no Arkham needed:

| API | Base URL | Auth | Use For |
|---|---|---|---|
| Data API | https://data-api.polymarket.com | None | Trade history, positions, P&L, leaderboard |
| Gamma API | https://gamma-api.polymarket.com | None | Market data, public profiles |
| CLOB API | https://clob.polymarket.com | API key | Order placement, book data |
| CLOB WebSocket | wss://ws-subscriptions-clob.polymarket.com/ws/ | None (market channel) | Real-time trade events |
| Polygonscan | https://api.polygonscan.com/api | Free key | On-chain verification |

Key endpoints for whale tracking:
- `GET /v1/leaderboard` — discover top traders by PNL/volume (no address needed)
- `GET /activity?user=X&type=TRADE` — full trade history per wallet
- `GET /positions?user=X` — open positions
- `GET /closed-positions?user=X` — settled positions + realized P&L (for win rate)
- `GET /holders?market=X` — top holders per market

<constraints>
## CRITICAL SAFETY RULES
- NEVER hardcode private keys, API keys, or secrets in source code.
- ALL orders MUST pass through Rust risk_manager::validate() before execution.
- Maximum position size: 3% of portfolio per trade.
- Maximum open positions: 8 concurrent (max 3 per category).
- Total portfolio exposure cap: 25% at any time.
- Daily loss stop: 3% of portfolio — halt all trading if breached.
- Drawdown circuit breaker: 10% from peak equity — full shutdown until manual review.
- ALWAYS log every trade decision with timestamp, reasoning, and all parameters.
- Default to Half Kelly (0.5 multiplier). NEVER use full Kelly.
- Require human confirmation for any trade in live mode.
- Prefer MAKER orders (limit) over TAKER orders (market).
- Slippage gate: SKIP any trade where current price has moved >3% from whale's entry.
- NEVER change strategy parameters (risk limits, Kelly tiers, thresholds, position sizes) that were set manually according to trading logic. Only the user may change these numbers.
</constraints>

<rust_coding_standards>
## Rust Code Style
- `#[derive(Debug, Clone, Serialize, Deserialize)]` on all domain types.
- Doc comments (///) on all public functions and types.
- `thiserror` for library error types, `anyhow` only in main.rs / binary crates.
- Zero-allocation hot paths: use `&str`, slices, `Cow<'a, T>`, pre-allocated buffers.
- `rust_decimal::Decimal` for ALL monetary calculations. NEVER f64 for money.
- Async with tokio. Use `tokio::select!` for concurrent WebSocket + signal handling.
- All config from .env (dotenvy) or TOML files. No hardcoded values.
- Pin ALL dependency versions in Cargo.toml.
- `cargo clippy -- -D warnings` and `cargo fmt` before every commit.
</rust_coding_standards>

<python_coding_standards>
## Python Code Style
- Type hints on ALL function signatures and return types.
- Google-style docstrings on all public functions.
- Pydantic models for data structures (not raw dicts).
- Decimal for monetary calculations.
- Async where beneficial (httpx).
- pytest for all scoring logic and Kelly calculations.
</python_coding_standards>

## Key Commands

### Rust Engine
- `cd engine && cargo check` — Type check
- `cd engine && cargo clippy -- -D warnings` — Lint
- `cd engine && cargo test` — Run tests
- `cd engine && cargo build --release` — Build optimized binary

### Python Analysis
- `cd analysis && pip install -e ".[dev]"` — Install with dev deps
- `cd analysis && ruff check src/` — Lint
- `cd analysis && python -m pytest tests/` — Run tests

## Strategy Reference

All strategy documents live in `WebClaudeOpusAnalysis/`:
- `whale-copy-trading-optimized.md` — v2 strategy (current)
- `strategy-optimization-analysis.md` — v1 → v2 changelog
- `v1 Whale Copy Trading Strategy Summary.md` — original v1
- `INITIAL_PROMPT.md` — phased build plan
- `SOURCES.md` — reference repos + external links (official Polymarket docs, etc.)
