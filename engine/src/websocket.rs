//! CLOB WebSocket client with auto-reconnect.
//!
//! Connects to Polymarket's CLOB WebSocket feed, subscribes to trade events,
//! and yields parsed [`WsTrade`] structs as an async stream.

use std::time::Duration;

use futures_util::{SinkExt, StreamExt};
use tokio::time::sleep;
use tokio_tungstenite::{connect_async, tungstenite::Message};

use crate::clob_types::WsTrade;
use crate::error::EngineError;

/// CLOB WebSocket client.
pub struct ClobWebSocket {
    ws_url: String,
    /// Asset IDs to subscribe to (empty = subscribe to all via market channel).
    assets: Vec<String>,
}

impl ClobWebSocket {
    /// Create a new WebSocket client.
    pub fn new(ws_url: String, assets: Vec<String>) -> Self {
        Self { ws_url, assets }
    }

    /// Connect to the WebSocket and return a receiver for trade events.
    ///
    /// Spawns a background task that handles reconnection with exponential
    /// backoff. Trade events are sent through the returned channel.
    /// The kill_tx sender is used to signal fatal errors to the engine.
    pub fn spawn_listener(
        &self,
        trade_tx: tokio::sync::mpsc::Sender<WsTrade>,
        kill_tx: tokio::sync::mpsc::Sender<String>,
    ) -> tokio::task::JoinHandle<()> {
        let ws_url = self.ws_url.clone();
        let assets = self.assets.clone();

        tokio::spawn(async move {
            let mut consecutive_failures: u32 = 0;
            let max_failures: u32 = 10;

            loop {
                match Self::connect_and_stream(&ws_url, &assets, &trade_tx).await {
                    Ok(()) => {
                        // Clean disconnect — reconnect immediately
                        consecutive_failures = 0;
                        tracing::info!("WebSocket disconnected cleanly, reconnecting...");
                    }
                    Err(e) => {
                        consecutive_failures += 1;
                        tracing::warn!(
                            error = %e,
                            failures = consecutive_failures,
                            "WebSocket error, reconnecting..."
                        );

                        if consecutive_failures >= max_failures {
                            let msg = format!(
                                "WebSocket failed {max_failures} consecutive times: {e}"
                            );
                            tracing::error!("{msg}");
                            let _ = kill_tx.send(msg).await;
                            return;
                        }

                        // Exponential backoff: 1s, 2s, 4s, ..., 30s cap
                        let delay = Duration::from_secs(
                            (1u64 << consecutive_failures.min(5)).min(30),
                        );
                        sleep(delay).await;
                    }
                }
            }
        })
    }

    /// Connect to the WebSocket and stream trade events until disconnection.
    async fn connect_and_stream(
        ws_url: &str,
        assets: &[String],
        trade_tx: &tokio::sync::mpsc::Sender<WsTrade>,
    ) -> Result<(), EngineError> {
        let (ws_stream, _response) = connect_async(ws_url).await?;
        let (mut write, mut read) = ws_stream.split();

        tracing::info!("WebSocket connected to {ws_url}");

        // Subscribe to market channel for trade events
        if assets.is_empty() {
            // Subscribe to the global market channel
            let sub_msg = serde_json::json!({
                "type": "subscribe",
                "channel": "market",
                "assets_id": serde_json::Value::Null,
            });
            write
                .send(Message::Text(sub_msg.to_string().into()))
                .await
                .map_err(EngineError::WebSocket)?;
        } else {
            // Subscribe to specific assets
            for asset_id in assets {
                let sub_msg = serde_json::json!({
                    "auth": {},
                    "type": "subscribe",
                    "channel": "market",
                    "assets_id": asset_id,
                });
                write
                    .send(Message::Text(sub_msg.to_string().into()))
                    .await
                    .map_err(EngineError::WebSocket)?;
            }
        }

        tracing::info!("Subscribed to trade feed");

        // Read messages
        while let Some(msg) = read.next().await {
            let msg = msg?;

            match msg {
                Message::Text(text) => {
                    if let Some(trade) = Self::parse_trade(&text)
                        && trade_tx.send(trade).await.is_err()
                    {
                        // Receiver dropped — engine shutting down
                        return Ok(());
                    }
                }
                Message::Ping(data) => {
                    write
                        .send(Message::Pong(data))
                        .await
                        .map_err(EngineError::WebSocket)?;
                }
                Message::Close(_) => {
                    tracing::info!("WebSocket server sent close frame");
                    return Ok(());
                }
                _ => {}
            }
        }

        Ok(())
    }

    /// Parse a raw WebSocket text message into a WsTrade.
    ///
    /// Returns None for non-trade messages (subscriptions, heartbeats, etc.).
    fn parse_trade(text: &str) -> Option<WsTrade> {
        // The CLOB WebSocket sends various message types.
        // Trade events contain "event_type": "trade" or are nested in arrays.
        let value: serde_json::Value = serde_json::from_str(text).ok()?;

        // Handle array of events (batch messages)
        if let Some(arr) = value.as_array() {
            // Return the first trade event found
            for item in arr {
                if let Some(trade) = Self::extract_trade(item) {
                    return Some(trade);
                }
            }
            return None;
        }

        Self::extract_trade(&value)
    }

    /// Extract a WsTrade from a JSON value if it represents a trade event.
    fn extract_trade(value: &serde_json::Value) -> Option<WsTrade> {
        // Check for event_type = "trade" or presence of trade fields
        let event_type = value.get("event_type").and_then(|v| v.as_str());

        if event_type == Some("trade") || event_type == Some("last_trade_price") {
            serde_json::from_value(value.clone()).ok()
        } else if value.get("asset_id").is_some() && value.get("price").is_some() {
            // Direct trade object without event_type wrapper
            serde_json::from_value(value.clone()).ok()
        } else {
            None
        }
    }
}
