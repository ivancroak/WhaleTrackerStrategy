//! Whale consensus signal source — copy-trading strategy.
//!
//! Watches a set of whale wallet addresses and accumulates trades into
//! consensus windows keyed by `(market_id, token_id, side)`. When a window
//! reaches a configurable age (tick), it emits a [`Signal`] with confidence
//! tier based on how many distinct whales agree.
//!
//! Confidence tiers:
//! - 1 whale  → LOW  (0.25 Kelly)
//! - 2 whales → MEDIUM (0.50 Kelly)
//! - 3+ whales → HIGH (0.75 Kelly)

use std::collections::{HashMap, HashSet};

use chrono::{DateTime, Duration, Utc};
use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use uuid::Uuid;

use crate::config::KellyConfig;
use crate::models::{ConfidenceTier, Side, Signal, Trade, Wallet};
use crate::signal_source::SignalSource;

// ---------------------------------------------------------------------------
// Config
// ---------------------------------------------------------------------------

/// Configuration for whale consensus signal generation.
#[derive(Debug, Clone)]
pub struct WhaleSignalConfig {
    /// How long to accumulate trades before emitting a signal (seconds).
    pub consensus_window_secs: i64,
    /// Minimum distinct wallets to emit any signal.
    pub min_wallets: u32,
    /// Portfolio value for sizing.
    pub portfolio_value: Decimal,
    /// Max position as fraction of portfolio.
    pub max_position_pct: Decimal,
    /// Kelly multipliers per tier.
    pub kelly: KellyConfig,
    /// Minimum spread from resolution price (skip if price < this or > 1 - this).
    pub min_spread_from_resolution: Decimal,
}

impl Default for WhaleSignalConfig {
    fn default() -> Self {
        Self {
            consensus_window_secs: 300,
            min_wallets: 1,
            portfolio_value: dec!(100),
            max_position_pct: dec!(0.03),
            kelly: KellyConfig {
                low: dec!(0.25),
                medium: dec!(0.50),
                high: dec!(0.75),
            },
            min_spread_from_resolution: dec!(0.05),
        }
    }
}

// ---------------------------------------------------------------------------
// Consensus window
// ---------------------------------------------------------------------------

/// Accumulates trades for a single (market, token, side) group.
#[derive(Debug, Clone)]
struct ConsensusWindow {
    /// Distinct wallet addresses that traded.
    distinct_wallets: HashSet<String>,
    /// Most recent trade's price (used as reference for slippage).
    latest_price: Decimal,
    /// Category from the latest trade's wallet.
    category: String,
    /// When the first trade in this window was observed.
    first_trade_at: DateTime<Utc>,
    /// When the latest trade was observed.
    latest_trade_at: DateTime<Utc>,
}

// ---------------------------------------------------------------------------
// Signal source implementation
// ---------------------------------------------------------------------------

/// Whale consensus copy-trading signal source.
pub struct WhaleSignalSource {
    /// Set of watched whale addresses (lowercase for O(1) lookup).
    watched: HashSet<String>,
    /// Active consensus windows: (market_id, token_id, side) -> window.
    windows: HashMap<(String, String, Side), ConsensusWindow>,
    /// Configuration.
    config: WhaleSignalConfig,
}

impl WhaleSignalSource {
    /// Create a new whale signal source.
    pub fn new(wallets: &[Wallet], config: WhaleSignalConfig) -> Self {
        let watched = wallets
            .iter()
            .map(|w| w.address.to_lowercase())
            .collect();

        Self {
            watched,
            windows: HashMap::new(),
            config,
        }
    }

    /// Determine confidence tier from whale count.
    fn tier_from_count(count: usize) -> ConfidenceTier {
        if count >= 3 {
            ConfidenceTier::High
        } else if count >= 2 {
            ConfidenceTier::Medium
        } else {
            ConfidenceTier::Low
        }
    }

    /// Get Kelly multiplier for a confidence tier.
    fn kelly_for_tier(&self, tier: ConfidenceTier) -> Decimal {
        match tier {
            ConfidenceTier::Low => self.config.kelly.low,
            ConfidenceTier::Medium => self.config.kelly.medium,
            ConfidenceTier::High => self.config.kelly.high,
        }
    }

    /// Build a Signal from a matured consensus window.
    fn emit_signal(
        &self,
        market_id: &str,
        token_id: &str,
        side: Side,
        window: &ConsensusWindow,
    ) -> Option<Signal> {
        let whale_count = window.distinct_wallets.len();

        if (whale_count as u32) < self.config.min_wallets {
            return None;
        }

        // Spread gate: skip prices too close to 0 or 1
        let min_spread = self.config.min_spread_from_resolution;
        if window.latest_price < min_spread
            || window.latest_price > (Decimal::ONE - min_spread)
        {
            return None;
        }

        let tier = Self::tier_from_count(whale_count);
        let kelly = self.kelly_for_tier(tier);
        let recommended_size = self.config.portfolio_value * self.config.max_position_pct * kelly;

        let total_watched = self.watched.len().max(1);
        let consensus_pct = Decimal::from(whale_count as u64)
            / Decimal::from(total_watched as u64);

        Some(Signal {
            signal_id: Uuid::new_v4(),
            basket_category: window.category.clone(),
            consensus_pct,
            confidence_tier: tier,
            kelly_multiplier: kelly,
            recommended_size,
            wallets_agreeing: whale_count as u32,
            market_id: market_id.to_string(),
            token_id: token_id.to_string(),
            side,
            detected_at: Utc::now(),
        })
    }
}

impl SignalSource for WhaleSignalSource {
    fn process_event(&mut self, trade: &Trade) -> Vec<Signal> {
        // Only process trades from watched wallets
        if !self.watched.contains(&trade.wallet_address.to_lowercase()) {
            return vec![];
        }

        let key = (
            trade.market_id.clone(),
            trade.token_id.clone(),
            trade.side,
        );

        let window = self.windows.entry(key).or_insert_with(|| ConsensusWindow {
            distinct_wallets: HashSet::new(),
            latest_price: trade.price,
            category: String::new(),
            first_trade_at: trade.timestamp,
            latest_trade_at: trade.timestamp,
        });

        window
            .distinct_wallets
            .insert(trade.wallet_address.to_lowercase());
        window.latest_price = trade.price;
        window.latest_trade_at = trade.timestamp;

        // Don't emit signals immediately — wait for tick() to let the
        // consensus window accumulate more whales.
        vec![]
    }

    fn tick(&mut self) -> Vec<Signal> {
        let now = Utc::now();
        let window_duration = Duration::seconds(self.config.consensus_window_secs);
        let mut signals = Vec::new();
        let mut expired_keys = Vec::new();

        for (key, window) in &self.windows {
            let age = now - window.first_trade_at;
            if age >= window_duration {
                if let Some(signal) =
                    self.emit_signal(&key.0, &key.1, key.2, window)
                {
                    signals.push(signal);
                }
                expired_keys.push(key.clone());
            }
        }

        // Remove emitted windows
        for key in expired_keys {
            self.windows.remove(&key);
        }

        signals
    }

    fn update_wallets(&mut self, wallets: Vec<Wallet>) {
        self.watched = wallets
            .iter()
            .map(|w| w.address.to_lowercase())
            .collect();
    }

    fn name(&self) -> &str {
        "whale_consensus"
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    fn test_wallet(address: &str) -> Wallet {
        Wallet {
            address: address.to_string(),
            category: "sports".to_string(),
            sharpe: dec!(2.0),
            kelly_fraction: dec!(0.15),
            rolling_wr: dec!(0.60),
            ev_per_trade: dec!(0.03),
            last_scored: Utc::now(),
        }
    }

    fn test_trade(wallet: &str, market: &str, side: Side, price: Decimal) -> Trade {
        Trade {
            wallet_address: wallet.to_string(),
            market_id: market.to_string(),
            token_id: "token_yes".to_string(),
            side,
            amount: dec!(100),
            price,
            timestamp: Utc::now(),
        }
    }

    fn test_config() -> WhaleSignalConfig {
        WhaleSignalConfig {
            consensus_window_secs: 0, // immediate for tests
            ..Default::default()
        }
    }

    #[test]
    fn ignores_non_whale_trades() {
        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        let trade = test_trade("0xRandom", "market1", Side::Buy, dec!(0.60));
        let signals = source.process_event(&trade);
        assert!(signals.is_empty());

        let tick_signals = source.tick();
        assert!(tick_signals.is_empty());
    }

    #[test]
    fn single_whale_emits_low_signal() {
        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        let trade = test_trade("0xwhale1", "market1", Side::Buy, dec!(0.60));
        source.process_event(&trade);

        let signals = source.tick();
        assert_eq!(signals.len(), 1);
        assert_eq!(signals[0].confidence_tier, ConfidenceTier::Low);
        assert_eq!(signals[0].kelly_multiplier, dec!(0.25));
        assert_eq!(signals[0].wallets_agreeing, 1);
    }

    #[test]
    fn two_whales_emit_medium_signal() {
        let wallets = vec![test_wallet("0xWhale1"), test_wallet("0xWhale2")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        source.process_event(&test_trade("0xwhale1", "market1", Side::Buy, dec!(0.60)));
        source.process_event(&test_trade("0xwhale2", "market1", Side::Buy, dec!(0.62)));

        let signals = source.tick();
        assert_eq!(signals.len(), 1);
        assert_eq!(signals[0].confidence_tier, ConfidenceTier::Medium);
        assert_eq!(signals[0].kelly_multiplier, dec!(0.50));
        assert_eq!(signals[0].wallets_agreeing, 2);
    }

    #[test]
    fn three_whales_emit_high_signal() {
        let wallets = vec![
            test_wallet("0xWhale1"),
            test_wallet("0xWhale2"),
            test_wallet("0xWhale3"),
        ];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        source.process_event(&test_trade("0xwhale1", "market1", Side::Buy, dec!(0.60)));
        source.process_event(&test_trade("0xwhale2", "market1", Side::Buy, dec!(0.61)));
        source.process_event(&test_trade("0xwhale3", "market1", Side::Buy, dec!(0.62)));

        let signals = source.tick();
        assert_eq!(signals.len(), 1);
        assert_eq!(signals[0].confidence_tier, ConfidenceTier::High);
        assert_eq!(signals[0].kelly_multiplier, dec!(0.75));
        assert_eq!(signals[0].wallets_agreeing, 3);
    }

    #[test]
    fn spread_gate_filters_near_resolution() {
        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        // Price too close to 1.0
        source.process_event(&test_trade("0xwhale1", "market1", Side::Buy, dec!(0.97)));
        let signals = source.tick();
        assert!(signals.is_empty());
    }

    #[test]
    fn spread_gate_filters_near_zero() {
        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        // Price too close to 0.0
        source.process_event(&test_trade("0xwhale1", "market1", Side::Buy, dec!(0.03)));
        let signals = source.tick();
        assert!(signals.is_empty());
    }

    #[test]
    fn different_markets_create_separate_signals() {
        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        source.process_event(&test_trade("0xwhale1", "market1", Side::Buy, dec!(0.60)));
        source.process_event(&test_trade("0xwhale1", "market2", Side::Sell, dec!(0.40)));

        let signals = source.tick();
        assert_eq!(signals.len(), 2);
    }

    #[test]
    fn window_cleared_after_tick() {
        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        source.process_event(&test_trade("0xwhale1", "market1", Side::Buy, dec!(0.60)));
        let signals = source.tick();
        assert_eq!(signals.len(), 1);

        // Second tick should be empty — window was consumed
        let signals = source.tick();
        assert!(signals.is_empty());
    }

    #[test]
    fn update_wallets_changes_watched_set() {
        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        // Initially, 0xWhale2 is not watched
        source.process_event(&test_trade("0xwhale2", "market1", Side::Buy, dec!(0.60)));
        assert!(source.tick().is_empty());

        // Update wallets to include 0xWhale2
        source.update_wallets(vec![test_wallet("0xWhale2")]);
        source.process_event(&test_trade("0xwhale2", "market1", Side::Buy, dec!(0.60)));
        let signals = source.tick();
        assert_eq!(signals.len(), 1);
    }

    #[test]
    fn recommended_size_uses_kelly() {
        let mut config = test_config();
        config.portfolio_value = dec!(10000);
        config.max_position_pct = dec!(0.03);

        let wallets = vec![test_wallet("0xWhale1")];
        let mut source = WhaleSignalSource::new(&wallets, config);

        source.process_event(&test_trade("0xwhale1", "market1", Side::Buy, dec!(0.60)));
        let signals = source.tick();

        // 10000 * 0.03 * 0.25 (LOW kelly) = 75
        assert_eq!(signals[0].recommended_size, dec!(75));
    }

    #[test]
    fn case_insensitive_wallet_matching() {
        let wallets = vec![test_wallet("0xAbCdEf")];
        let mut source = WhaleSignalSource::new(&wallets, test_config());

        source.process_event(&test_trade("0xABCDEF", "market1", Side::Buy, dec!(0.60)));
        let signals = source.tick();
        assert_eq!(signals.len(), 1);
    }
}
