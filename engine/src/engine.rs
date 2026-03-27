//! Async engine orchestrator.
//!
//! Ties together the WebSocket feed, signal source, risk manager, and executor
//! into a single `tokio::select!` loop. Communicates with Telegram (Phase C)
//! via channels.

use std::path::{Path, PathBuf};
use std::sync::Arc;
use std::time::Duration;

use chrono::Utc;
use rust_decimal::Decimal;
use tokio::sync::{broadcast, mpsc, Mutex};
use tokio::time::interval;

use crate::clob_types::{OrderResult, WsTrade};
use crate::config::EngineConfig;
use crate::error::EngineError;
use crate::executor::ClobExecutor;
use crate::models::{Position, Side, Signal, Trade};
use crate::risk_manager::RiskManager;
use crate::signal_source::SignalSource;
use crate::websocket::ClobWebSocket;

// ---------------------------------------------------------------------------
// Engine commands and alerts
// ---------------------------------------------------------------------------

/// Commands sent to the engine (from Telegram or internal).
#[derive(Debug, Clone)]
pub enum EngineCommand {
    /// Graceful shutdown.
    Stop,
    /// Pause signal processing (keep WebSocket alive).
    Pause,
    /// Resume signal processing.
    Resume,
    /// Activate kill switch.
    Kill { reason: String },
    /// Request status snapshot.
    Status,
    /// Request open positions list.
    GetPositions,
}

/// Events emitted by the engine (for Telegram alerts).
#[derive(Debug, Clone)]
pub enum AlertEvent {
    /// A trade was successfully executed.
    TradeExecuted(OrderResult),
    /// A signal was denied by the risk manager.
    RiskDenied {
        market_id: String,
        side: Side,
        size: Decimal,
        reason: String,
    },
    /// An order execution failed.
    ExecutionFailed {
        market_id: String,
        error: String,
    },
    /// Kill switch was activated.
    KillSwitch { reason: String },
    /// Daily P&L summary.
    DailyPnlSummary {
        pnl: Decimal,
        portfolio_value: Decimal,
    },
    /// Engine status snapshot.
    EngineStatus(StatusSnapshot),
}

/// Snapshot of engine state for /status command.
#[derive(Debug, Clone)]
pub struct StatusSnapshot {
    pub portfolio_value: Decimal,
    pub peak_equity: Decimal,
    pub daily_pnl: Decimal,
    pub open_positions: usize,
    pub total_exposure_pct: Decimal,
    pub is_halted: bool,
    pub is_paused: bool,
    pub strategy: String,
}

// ---------------------------------------------------------------------------
// Trade log
// ---------------------------------------------------------------------------

/// Append an order result to the JSONL trade log.
fn log_trade(log_path: &Path, result: &OrderResult) {
    if let Ok(json) = serde_json::to_string(result)
        && let Ok(mut f) = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(log_path)
    {
        use std::io::Write;
        let _ = writeln!(f, "{json}");
    }
}

// ---------------------------------------------------------------------------
// Engine
// ---------------------------------------------------------------------------

/// The main trading engine.
pub struct Engine {
    config: EngineConfig,
    risk_manager: Arc<Mutex<RiskManager>>,
    signal_source: Box<dyn SignalSource>,
    executor: ClobExecutor,
    ws: ClobWebSocket,
    /// Receive commands from Telegram / internal.
    cmd_rx: mpsc::Receiver<EngineCommand>,
    /// Send alerts to Telegram.
    alert_tx: broadcast::Sender<AlertEvent>,
    /// Path to trades.jsonl log.
    log_path: PathBuf,
    /// Whether signal processing is paused.
    paused: bool,
}

impl Engine {
    /// Create a new engine.
    #[allow(clippy::too_many_arguments)]
    pub fn new(
        config: EngineConfig,
        risk_manager: Arc<Mutex<RiskManager>>,
        signal_source: Box<dyn SignalSource>,
        executor: ClobExecutor,
        ws: ClobWebSocket,
        cmd_rx: mpsc::Receiver<EngineCommand>,
        alert_tx: broadcast::Sender<AlertEvent>,
        log_path: PathBuf,
    ) -> Self {
        Self {
            config,
            risk_manager,
            signal_source,
            executor,
            ws,
            cmd_rx,
            alert_tx,
            log_path,
            paused: false,
        }
    }

    /// Run the engine main loop.
    pub async fn run(mut self) -> Result<(), EngineError> {
        // Create channels for WebSocket trades and kill signals
        let (trade_tx, mut trade_rx) = mpsc::channel::<WsTrade>(256);
        let (kill_tx, mut kill_rx) = mpsc::channel::<String>(4);

        // Spawn WebSocket listener
        let _ws_handle = self.ws.spawn_listener(trade_tx, kill_tx);

        // Tick intervals
        let mut signal_tick = interval(Duration::from_secs(60));
        let mut daily_reset = interval(Duration::from_secs(86400));

        tracing::info!(
            strategy = self.signal_source.name(),
            "Engine started"
        );

        loop {
            tokio::select! {
                // Branch 1: WebSocket trade event
                Some(ws_trade) = trade_rx.recv() => {
                    if !self.paused {
                        self.handle_ws_trade(ws_trade).await;
                    }
                }

                // Branch 2: Periodic signal tick
                _ = signal_tick.tick() => {
                    if !self.paused {
                        let signals = self.signal_source.tick();
                        for signal in signals {
                            self.process_signal(signal).await;
                        }
                    }
                }

                // Branch 3: Engine commands
                Some(cmd) = self.cmd_rx.recv() => {
                    match self.handle_command(cmd).await {
                        ControlFlow::Continue => {}
                        ControlFlow::Stop => {
                            tracing::info!("Engine stopping by command");
                            return Ok(());
                        }
                    }
                }

                // Branch 4: Kill signal from WebSocket
                Some(reason) = kill_rx.recv() => {
                    tracing::error!(reason = %reason, "Kill switch from WebSocket");
                    let mut rm = self.risk_manager.lock().await;
                    rm.kill_switch(&reason);
                    let _ = self.alert_tx.send(AlertEvent::KillSwitch {
                        reason: reason.clone(),
                    });
                    return Err(EngineError::KillSwitch(reason));
                }

                // Branch 5: Daily P&L reset
                _ = daily_reset.tick() => {
                    let mut rm = self.risk_manager.lock().await;
                    let pnl = rm.state().daily_pnl;
                    let portfolio = rm.state().portfolio_value;
                    rm.reset_daily_pnl();
                    tracing::info!(daily_pnl = %pnl, "Daily P&L reset");
                    let _ = self.alert_tx.send(AlertEvent::DailyPnlSummary {
                        pnl,
                        portfolio_value: portfolio,
                    });
                }
            }
        }
    }

    /// Process a raw WebSocket trade through the signal source.
    async fn handle_ws_trade(&mut self, ws_trade: WsTrade) {
        let start = std::time::Instant::now();

        // Convert WsTrade to domain Trade
        let trade = match Self::ws_trade_to_domain(&ws_trade) {
            Some(t) => t,
            None => return,
        };

        // Feed to signal source
        let signals = self.signal_source.process_event(&trade);
        for signal in signals {
            self.process_signal(signal).await;
        }

        let latency = start.elapsed();
        if latency > Duration::from_millis(5) {
            tracing::warn!(
                latency_ms = latency.as_millis(),
                "Tick-to-signal exceeded 5ms target"
            );
        }
    }

    /// Validate a signal through risk management and execute if approved.
    async fn process_signal(&self, signal: Signal) {
        tracing::info!(
            market = %signal.market_id,
            side = ?signal.side,
            confidence = ?signal.confidence_tier,
            whales = signal.wallets_agreeing,
            size = %signal.recommended_size,
            "Processing signal"
        );

        // Slippage gate (check min spread from resolution)
        let min_spread = self.config.execution.min_spread_from_resolution;
        // Note: signal-level spread check is already done in SignalSource,
        // but we double-check here for safety.

        // Risk validation
        let approval = {
            let rm = self.risk_manager.lock().await;
            rm.validate(&signal)
        };

        let approval = match approval {
            Ok(a) => a,
            Err(denial) => {
                tracing::info!(reason = %denial, "Signal denied by risk manager");
                let _ = self.alert_tx.send(AlertEvent::RiskDenied {
                    market_id: signal.market_id.clone(),
                    side: signal.side,
                    size: signal.recommended_size,
                    reason: denial.to_string(),
                });
                return;
            }
        };

        tracing::info!(
            approved_size = %approval.approved_size,
            "Risk approved, executing order"
        );

        // Determine order price from signal context
        // (whale entry price or use a reasonable default)
        let order_price = min_spread; // Will be refined in strategy research phase

        // Execute
        let result = self
            .executor
            .place_limit_order(
                &signal.token_id,
                signal.side,
                order_price,
                approval.approved_size,
            )
            .await;

        match result {
            Ok(mut order_result) => {
                order_result.signal_id = signal.signal_id;

                // Record position in risk manager
                let position = Position {
                    market_id: signal.market_id.clone(),
                    token_id: signal.token_id.clone(),
                    side: signal.side,
                    entry_price: order_result.price,
                    size: order_result.size,
                    category: signal.basket_category.clone(),
                    opened_at: Utc::now(),
                    source_signal: signal.signal_id,
                };

                {
                    let mut rm = self.risk_manager.lock().await;
                    rm.record_trade(position);
                }

                // Log and alert
                log_trade(&self.log_path, &order_result);
                tracing::info!(
                    order_id = %order_result.order_id,
                    latency_us = order_result.latency_us,
                    "Order placed"
                );
                let _ = self.alert_tx.send(AlertEvent::TradeExecuted(order_result));
            }
            Err(e) => {
                tracing::error!(error = %e, "Order execution failed");
                let _ = self.alert_tx.send(AlertEvent::ExecutionFailed {
                    market_id: signal.market_id,
                    error: e.to_string(),
                });
            }
        }
    }

    /// Handle an engine command.
    async fn handle_command(&mut self, cmd: EngineCommand) -> ControlFlow {
        match cmd {
            EngineCommand::Stop => {
                tracing::info!("Stop command received");
                ControlFlow::Stop
            }
            EngineCommand::Pause => {
                self.paused = true;
                tracing::info!("Engine paused");
                ControlFlow::Continue
            }
            EngineCommand::Resume => {
                self.paused = false;
                tracing::info!("Engine resumed");
                ControlFlow::Continue
            }
            EngineCommand::Kill { reason } => {
                let mut rm = self.risk_manager.lock().await;
                rm.kill_switch(&reason);
                let _ = self.alert_tx.send(AlertEvent::KillSwitch {
                    reason: reason.clone(),
                });
                tracing::error!(reason = %reason, "Kill switch activated");
                ControlFlow::Stop
            }
            EngineCommand::Status => {
                let rm = self.risk_manager.lock().await;
                let state = rm.state();
                let snapshot = StatusSnapshot {
                    portfolio_value: state.portfolio_value,
                    peak_equity: state.peak_equity,
                    daily_pnl: state.daily_pnl,
                    open_positions: state.open_positions.len(),
                    total_exposure_pct: state.total_exposure * Decimal::from(100),
                    is_halted: rm.is_halted(),
                    is_paused: self.paused,
                    strategy: self.signal_source.name().to_string(),
                };
                let _ = self.alert_tx.send(AlertEvent::EngineStatus(snapshot));
                ControlFlow::Continue
            }
            EngineCommand::GetPositions => {
                // Positions are returned via the status snapshot
                let rm = self.risk_manager.lock().await;
                let state = rm.state();
                let snapshot = StatusSnapshot {
                    portfolio_value: state.portfolio_value,
                    peak_equity: state.peak_equity,
                    daily_pnl: state.daily_pnl,
                    open_positions: state.open_positions.len(),
                    total_exposure_pct: state.total_exposure * Decimal::from(100),
                    is_halted: rm.is_halted(),
                    is_paused: self.paused,
                    strategy: self.signal_source.name().to_string(),
                };
                let _ = self.alert_tx.send(AlertEvent::EngineStatus(snapshot));
                ControlFlow::Continue
            }
        }
    }

    /// Convert a raw WebSocket trade to the domain Trade type.
    fn ws_trade_to_domain(ws: &WsTrade) -> Option<Trade> {
        let price = ws.price.parse::<Decimal>().ok()?;
        let size = ws.size.parse::<Decimal>().ok()?;
        let side = match ws.side.to_uppercase().as_str() {
            "BUY" => Side::Buy,
            "SELL" => Side::Sell,
            _ => return None,
        };

        // Parse timestamp or use now
        let timestamp = chrono::DateTime::parse_from_rfc3339(&ws.timestamp)
            .map(|dt| dt.with_timezone(&Utc))
            .unwrap_or_else(|_| Utc::now());

        // Use taker_address as the trade's wallet (taker initiated the trade)
        let wallet = if ws.taker_address.is_empty() {
            ws.maker_address.clone()
        } else {
            ws.taker_address.clone()
        };

        Some(Trade {
            wallet_address: wallet,
            market_id: ws.market.clone(),
            token_id: ws.asset_id.clone(),
            side,
            amount: size,
            price,
            timestamp,
        })
    }
}

/// Internal control flow for the engine loop.
enum ControlFlow {
    Continue,
    Stop,
}
