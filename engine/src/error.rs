//! Unified error types for the engine.

use crate::risk_manager::RiskDenial;

/// Top-level engine error.
#[derive(Debug, thiserror::Error)]
pub enum EngineError {
    /// WebSocket connection or protocol error.
    #[error("WebSocket error: {0}")]
    WebSocket(#[from] tokio_tungstenite::tungstenite::Error),

    /// JSON parsing error on the hot path.
    #[error("JSON parse error: {0}")]
    JsonParse(String),

    /// CLOB REST API returned an error.
    #[error("CLOB API error {status}: {message}")]
    ClobApi { status: u16, message: String },

    /// EIP-712 signing failed.
    #[error("Signing error: {0}")]
    Signing(String),

    /// HTTP request failed.
    #[error("HTTP error: {0}")]
    Http(#[from] reqwest::Error),

    /// Configuration loading failed.
    #[error("Config error: {0}")]
    Config(String),

    /// Trade was denied by the risk manager.
    #[error("Risk denied: {0}")]
    RiskDenied(#[from] RiskDenial),

    /// Kill switch was activated.
    #[error("Kill switch: {0}")]
    KillSwitch(String),
}
