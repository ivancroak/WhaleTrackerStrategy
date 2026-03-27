//! Polymarket CLOB API request and response types.
//!
//! These types model the JSON payloads for the CLOB REST API order endpoints.
//! The EIP-712 signing types live in [`crate::executor`].

use chrono::{DateTime, Utc};
use rust_decimal::Decimal;
use serde::{Deserialize, Serialize};
use uuid::Uuid;

use crate::models::Side;

// ---------------------------------------------------------------------------
// Contract addresses (Polygon Mainnet, Chain ID 137)
// ---------------------------------------------------------------------------

/// CTF Exchange contract address.
pub const CTF_EXCHANGE: &str = "0x4bFb41d5B3570DeFd03C39a9A4D8dE6Bd8B8982E";

/// Neg Risk CTF Exchange contract address.
pub const NEG_RISK_CTF_EXCHANGE: &str = "0xC5d563A36AE78145C45a50134d48A1215220f80a";

// ---------------------------------------------------------------------------
// WebSocket message types
// ---------------------------------------------------------------------------

/// Raw trade event from the CLOB WebSocket feed.
#[derive(Debug, Clone, Deserialize)]
pub struct WsTrade {
    /// Condition ID / market identifier.
    pub market: String,
    /// CLOB token ID (ERC-1155 asset).
    pub asset_id: String,
    /// "BUY" or "SELL".
    pub side: String,
    /// Trade price as string (parsed to Decimal downstream).
    pub price: String,
    /// Trade size as string.
    pub size: String,
    /// Maker address.
    #[serde(default)]
    pub maker_address: String,
    /// Taker address.
    #[serde(default)]
    pub taker_address: String,
    /// Timestamp string.
    #[serde(default)]
    pub timestamp: String,
    /// On-chain transaction hash.
    #[serde(default)]
    pub transaction_hash: String,
}

/// Envelope for WebSocket messages.
#[derive(Debug, Deserialize)]
pub struct WsMessage {
    /// Event type (e.g., "trade", "price_change").
    #[serde(default)]
    pub event_type: String,
    /// Nested trade data (present when event_type = trade).
    #[serde(flatten)]
    pub data: serde_json::Value,
}

// ---------------------------------------------------------------------------
// CLOB REST API types
// ---------------------------------------------------------------------------

/// Order submission request body for `POST /order`.
#[derive(Debug, Serialize)]
pub struct OrderRequest {
    /// The signed order.
    pub order: SignedOrderPayload,
    /// Order type: "GTC", "GTD", "FOK", "FAK".
    #[serde(rename = "orderType")]
    pub order_type: String,
    /// API key UUID (owner).
    pub owner: String,
}

/// The signed order payload as expected by the CLOB API.
///
/// Note: `salt` is serialized as a JSON number (not string) per Polymarket spec.
/// All other uint256 fields are serialized as strings.
#[derive(Debug, Serialize)]
pub struct SignedOrderPayload {
    pub salt: u64,
    pub maker: String,
    pub signer: String,
    pub taker: String,
    #[serde(rename = "tokenId")]
    pub token_id: String,
    #[serde(rename = "makerAmount")]
    pub maker_amount: String,
    #[serde(rename = "takerAmount")]
    pub taker_amount: String,
    pub expiration: String,
    pub nonce: String,
    #[serde(rename = "feeRateBps")]
    pub fee_rate_bps: String,
    /// "BUY" or "SELL" (string in JSON, uint8 in EIP-712).
    pub side: String,
    #[serde(rename = "signatureType")]
    pub signature_type: u8,
    /// Hex-encoded EIP-712 signature.
    pub signature: String,
}

/// Response from `POST /order`.
#[derive(Debug, Deserialize)]
pub struct OrderResponse {
    /// Whether the order was accepted.
    #[serde(default)]
    pub success: bool,
    /// Order ID (UUID).
    #[serde(rename = "orderID")]
    pub order_id: Option<String>,
    /// Error message if failed.
    #[serde(rename = "errorMsg")]
    pub error_msg: Option<String>,
    /// Order status (e.g., "LIVE", "MATCHED").
    #[serde(default)]
    pub status: String,
}

/// Response from `GET /neg-risk?token_id=X`.
#[derive(Debug, Deserialize)]
pub struct NegRiskResponse {
    pub neg_risk: bool,
}

/// Response from `GET /tick-size?token_id=X`.
#[derive(Debug, Deserialize)]
pub struct TickSizeResponse {
    pub minimum_tick_size: String,
}

/// Response from `GET /fee-rate?token_id=X`.
#[derive(Debug, Deserialize)]
pub struct FeeRateResponse {
    #[serde(default)]
    pub base_fee: u32,
}

// ---------------------------------------------------------------------------
// Engine-internal order result
// ---------------------------------------------------------------------------

/// Result of an order execution attempt (logged to trades.jsonl).
#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct OrderResult {
    /// Signal ID for audit trail.
    pub signal_id: Uuid,
    /// Whether the order was placed successfully.
    pub success: bool,
    /// CLOB order ID.
    #[serde(default)]
    pub order_id: String,
    /// Trade side.
    pub side: Side,
    /// CLOB token ID.
    pub token_id: String,
    /// Execution price.
    pub price: Decimal,
    /// Position size in USDC.
    pub size: Decimal,
    /// Human-readable reason.
    pub reason: String,
    /// When the order was placed.
    pub timestamp: DateTime<Utc>,
    /// Tick-to-trade latency in microseconds.
    #[serde(default)]
    pub latency_us: u64,
}
