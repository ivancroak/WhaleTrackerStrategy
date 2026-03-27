//! Strategy-agnostic signal source trait.
//!
//! Any trading strategy implements [`SignalSource`] to plug into the engine.
//! The engine calls [`process_event`] for each raw trade from the WebSocket
//! feed, and [`tick`] periodically for time-based signal logic.

use crate::models::{Signal, Trade, Wallet};

/// A pluggable signal generation strategy.
///
/// The engine feeds raw CLOB trade events into [`process_event`] and calls
/// [`tick`] on a timer. Implementations accumulate state and emit [`Signal`]s
/// when their criteria are met.
///
/// # Examples
///
/// - `WhaleSignalSource` — consensus-based whale copy trading
/// - Future: momentum, market-making, sentiment, etc.
pub trait SignalSource: Send + Sync {
    /// Process a raw trade event from the WebSocket feed.
    ///
    /// Returns zero or more signals if the trade triggers the strategy.
    fn process_event(&mut self, trade: &Trade) -> Vec<Signal>;

    /// Periodic tick for time-based signal logic.
    ///
    /// Called every ~60 seconds by the engine. Implementations can use this
    /// to emit signals from accumulated state (e.g., consensus windows that
    /// have matured).
    fn tick(&mut self) -> Vec<Signal>;

    /// Hot-reload the watched wallet list.
    ///
    /// Called when `config/wallets.json` changes on disk.
    fn update_wallets(&mut self, wallets: Vec<Wallet>);

    /// Human-readable strategy name for logging.
    fn name(&self) -> &str;
}
