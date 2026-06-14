//! Configuration loading from TOML files and environment variables.
//!
//! The engine reads its config from `config/engine.toml` for all trading
//! parameters, and from environment variables (via `.env`) for secrets
//! like private keys and API credentials.

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use serde::Deserialize;
use std::path::Path;

/// Top-level engine configuration.
#[derive(Debug, Clone, Deserialize)]
pub struct EngineConfig {
    /// Risk management thresholds.
    pub risk: RiskConfig,
    /// Kelly criterion tier multipliers.
    pub kelly: KellyConfig,
    /// Execution parameters.
    pub execution: ExecutionConfig,
    /// Connection endpoints.
    pub connection: ConnectionConfig,
}

/// Risk management parameters — the five-layer safety system.
#[derive(Debug, Clone, Deserialize)]
pub struct RiskConfig {
    /// Max fraction of portfolio per trade (default: 0.03 = 3%).
    #[serde(default = "default_max_position_pct")]
    pub max_position_pct: Decimal,
    /// Max concurrent open positions (default: 8).
    #[serde(default = "default_max_open_positions")]
    pub max_open_positions: u32,
    /// Max positions in a single category (default: 3).
    #[serde(default = "default_max_per_category")]
    pub max_per_category: u32,
    /// Max total exposure as fraction of portfolio (default: 0.25 = 25%).
    #[serde(default = "default_total_exposure_cap")]
    pub total_exposure_cap: Decimal,
    /// Daily loss halt threshold as fraction (default: 0.03 = 3%).
    #[serde(default = "default_daily_loss_stop_pct")]
    pub daily_loss_stop_pct: Decimal,
    /// Drawdown from peak equity that triggers shutdown (default: 0.10 = 10%).
    #[serde(default = "default_drawdown_breaker_pct")]
    pub drawdown_breaker_pct: Decimal,
}

/// Kelly criterion multipliers by confidence tier.
#[derive(Debug, Clone, Deserialize)]
pub struct KellyConfig {
    /// Multiplier for LOW confidence signals (60-69% consensus).
    #[serde(default = "default_kelly_low")]
    pub low: Decimal,
    /// Multiplier for MEDIUM confidence signals (70-79% consensus).
    #[serde(default = "default_kelly_medium")]
    pub medium: Decimal,
    /// Multiplier for HIGH confidence signals (80%+ consensus).
    #[serde(default = "default_kelly_high")]
    pub high: Decimal,
}

/// Execution and slippage parameters.
#[derive(Debug, Clone, Deserialize)]
pub struct ExecutionConfig {
    /// Skip trade if price moved more than this from whale entry (default: 0.03).
    #[serde(default = "default_max_slippage")]
    pub max_slippage_from_whale: Decimal,
    /// Don't enter if market is within this of resolution price (default: 0.05).
    #[serde(default = "default_min_spread")]
    pub min_spread_from_resolution: Decimal,
}

/// Network connection endpoints.
#[derive(Debug, Clone, Deserialize)]
pub struct ConnectionConfig {
    /// CLOB REST API base URL.
    #[serde(default = "default_clob_url")]
    pub clob_url: String,
    /// CLOB WebSocket URL.
    #[serde(default = "default_ws_url")]
    pub ws_url: String,
    /// Polygon chain ID.
    #[serde(default = "default_chain_id")]
    pub chain_id: u64,
}

/// Wallet credentials loaded from environment only (never from config files).
#[derive(Debug, Clone)]
pub struct WalletConfig {
    /// Polygon private key for EIP-712 signing.
    pub private_key: String,
    /// CLOB API key (optional, for authenticated endpoints).
    pub clob_api_key: Option<String>,
    /// CLOB API secret.
    pub clob_api_secret: Option<String>,
    /// CLOB API passphrase.
    pub clob_api_passphrase: Option<String>,
}

// --- Default value functions for serde ---

fn default_max_position_pct() -> Decimal { dec!(0.03) }
fn default_max_open_positions() -> u32 { 8 }
fn default_max_per_category() -> u32 { 3 }
fn default_total_exposure_cap() -> Decimal { dec!(0.25) }
fn default_daily_loss_stop_pct() -> Decimal { dec!(0.03) }
fn default_drawdown_breaker_pct() -> Decimal { dec!(0.10) }
fn default_kelly_low() -> Decimal { dec!(0.25) }
fn default_kelly_medium() -> Decimal { dec!(0.50) }
fn default_kelly_high() -> Decimal { dec!(0.75) }
fn default_max_slippage() -> Decimal { dec!(0.03) }
fn default_min_spread() -> Decimal { dec!(0.05) }
fn default_clob_url() -> String { "https://clob.polymarket.com".to_string() }
fn default_ws_url() -> String { "wss://ws-subscriptions-clob.polymarket.com/ws/".to_string() }
fn default_chain_id() -> u64 { 137 }

/// Load engine configuration from a TOML file.
///
/// Falls back to default values for any missing fields.
///
/// # Errors
///
/// Returns an error if the file cannot be read or contains invalid TOML.
pub fn load_engine_config(path: &Path) -> anyhow::Result<EngineConfig> {
    let contents = std::fs::read_to_string(path)?;
    let config: EngineConfig = toml::from_str(&contents)?;
    Ok(config)
}

/// Load wallet credentials from environment variables.
///
/// The private key is required; CLOB API credentials are optional.
///
/// # Errors
///
/// Returns an error if `PRIVATE_KEY` is not set.
pub fn load_wallet_config() -> anyhow::Result<WalletConfig> {
    dotenvy::dotenv().ok();

    let private_key = std::env::var("PRIVATE_KEY")
        .map_err(|_| anyhow::anyhow!("PRIVATE_KEY environment variable is required"))?;

    Ok(WalletConfig {
        private_key,
        clob_api_key: std::env::var("CLOB_API_KEY").ok(),
        clob_api_secret: std::env::var("CLOB_API_SECRET").ok(),
        clob_api_passphrase: std::env::var("CLOB_API_PASSPHRASE").ok(),
    })
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn test_load_default_config() {
        let toml_str = r#"
[risk]
[kelly]
[execution]
[connection]
"#;
        let config: EngineConfig = toml::from_str(toml_str).unwrap();

        assert_eq!(config.risk.max_position_pct, dec!(0.03));
        assert_eq!(config.risk.max_open_positions, 8);
        assert_eq!(config.risk.max_per_category, 3);
        assert_eq!(config.risk.total_exposure_cap, dec!(0.25));
        assert_eq!(config.risk.daily_loss_stop_pct, dec!(0.03));
        assert_eq!(config.risk.drawdown_breaker_pct, dec!(0.10));
        assert_eq!(config.kelly.low, dec!(0.25));
        assert_eq!(config.kelly.medium, dec!(0.50));
        assert_eq!(config.kelly.high, dec!(0.75));
        assert_eq!(config.execution.max_slippage_from_whale, dec!(0.03));
        assert_eq!(config.execution.min_spread_from_resolution, dec!(0.05));
        assert_eq!(config.connection.chain_id, 137);
    }

    #[test]
    fn test_load_custom_config() {
        let toml_str = r#"
[risk]
max_position_pct = "0.05"
max_open_positions = 4

[kelly]
low = "0.10"

[execution]
max_slippage_from_whale = "0.02"

[connection]
chain_id = 80001
"#;
        let config: EngineConfig = toml::from_str(toml_str).unwrap();

        assert_eq!(config.risk.max_position_pct, dec!(0.05));
        assert_eq!(config.risk.max_open_positions, 4);
        // Unchanged fields still use defaults
        assert_eq!(config.risk.max_per_category, 3);
        assert_eq!(config.kelly.low, dec!(0.10));
        assert_eq!(config.kelly.medium, dec!(0.50));
        assert_eq!(config.connection.chain_id, 80001);
    }

    #[test]
    fn test_load_config_from_file() {
        let config_path = Path::new(env!("CARGO_MANIFEST_DIR"))
            .parent()
            .unwrap()
            .join("config/engine.toml");

        if config_path.exists() {
            let config = load_engine_config(&config_path).unwrap();
            // The shipped config overrides the 0.03 struct default with the
            // deliberate aggressive-v2 value (0.10); assert the real file value.
            assert_eq!(config.risk.max_position_pct, dec!(0.10));
        }
    }
}
