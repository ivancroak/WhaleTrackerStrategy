//! EIP-712 order signing and CLOB API order placement.
//!
//! Signs Polymarket CTF Exchange orders using alloy's `sol!` macro for
//! EIP-712 typed data hashing. Posts signed orders to the CLOB REST API
//! with HMAC-SHA256 authenticated headers.
//!
//! Reference: <https://github.com/Polymarket/rs-clob-client>

use std::time::{SystemTime, UNIX_EPOCH};

use alloy::primitives::{Address, U256};
use alloy::signers::local::PrivateKeySigner;
use alloy::signers::Signer;
use alloy::sol;
use alloy::sol_types::{eip712_domain, SolStruct};
use base64::engine::general_purpose::URL_SAFE;
use base64::Engine as _;
use hmac::{Hmac, Mac};
use rand::Rng;
use reqwest::header::{HeaderMap, HeaderValue};
use rust_decimal::prelude::ToPrimitive;
use rust_decimal::Decimal;
use sha2::Sha256;

use crate::clob_types::{
    FeeRateResponse, NegRiskResponse, OrderRequest, OrderResponse, OrderResult,
    SignedOrderPayload, CTF_EXCHANGE, NEG_RISK_CTF_EXCHANGE,
};
use crate::config::{ConnectionConfig, WalletConfig};
use crate::error::EngineError;
use crate::models::Side;

// ---------------------------------------------------------------------------
// EIP-712 Order struct (matches Polymarket CTF Exchange contract)
// ---------------------------------------------------------------------------

sol! {
    /// EIP-712 typed struct for Polymarket CLOB orders.
    /// Field order MUST match the contract's typeHash exactly.
    #[derive(Debug)]
    struct Order {
        uint256 salt;
        address maker;
        address signer;
        address taker;
        uint256 tokenId;
        uint256 makerAmount;
        uint256 takerAmount;
        uint256 expiration;
        uint256 nonce;
        uint256 feeRateBps;
        uint8   side;
        uint8   signatureType;
    }
}

/// Zero address — used as taker for public orders.
const ZERO_ADDRESS: &str = "0x0000000000000000000000000000000000000000";

/// Fixed-point scale for USDC (6 decimals).
const USDC_DECIMALS: u32 = 6;

// ---------------------------------------------------------------------------
// Executor
// ---------------------------------------------------------------------------

/// CLOB order executor with EIP-712 signing and HMAC authentication.
pub struct ClobExecutor {
    signer: PrivateKeySigner,
    signer_address: Address,
    http: reqwest::Client,
    clob_url: String,
    chain_id: u64,
    api_key: String,
    api_secret: String,
    api_passphrase: String,
}

impl ClobExecutor {
    /// Create a new executor from wallet and connection config.
    pub fn new(wallet: &WalletConfig, connection: &ConnectionConfig) -> Result<Self, EngineError> {
        let signer: PrivateKeySigner = wallet
            .private_key
            .parse()
            .map_err(|e| EngineError::Signing(format!("Invalid private key: {e}")))?;

        let signer_address = signer.address();

        let api_key = wallet
            .clob_api_key
            .clone()
            .ok_or_else(|| EngineError::Config("CLOB_API_KEY not set".into()))?;
        let api_secret = wallet
            .clob_api_secret
            .clone()
            .ok_or_else(|| EngineError::Config("CLOB_API_SECRET not set".into()))?;
        let api_passphrase = wallet
            .clob_api_passphrase
            .clone()
            .ok_or_else(|| EngineError::Config("CLOB_API_PASSPHRASE not set".into()))?;

        Ok(Self {
            signer,
            signer_address,
            http: reqwest::Client::new(),
            clob_url: connection.clob_url.clone(),
            chain_id: connection.chain_id,
            api_key,
            api_secret,
            api_passphrase,
        })
    }

    /// Sign and post a GTC limit order to the CLOB API.
    pub async fn place_limit_order(
        &self,
        token_id: &str,
        side: Side,
        price: Decimal,
        size: Decimal,
    ) -> Result<OrderResult, EngineError> {
        let start = std::time::Instant::now();

        // Pre-flight: check neg-risk and fee rate
        let neg_risk = self.check_neg_risk(token_id).await?;
        let fee_rate_bps = self.get_fee_rate(token_id).await?;

        // Select the correct exchange contract for EIP-712 domain
        let exchange_address: Address = if neg_risk {
            NEG_RISK_CTF_EXCHANGE.parse().unwrap()
        } else {
            CTF_EXCHANGE.parse().unwrap()
        };

        // Calculate maker/taker amounts (6-decimal fixed point)
        let (maker_amount, taker_amount) = Self::compute_amounts(side, price, size);

        // Generate random salt (masked to 2^53 - 1 for IEEE 754 safety)
        let salt: u64 = {
            let mut rng = rand::thread_rng();
            rng.r#gen::<u64>() & ((1u64 << 53) - 1)
        };

        // Build EIP-712 order struct
        let order = Order {
            salt: U256::from(salt),
            maker: self.signer_address,
            signer: self.signer_address,
            taker: ZERO_ADDRESS.parse().unwrap(),
            tokenId: U256::from_str_radix(token_id, 10)
                .unwrap_or(U256::ZERO),
            makerAmount: U256::from(maker_amount),
            takerAmount: U256::from(taker_amount),
            expiration: U256::ZERO,
            nonce: U256::ZERO,
            feeRateBps: U256::from(fee_rate_bps),
            side: match side {
                Side::Buy => 0u8,
                Side::Sell => 1u8,
            },
            signatureType: 0u8, // EOA
        };

        // Build EIP-712 domain
        let domain = eip712_domain! {
            name: "Polymarket CTF Exchange",
            version: "1",
            chain_id: self.chain_id,
            verifying_contract: exchange_address,
        };

        // Compute EIP-712 signing hash
        let signing_hash = order.eip712_signing_hash(&domain);

        // Sign
        let signature = self
            .signer
            .sign_hash(&signing_hash)
            .await
            .map_err(|e| EngineError::Signing(format!("Sign failed: {e}")))?;

        let sig_hex = format!("0x{}", alloy::hex::encode(signature.as_bytes()));

        // Build API request
        let side_str = match side {
            Side::Buy => "BUY",
            Side::Sell => "SELL",
        };

        let payload = SignedOrderPayload {
            salt,
            maker: format!("{:?}", self.signer_address),
            signer: format!("{:?}", self.signer_address),
            taker: ZERO_ADDRESS.to_string(),
            token_id: token_id.to_string(),
            maker_amount: maker_amount.to_string(),
            taker_amount: taker_amount.to_string(),
            expiration: "0".to_string(),
            nonce: "0".to_string(),
            fee_rate_bps: fee_rate_bps.to_string(),
            side: side_str.to_string(),
            signature_type: 0,
            signature: sig_hex,
        };

        let request = OrderRequest {
            order: payload,
            order_type: "GTC".to_string(),
            owner: self.api_key.clone(),
        };

        // Post to CLOB API
        let body = serde_json::to_string(&request)
            .map_err(|e| EngineError::JsonParse(e.to_string()))?;

        let url = format!("{}/order", self.clob_url);
        let headers = self.build_hmac_headers("POST", "/order", &body);

        let resp = self
            .http
            .post(&url)
            .headers(headers)
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?;

        let status = resp.status().as_u16();
        let resp_body: OrderResponse = resp.json().await.map_err(|e| EngineError::ClobApi {
            status,
            message: format!("Failed to parse response: {e}"),
        })?;

        let latency = start.elapsed();

        if resp_body.success {
            Ok(OrderResult {
                signal_id: uuid::Uuid::nil(), // Filled by engine
                success: true,
                order_id: resp_body.order_id.unwrap_or_default(),
                side,
                token_id: token_id.to_string(),
                price,
                size,
                reason: format!("Order placed ({})", resp_body.status),
                timestamp: chrono::Utc::now(),
                latency_us: latency.as_micros() as u64,
            })
        } else {
            Err(EngineError::ClobApi {
                status,
                message: resp_body
                    .error_msg
                    .unwrap_or_else(|| "Unknown error".into()),
            })
        }
    }

    /// Cancel an order by ID.
    pub async fn cancel_order(&self, order_id: &str) -> Result<(), EngineError> {
        let body = serde_json::json!({ "orderID": order_id }).to_string();
        let url = format!("{}/order", self.clob_url);
        let headers = self.build_hmac_headers("DELETE", "/order", &body);

        let resp = self
            .http
            .delete(&url)
            .headers(headers)
            .header("Content-Type", "application/json")
            .body(body)
            .send()
            .await?;

        if resp.status().is_success() {
            Ok(())
        } else {
            let status = resp.status().as_u16();
            let text = resp.text().await.unwrap_or_default();
            Err(EngineError::ClobApi {
                status,
                message: text,
            })
        }
    }

    // -----------------------------------------------------------------------
    // Pre-flight API calls
    // -----------------------------------------------------------------------

    /// Check if a token is in a neg-risk market.
    async fn check_neg_risk(&self, token_id: &str) -> Result<bool, EngineError> {
        let url = format!("{}/neg-risk?token_id={}", self.clob_url, token_id);
        let resp: NegRiskResponse = self.http.get(&url).send().await?.json().await?;
        Ok(resp.neg_risk)
    }

    /// Get the current fee rate for a token.
    async fn get_fee_rate(&self, token_id: &str) -> Result<u32, EngineError> {
        let url = format!("{}/fee-rate?token_id={}", self.clob_url, token_id);
        let resp: FeeRateResponse = self.http.get(&url).send().await?.json().await?;
        Ok(resp.base_fee)
    }

    // -----------------------------------------------------------------------
    // Amount calculation
    // -----------------------------------------------------------------------

    /// Compute maker and taker amounts for a limit order.
    ///
    /// Returns (maker_amount, taker_amount) as 6-decimal fixed-point integers.
    ///
    /// - BUY: maker pays USDC (size * price), taker gives shares (size)
    /// - SELL: maker gives shares (size), taker pays USDC (size * price)
    fn compute_amounts(side: Side, price: Decimal, size: Decimal) -> (u128, u128) {
        let scale = Decimal::from(10u64.pow(USDC_DECIMALS));

        match side {
            Side::Buy => {
                let taker_amount = (size * scale).trunc().to_u128().unwrap_or(0);
                let maker_amount = (size * price * scale).trunc().to_u128().unwrap_or(0);
                (maker_amount, taker_amount)
            }
            Side::Sell => {
                let maker_amount = (size * scale).trunc().to_u128().unwrap_or(0);
                let taker_amount = (size * price * scale).trunc().to_u128().unwrap_or(0);
                (maker_amount, taker_amount)
            }
        }
    }

    // -----------------------------------------------------------------------
    // HMAC authentication
    // -----------------------------------------------------------------------

    /// Build L2 HMAC authentication headers for the CLOB API.
    fn build_hmac_headers(&self, method: &str, path: &str, body: &str) -> HeaderMap {
        let timestamp = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .unwrap()
            .as_secs()
            .to_string();

        let message = format!("{timestamp}{method}{path}{body}");

        let decoded_secret = URL_SAFE
            .decode(&self.api_secret)
            .unwrap_or_default();

        let mut mac =
            Hmac::<Sha256>::new_from_slice(&decoded_secret).expect("HMAC accepts any key size");
        mac.update(message.as_bytes());
        let signature = URL_SAFE.encode(mac.finalize().into_bytes());

        let mut headers = HeaderMap::new();
        headers.insert(
            "POLY_ADDRESS",
            HeaderValue::from_str(&format!("{:?}", self.signer_address)).unwrap(),
        );
        headers.insert(
            "POLY_API_KEY",
            HeaderValue::from_str(&self.api_key).unwrap(),
        );
        headers.insert(
            "POLY_PASSPHRASE",
            HeaderValue::from_str(&self.api_passphrase).unwrap(),
        );
        headers.insert(
            "POLY_SIGNATURE",
            HeaderValue::from_str(&signature).unwrap(),
        );
        headers.insert(
            "POLY_TIMESTAMP",
            HeaderValue::from_str(&timestamp).unwrap(),
        );
        headers
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use rust_decimal_macros::dec;

    #[test]
    fn test_compute_amounts_buy() {
        // Buy 100 shares at $0.34 → maker pays 34 USDC, taker gives 100 shares
        let (maker, taker) = ClobExecutor::compute_amounts(Side::Buy, dec!(0.34), dec!(100));
        assert_eq!(maker, 34_000_000); // 34 USDC in 6-decimal
        assert_eq!(taker, 100_000_000); // 100 shares in 6-decimal
    }

    #[test]
    fn test_compute_amounts_sell() {
        // Sell 100 shares at $0.65 → maker gives 100 shares, taker pays 65 USDC
        let (maker, taker) = ClobExecutor::compute_amounts(Side::Sell, dec!(0.65), dec!(100));
        assert_eq!(maker, 100_000_000); // 100 shares
        assert_eq!(taker, 65_000_000); // 65 USDC
    }

    #[test]
    fn test_compute_amounts_fractional() {
        // Buy 2.5 shares at $0.50
        let (maker, taker) = ClobExecutor::compute_amounts(Side::Buy, dec!(0.50), dec!(2.5));
        assert_eq!(maker, 1_250_000); // 1.25 USDC
        assert_eq!(taker, 2_500_000); // 2.5 shares
    }
}
