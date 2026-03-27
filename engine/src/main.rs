//! Whale Engine CLI — Polymarket trading engine entry point.

use std::path::PathBuf;
use std::sync::Arc;

use clap::Parser;
use rust_decimal::Decimal;
use tokio::sync::{broadcast, mpsc, Mutex};

use whale_engine::config::{load_engine_config, load_wallet_config};
use whale_engine::engine::Engine;
use whale_engine::engine::EngineCommand;
use whale_engine::executor::ClobExecutor;
use whale_engine::models::Wallet;
use whale_engine::risk_manager::RiskManager;
use whale_engine::websocket::ClobWebSocket;
use whale_engine::whale_signal::{WhaleSignalConfig, WhaleSignalSource};

/// Polymarket trading engine.
#[derive(Parser)]
#[command(name = "whale-engine", about = "Polymarket trading engine")]
struct Cli {
    /// Portfolio value in USDC.
    #[arg(long, default_value = "100")]
    portfolio: String,

    /// Path to engine.toml config file.
    #[arg(long, default_value = "config/engine.toml")]
    config: PathBuf,

    /// Path to wallets.json watchlist.
    #[arg(long, default_value = "config/wallets.json")]
    wallets: PathBuf,

    /// Path to trades.jsonl audit log.
    #[arg(long, default_value = "data/trades.jsonl")]
    log: PathBuf,
}

#[tokio::main]
async fn main() -> anyhow::Result<()> {
    // Load .env
    dotenvy::dotenv().ok();

    // Initialize structured logging
    tracing_subscriber::fmt()
        .with_env_filter(
            tracing_subscriber::EnvFilter::try_from_default_env()
                .unwrap_or_else(|_| tracing_subscriber::EnvFilter::new("info")),
        )
        .json()
        .init();

    let cli = Cli::parse();
    let portfolio = cli
        .portfolio
        .parse::<Decimal>()
        .map_err(|e| anyhow::anyhow!("Invalid portfolio value: {e}"))?;

    // Load configs
    let config = load_engine_config(&cli.config)?;
    let wallet_config = load_wallet_config()?;

    tracing::info!(
        portfolio = %portfolio,
        config = %cli.config.display(),
        "Loading engine configuration"
    );

    // Load wallets from JSON
    let wallets = load_wallets(&cli.wallets)?;
    tracing::info!(wallets = wallets.len(), "Loaded wallet watchlist");

    // Build components
    let risk_manager = Arc::new(Mutex::new(RiskManager::new(
        config.risk.clone(),
        portfolio,
    )));

    let signal_config = WhaleSignalConfig {
        consensus_window_secs: 300,
        min_wallets: 1,
        portfolio_value: portfolio,
        max_position_pct: config.risk.max_position_pct,
        kelly: config.kelly.clone(),
        min_spread_from_resolution: config.execution.min_spread_from_resolution,
    };
    let signal_source = WhaleSignalSource::new(&wallets, signal_config);

    let executor = ClobExecutor::new(&wallet_config, &config.connection)?;

    let ws = ClobWebSocket::new(config.connection.ws_url.clone(), vec![]);

    // Channels
    let (cmd_tx, cmd_rx) = mpsc::channel::<EngineCommand>(32);
    let (alert_tx, _alert_rx) = broadcast::channel(64);

    // Telegram bot (optional, requires --features telegram + env vars)
    #[cfg(feature = "telegram")]
    {
        let tg_token = std::env::var("TELEGRAM_BOT_TOKEN").ok();
        let tg_chat_id = std::env::var("TELEGRAM_CHAT_ID")
            .ok()
            .and_then(|s| s.parse::<i64>().ok());

        if let (Some(token), Some(chat_id)) = (tg_token, tg_chat_id) {
            let tg_bot = whale_engine::telegram::TelegramBot::new(
                token,
                chat_id,
                cmd_tx.clone(),
                alert_tx.subscribe(),
            );
            tokio::spawn(async move {
                tg_bot.run().await;
                tracing::info!("Telegram bot stopped");
            });
            tracing::info!("Telegram bot started (chat_id: {chat_id})");
        } else {
            tracing::info!("Telegram not configured (set TELEGRAM_BOT_TOKEN + TELEGRAM_CHAT_ID)");
        }
    }

    // Ensure log directory exists
    if let Some(parent) = cli.log.parent() {
        std::fs::create_dir_all(parent)?;
    }

    let engine = Engine::new(
        config,
        risk_manager,
        Box::new(signal_source),
        executor,
        ws,
        cmd_rx,
        alert_tx,
        cli.log,
    );

    // Graceful shutdown on SIGINT/SIGTERM
    let shutdown_tx = cmd_tx.clone();
    tokio::spawn(async move {
        tokio::signal::ctrl_c().await.ok();
        tracing::info!("Shutdown signal received (Ctrl-C)");
        let _ = shutdown_tx.send(EngineCommand::Stop).await;
    });

    tracing::info!("Engine starting...");

    // Run the engine
    match engine.run().await {
        Ok(()) => {
            tracing::info!("Engine stopped gracefully");
            Ok(())
        }
        Err(e) => {
            tracing::error!(error = %e, "Engine stopped with error");
            Err(e.into())
        }
    }
}

/// Load wallet watchlist from a JSON file.
///
/// Returns an empty vec if the file doesn't exist.
fn load_wallets(path: &PathBuf) -> anyhow::Result<Vec<Wallet>> {
    if !path.exists() {
        tracing::warn!(path = %path.display(), "Wallets file not found, starting empty");
        return Ok(vec![]);
    }

    let contents = std::fs::read_to_string(path)?;
    let value: serde_json::Value = serde_json::from_str(&contents)?;

    // wallets.json has format: {"wallets": [...], "updated_at": ...}
    // The wallet objects are WatchedWallet (Python), not Wallet (Rust).
    // Extract addresses and create minimal Wallet structs.
    let wallet_array = value
        .get("wallets")
        .and_then(|v| v.as_array())
        .cloned()
        .unwrap_or_default();

    let mut wallets = Vec::new();
    for w in wallet_array {
        if let Some(address) = w.get("address").and_then(|v| v.as_str()) {
            let category = w
                .get("category")
                .and_then(|v| v.as_str())
                .unwrap_or("general")
                .to_string();

            wallets.push(Wallet {
                address: address.to_string(),
                category,
                sharpe: rust_decimal_macros::dec!(0),
                kelly_fraction: rust_decimal_macros::dec!(0),
                rolling_wr: rust_decimal_macros::dec!(0),
                ev_per_trade: rust_decimal_macros::dec!(0),
                last_scored: chrono::Utc::now(),
            });
        }
    }

    Ok(wallets)
}
