# WhaleTrackerStrategy — Agent Instructions

This file is read by AI agents (Claude Code).
Vendor-specific config lives in `.claude/`.

## Read These First

- `docs/ai/PROJECT_MEMORY.md` — **living project state** (what's built, what's not, recent changes)
- `.agents/rules/project-context.md` — architecture, key files, priorities
- `.agents/rules/coding-rules.md` — code style, safety rules, testing expectations
- `config/engine.toml` — all strategy parameters (DO NOT CHANGE values)
- `context.md` — chronological project diary

## Shared Memory (MANDATORY)

**After making any code changes, you MUST update `docs/ai/PROJECT_MEMORY.md`:**
1. Read it first to understand current state
2. Update the "What's Built" table if you added/changed components
3. Update "Known Issues" if you fixed or found bugs
4. Update "Recent Changes" with a one-line summary of what you did
5. Delete outdated entries (keep only last 2 sessions in "Recent Changes")
6. Verify the file still matches actual project structure and code

If PROJECT_MEMORY.md contradicts the actual code, **fix the memory file** — code is truth.

## Architecture

Rust engine (`engine/`) for fast execution + Python analysis (`analysis/`) for scoring.
Wallet selection logic lives in `analysis/src/whale_tracker/scorer.py`.
Full details in `.agents/rules/project-context.md`.

## Critical Rules

1. **NEVER change strategy parameters** in `config/engine.toml`. Only the user changes these.
2. **NEVER hardcode secrets.** Use `.env` files.
3. **Use Decimal for money.** `decimal.Decimal` (Python), `rust_decimal::Decimal` (Rust). Never float.
4. **Run tests before declaring done.** `python3 -m pytest analysis/tests/ -v` and `cd engine && cargo test`.
5. **Minimal changes only.** Do what was asked, nothing more.

## Available Skills

Shared skills in `.agents/skills/`:

| Skill | Purpose |
|---|---|
| `repo-research` | Research codebase before making changes |
| `wallet-selection-audit` | Audit wallet scoring pipeline |
| `data-pipeline-audit` | Audit API data flow |
| `polymarket` | Polymarket API reference |
| `polymarket-trading` | Order execution patterns |
| `polymarket-analysis` | Market analysis, edges |
| `polymarket-wallet-xray` | Wallet scoring, bot detection |
| `polymarket-whale-copier` | Copy trading logic |
| `kelly-position-sizing` | Kelly Criterion math |
| `rust-coding` | Idiomatic Rust patterns |
| `rust-async` | Tokio, channels, concurrency |
| `rust-performance` | Benchmarks, profiling |
| `rust-fintech` | Decimal, financial types |
| `python-pro` | Type-safe Python 3.11+ |
| `python-testing` | pytest, fixtures, property-based |
| `python-code-quality` | ruff, mypy, refactoring |

## Commands

```bash
# Python
cd analysis && python3 -m ruff check src/     # lint
cd analysis && python3 -m pytest tests/ -v     # test

# Rust
cd engine && cargo clippy -- -D warnings       # lint
cd engine && cargo test                        # test

```
