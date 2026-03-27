//! Telegram management plane — alerts and remote control.
//!
//! Embeds an async Telegram bot that:
//! - Forwards engine alerts (trades, risk denials, kill switch, daily P&L)
//! - Accepts commands: /stop, /pause, /resume, /status, /positions, /kill
//! - Only responds to the configured `TELEGRAM_CHAT_ID`
//!
//! Requires the `telegram` feature flag: `cargo build --features telegram`

use rust_decimal::Decimal;
use teloxide::prelude::*;
use teloxide::types::ChatId;
use tokio::sync::{broadcast, mpsc};

use crate::engine::{AlertEvent, EngineCommand, StatusSnapshot};

/// Telegram bot for engine management.
pub struct TelegramBot {
    bot: Bot,
    chat_id: ChatId,
    cmd_tx: mpsc::Sender<EngineCommand>,
    alert_rx: broadcast::Receiver<AlertEvent>,
}

impl TelegramBot {
    /// Create a new Telegram bot.
    ///
    /// # Arguments
    /// - `token`: Telegram bot token from BotFather.
    /// - `chat_id`: Authorized chat ID (only this chat can control the engine).
    /// - `cmd_tx`: Channel to send commands to the engine.
    /// - `alert_rx`: Channel to receive alerts from the engine.
    pub fn new(
        token: String,
        chat_id: i64,
        cmd_tx: mpsc::Sender<EngineCommand>,
        alert_rx: broadcast::Receiver<AlertEvent>,
    ) -> Self {
        let bot = Bot::new(token);
        Self {
            bot,
            chat_id: ChatId(chat_id),
            cmd_tx,
            alert_rx,
        }
    }

    /// Run both the alert forwarder and command listener concurrently.
    pub async fn run(self) {
        let bot_alerts = self.bot.clone();
        let chat_id = self.chat_id;
        let mut alert_rx = self.alert_rx;

        // Spawn alert forwarder
        let alert_handle = tokio::spawn(async move {
            loop {
                match alert_rx.recv().await {
                    Ok(event) => {
                        let msg = format_alert(&event);
                        if let Err(e) = bot_alerts.send_message(chat_id, &msg).await {
                            tracing::warn!(error = %e, "Failed to send Telegram alert");
                        }
                    }
                    Err(broadcast::error::RecvError::Lagged(n)) => {
                        tracing::warn!(lagged = n, "Telegram alert subscriber lagged");
                    }
                    Err(broadcast::error::RecvError::Closed) => {
                        tracing::info!("Alert channel closed, stopping Telegram alerts");
                        break;
                    }
                }
            }
        });

        // Run command listener
        let cmd_tx = self.cmd_tx;
        let authorized_chat = self.chat_id;
        let bot = self.bot;

        let handler = Update::filter_message().endpoint(
            move |msg: Message, bot: Bot| {
                let cmd_tx = cmd_tx.clone();
                let authorized = authorized_chat;
                async move {
                    handle_message(msg, bot, authorized, cmd_tx).await
                }
            },
        );

        let mut dispatcher = Dispatcher::builder(bot, handler)
            .enable_ctrlc_handler()
            .build();

        tokio::select! {
            _ = dispatcher.dispatch() => {}
            _ = alert_handle => {}
        }
    }
}

/// Handle an incoming Telegram message.
async fn handle_message(
    msg: Message,
    bot: Bot,
    authorized_chat: ChatId,
    cmd_tx: mpsc::Sender<EngineCommand>,
) -> Result<(), teloxide::RequestError> {
    // Authorization check
    if msg.chat.id != authorized_chat {
        bot.send_message(msg.chat.id, "Unauthorized.").await?;
        return Ok(());
    }

    let text = msg.text().unwrap_or("");
    let response = match text {
        "/stop" => {
            let _ = cmd_tx.send(EngineCommand::Stop).await;
            "Stopping engine...".to_string()
        }
        "/pause" => {
            let _ = cmd_tx.send(EngineCommand::Pause).await;
            "Engine paused. Signals ignored, WebSocket alive.".to_string()
        }
        "/resume" => {
            let _ = cmd_tx.send(EngineCommand::Resume).await;
            "Engine resumed. Processing signals.".to_string()
        }
        "/status" => {
            let _ = cmd_tx.send(EngineCommand::Status).await;
            "Fetching status...".to_string()
        }
        "/positions" => {
            let _ = cmd_tx.send(EngineCommand::GetPositions).await;
            "Fetching positions...".to_string()
        }
        "/kill" => {
            let _ = cmd_tx
                .send(EngineCommand::Kill {
                    reason: "Manual kill via Telegram".to_string(),
                })
                .await;
            "KILL SWITCH ACTIVATED. Engine halting.".to_string()
        }
        "/help" | "/start" => {
            "Whale Engine Commands:\n\
             /status — Risk state snapshot\n\
             /positions — Open positions\n\
             /pause — Pause signal processing\n\
             /resume — Resume signal processing\n\
             /stop — Graceful shutdown\n\
             /kill — Emergency halt (kill switch)"
                .to_string()
        }
        _ => {
            "Unknown command. Send /help for available commands.".to_string()
        }
    };

    bot.send_message(msg.chat.id, response).await?;
    Ok(())
}

/// Format an alert event as a Telegram message.
fn format_alert(event: &AlertEvent) -> String {
    match event {
        AlertEvent::TradeExecuted(result) => {
            format!(
                "TRADE EXECUTED\n\
                 Side: {:?}\n\
                 Token: {}...\n\
                 Size: ${}\n\
                 Price: {}\n\
                 Order: {}\n\
                 Latency: {}us",
                result.side,
                &result.token_id[..result.token_id.len().min(16)],
                result.size,
                result.price,
                result.order_id,
                result.latency_us,
            )
        }
        AlertEvent::RiskDenied {
            market_id,
            side,
            size,
            reason,
        } => {
            format!(
                "RISK DENIED\n\
                 Market: {}...\n\
                 Side: {:?}\n\
                 Size: ${}\n\
                 Reason: {}",
                &market_id[..market_id.len().min(20)],
                side,
                size,
                reason,
            )
        }
        AlertEvent::ExecutionFailed { market_id, error } => {
            format!(
                "EXECUTION FAILED\n\
                 Market: {}...\n\
                 Error: {}",
                &market_id[..market_id.len().min(20)],
                error,
            )
        }
        AlertEvent::KillSwitch { reason } => {
            format!("KILL SWITCH ACTIVATED\nReason: {reason}")
        }
        AlertEvent::DailyPnlSummary {
            pnl,
            portfolio_value,
        } => {
            let pnl_sign = if *pnl >= Decimal::ZERO { "+" } else { "" };
            format!(
                "DAILY P&L SUMMARY\n\
                 P&L: {pnl_sign}${pnl}\n\
                 Portfolio: ${portfolio_value}"
            )
        }
        AlertEvent::EngineStatus(s) => format_status(s),
    }
}

/// Format a status snapshot as a Telegram message.
fn format_status(s: &StatusSnapshot) -> String {
    let halted_str = if s.is_halted { " [HALTED]" } else { "" };
    let paused_str = if s.is_paused { " [PAUSED]" } else { "" };

    format!(
        "ENGINE STATUS{halted_str}{paused_str}\n\
         Strategy: {}\n\
         Portfolio: ${}\n\
         Peak: ${}\n\
         Daily P&L: ${}\n\
         Positions: {}\n\
         Exposure: {}%",
        s.strategy,
        s.portfolio_value,
        s.peak_equity,
        s.daily_pnl,
        s.open_positions,
        s.total_exposure_pct,
    )
}
