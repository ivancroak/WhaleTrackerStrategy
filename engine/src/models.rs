//! Domain types for the whale copy trading engine.
//!
//! All monetary values use [`rust_decimal::Decimal`] — NEVER `f64` for money.
//! These types are serialization-compatible with the Python Pydantic models
//! in `analysis/src/whale_tracker/models.py`.

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

/// A scored whale wallet from the analysis layer.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Wallet {
    /// Ethereum address (checksummed).
    pub address: String,
    /// Market category this wallet specializes in (e.g., "crypto", "sports").
    pub category: String,
    /// Sharpe ratio — risk-adjusted return metric. Threshold: >1.5.
    pub sharpe: Decimal,
    /// Kelly fraction — optimal bet size from edge/odds formula.
    pub kelly_fraction: Decimal,
    /// 30-trade rolling win rate. Stop copying if <0.52.
    pub rolling_wr: Decimal,
    /// Expected value per trade as fraction of position size. Threshold: >0.01.
    pub ev_per_trade: Decimal,
    /// When this wallet was last scored by the analysis layer.
    pub last_scored: DateTime<Utc>,
}

/// Which side of a binary market.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Hash, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum Side {
    /// Buying YES tokens.
    Buy,
    /// Buying NO tokens (equivalent to selling YES).
    Sell,
}

/// A single trade observed on the CLOB WebSocket feed.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Trade {
    /// Wallet address that executed the trade.
    pub wallet_address: String,
    /// Polymarket condition ID / market identifier.
    pub market_id: String,
    /// CLOB token ID for the specific outcome.
    pub token_id: String,
    /// Buy or sell.
    pub side: Side,
    /// Trade amount in USDC.
    pub amount: Decimal,
    /// Execution price (0.0 to 1.0 range).
    pub price: Decimal,
    /// When the trade was observed.
    pub timestamp: DateTime<Utc>,
}

/// Signal confidence tier — determines Kelly multiplier.
#[derive(Debug, Clone, Copy, PartialEq, Eq, Serialize, Deserialize)]
#[serde(rename_all = "UPPERCASE")]
pub enum ConfidenceTier {
    /// 60-69% basket consensus → quarter-Kelly (0.25).
    Low,
    /// 70-79% basket consensus → half-Kelly (0.50).
    Medium,
    /// 80%+ basket consensus → three-quarter Kelly (0.75).
    High,
}

/// A consensus signal detected from a wallet basket.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Signal {
    /// Unique identifier for audit trail.
    pub signal_id: Uuid,
    /// Category of the basket that generated this signal.
    pub basket_category: String,
    /// Fraction of basket wallets that agree (0.0 to 1.0).
    pub consensus_pct: Decimal,
    /// Derived confidence tier.
    pub confidence_tier: ConfidenceTier,
    /// Kelly multiplier for this tier (0.25 / 0.50 / 0.75).
    pub kelly_multiplier: Decimal,
    /// Recommended position size in USDC.
    pub recommended_size: Decimal,
    /// How many wallets in the basket agree on this trade.
    pub wallets_agreeing: u32,
    /// Target market.
    pub market_id: String,
    /// CLOB token ID for the specific outcome.
    pub token_id: String,
    /// Trade direction.
    pub side: Side,
    /// When the signal was detected.
    pub detected_at: DateTime<Utc>,
}

/// An open position held by the engine.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Position {
    /// Polymarket condition ID / market identifier.
    pub market_id: String,
    /// CLOB token ID for the specific outcome.
    pub token_id: String,
    /// Buy or sell.
    pub side: Side,
    /// Price at which the position was entered.
    pub entry_price: Decimal,
    /// Position size in USDC.
    pub size: Decimal,
    /// Market category — used by risk manager for per-category limits.
    pub category: String,
    /// When the position was opened.
    pub opened_at: DateTime<Utc>,
    /// Signal ID that triggered this position.
    pub source_signal: Uuid,
}

/// Real-time risk state tracked by the risk manager.
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct RiskState {
    /// Current total portfolio value in USDC.
    pub portfolio_value: Decimal,
    /// Highest portfolio value seen (for drawdown calculation).
    pub peak_equity: Decimal,
    /// Today's cumulative P&L in USDC.
    pub daily_pnl: Decimal,
    /// Currently open positions.
    pub open_positions: Vec<Position>,
    /// Sum of all open position sizes as fraction of portfolio.
    pub total_exposure: Decimal,
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_side_serialization() {
        let buy = Side::Buy;
        let json = serde_json::to_string(&buy).unwrap();
        assert_eq!(json, r#""BUY""#);

        let sell: Side = serde_json::from_str(r#""SELL""#).unwrap();
        assert_eq!(sell, Side::Sell);
    }

    #[test]
    fn test_confidence_tier_serialization() {
        let tier = ConfidenceTier::High;
        let json = serde_json::to_string(&tier).unwrap();
        assert_eq!(json, r#""HIGH""#);

        let low: ConfidenceTier = serde_json::from_str(r#""LOW""#).unwrap();
        assert_eq!(low, ConfidenceTier::Low);
    }

    #[test]
    fn test_wallet_round_trip() {
        let wallet = Wallet {
            address: "0xAbC123".to_string(),
            category: "crypto".to_string(),
            sharpe: dec!(2.1),
            kelly_fraction: dec!(0.15),
            rolling_wr: dec!(0.65),
            ev_per_trade: dec!(0.03),
            last_scored: Utc::now(),
        };

        let json = serde_json::to_string(&wallet).unwrap();
        let decoded: Wallet = serde_json::from_str(&json).unwrap();
        assert_eq!(decoded.address, wallet.address);
        assert_eq!(decoded.sharpe, dec!(2.1));
    }

    #[test]
    fn test_risk_state_defaults() {
        let state = RiskState {
            portfolio_value: dec!(15000),
            peak_equity: dec!(15000),
            daily_pnl: dec!(0),
            open_positions: vec![],
            total_exposure: dec!(0),
        };

        assert!(state.open_positions.is_empty());
        assert_eq!(state.total_exposure, dec!(0));
    }
}
