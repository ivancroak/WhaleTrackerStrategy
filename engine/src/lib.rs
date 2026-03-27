//! Whale Engine — Polymarket trading execution engine.
//!
//! This crate implements the latency-critical "hot path" for detecting trades
//! on Polymarket's CLOB WebSocket feed and executing orders via EIP-712
//! signed transactions.
//!
//! # Architecture
//!
//! - `models` — Domain types shared across all modules
//! - `config` — TOML + env configuration loading
//! - `risk_manager` — Five-layer risk validation gate
//! - `signal_source` — Strategy-agnostic signal generation trait
//! - `whale_signal` — Whale consensus copy-trading strategy
//! - `error` — Unified error types
//! - `clob_types` — CLOB API request/response types and contract addresses
//! - `websocket` — CLOB WebSocket listener with auto-reconnect
//! - `executor` — EIP-712 order signing and CLOB placement
//! - `engine` — Async orchestrator (tokio::select! main loop)
//!
//! Optional (feature-gated):
//! - `telegram` — Telegram alerts and control commands (`--features telegram`)

pub mod clob_types;
pub mod config;
pub mod engine;
pub mod error;
pub mod executor;
pub mod models;
pub mod risk_manager;
pub mod signal_source;
#[cfg(feature = "telegram")]
pub mod telegram;
pub mod websocket;
pub mod whale_signal;
