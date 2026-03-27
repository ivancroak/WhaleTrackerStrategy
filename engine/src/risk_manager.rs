//! Five-layer risk validation gate for the whale copy trading engine.
//!
//! **Every** order must pass through [`RiskManager::validate()`] before execution.
//! The risk manager enforces position sizing, exposure caps, category limits,
//! daily loss stops, and drawdown circuit breakers.
//!
//! # Check Order (fail fast — cheapest checks first)
//!
//! 1. Kill switch halted?
//! 2. Drawdown breaker (10% from peak equity)
//! 3. Daily loss stop (3% of portfolio)
//! 4. Max open positions (8)
//! 5. Category limit (3 per category)
//! 6. Position size (3% of portfolio)
//! 7. Total exposure cap (25%)

use rust_decimal::Decimal;
use rust_decimal_macros::dec;
use uuid::Uuid;

use crate::config::RiskConfig;
use crate::models::{Position, RiskState, Signal};

/// Reason a trade was denied by the risk manager.
#[derive(Debug, Clone, thiserror::Error)]
pub enum RiskDenial {
    /// Requested position exceeds max-per-trade limit.
    #[error("Position size ${size} exceeds {pct}% max (${limit} allowed)")]
    PositionTooLarge {
        size: Decimal,
        pct: Decimal,
        limit: Decimal,
    },

    /// Too many concurrent open positions.
    #[error("Max open positions reached ({count}/{max})")]
    TooManyPositions { count: u32, max: u32 },

    /// Category already at capacity.
    #[error("Category '{category}' at capacity ({count}/{max} positions)")]
    CategoryFull {
        category: String,
        count: u32,
        max: u32,
    },

    /// Adding this position would breach the total exposure cap.
    #[error("Exposure would be {new_exposure}%, exceeding {cap}% cap")]
    ExposureCapExceeded { new_exposure: Decimal, cap: Decimal },

    /// Daily cumulative loss exceeds threshold — halt trading for the day.
    #[error("Daily loss stop: lost ${loss} today ({pct}% of portfolio, limit {limit_pct}%)")]
    DailyLossStop {
        loss: Decimal,
        pct: Decimal,
        limit_pct: Decimal,
    },

    /// Drawdown from peak equity exceeds circuit breaker — full shutdown.
    #[error("Drawdown breaker: {drawdown}% from peak (limit {limit}%)")]
    DrawdownBreaker { drawdown: Decimal, limit: Decimal },

    /// Trading has been halted (kill switch or circuit breaker triggered).
    #[error("Trading halted: {reason}")]
    Halted { reason: String },
}

/// Approval token returned when a signal passes all risk checks.
#[derive(Debug, Clone)]
pub struct Approval {
    /// Signal ID that was approved.
    pub signal_id: Uuid,
    /// Approved position size in USDC.
    pub approved_size: Decimal,
}

/// Five-layer risk validation gate.
///
/// Owns mutable [`RiskState`] and immutable [`RiskConfig`]. All trade signals
/// must pass through [`validate()`](RiskManager::validate) before execution.
pub struct RiskManager {
    state: RiskState,
    config: RiskConfig,
    halted: bool,
    halt_reason: Option<String>,
}

impl RiskManager {
    /// Create a new risk manager with the given config and initial portfolio value.
    pub fn new(config: RiskConfig, portfolio_value: Decimal) -> Self {
        Self {
            state: RiskState {
                portfolio_value,
                peak_equity: portfolio_value,
                daily_pnl: dec!(0),
                open_positions: Vec::new(),
                total_exposure: dec!(0),
            },
            config,
            halted: false,
            halt_reason: None,
        }
    }

    /// Validate a trade signal against all five risk layers.
    ///
    /// Returns [`Approval`] if the trade passes, or [`RiskDenial`] with a
    /// specific reason if any layer rejects it.
    ///
    /// # Check Order
    ///
    /// Checks are ordered cheapest-first for fail-fast behavior:
    /// 1. Kill switch
    /// 2. Drawdown breaker
    /// 3. Daily loss stop
    /// 4. Position count
    /// 5. Category count
    /// 6. Position size
    /// 7. Exposure cap
    pub fn validate(&self, signal: &Signal) -> Result<Approval, RiskDenial> {
        let hundred = dec!(100);

        // 1. Kill switch
        if self.halted {
            return Err(RiskDenial::Halted {
                reason: self
                    .halt_reason
                    .clone()
                    .unwrap_or_else(|| "Unknown".to_string()),
            });
        }

        // 2. Drawdown breaker (Layer 5b)
        if self.state.peak_equity > dec!(0) {
            let drawdown_pct =
                (self.state.peak_equity - self.state.portfolio_value) / self.state.peak_equity;
            if drawdown_pct > self.config.drawdown_breaker_pct {
                return Err(RiskDenial::DrawdownBreaker {
                    drawdown: drawdown_pct * hundred,
                    limit: self.config.drawdown_breaker_pct * hundred,
                });
            }
        }

        // 3. Daily loss stop (Layer 5a)
        let daily_loss_limit = self.state.portfolio_value * self.config.daily_loss_stop_pct;
        if self.state.daily_pnl < dec!(0) && self.state.daily_pnl.abs() > daily_loss_limit {
            let loss_pct =
                (self.state.daily_pnl.abs() / self.state.portfolio_value) * hundred;
            return Err(RiskDenial::DailyLossStop {
                loss: self.state.daily_pnl.abs(),
                pct: loss_pct,
                limit_pct: self.config.daily_loss_stop_pct * hundred,
            });
        }

        // 4. Position count (Layer 3)
        let open_count = self.state.open_positions.len() as u32;
        if open_count >= self.config.max_open_positions {
            return Err(RiskDenial::TooManyPositions {
                count: open_count,
                max: self.config.max_open_positions,
            });
        }

        // 5. Category count (Layer 2)
        let category_count = self
            .state
            .open_positions
            .iter()
            .filter(|p| p.category == signal.basket_category)
            .count() as u32;
        if category_count >= self.config.max_per_category {
            return Err(RiskDenial::CategoryFull {
                category: signal.basket_category.clone(),
                count: category_count,
                max: self.config.max_per_category,
            });
        }

        // 6. Position size (Layer 1)
        let max_size = self.state.portfolio_value * self.config.max_position_pct;
        if signal.recommended_size > max_size {
            return Err(RiskDenial::PositionTooLarge {
                size: signal.recommended_size,
                pct: self.config.max_position_pct * hundred,
                limit: max_size,
            });
        }

        // 7. Exposure cap (Layer 4)
        let new_exposure_usd = self.total_exposure_usd() + signal.recommended_size;
        let new_exposure_pct = new_exposure_usd / self.state.portfolio_value;
        if new_exposure_pct > self.config.total_exposure_cap {
            return Err(RiskDenial::ExposureCapExceeded {
                new_exposure: new_exposure_pct * hundred,
                cap: self.config.total_exposure_cap * hundred,
            });
        }

        Ok(Approval {
            signal_id: signal.signal_id,
            approved_size: signal.recommended_size,
        })
    }

    /// Record a new open position after trade execution.
    ///
    /// Updates the open position list and recalculates total exposure.
    pub fn record_trade(&mut self, position: Position) {
        self.state.open_positions.push(position);
        self.recalculate_exposure();
    }

    /// Record realized P&L from a closed trade or mark-to-market update.
    ///
    /// Updates daily P&L, portfolio value, and peak equity (if new ATH).
    pub fn record_pnl(&mut self, amount: Decimal) {
        self.state.daily_pnl += amount;
        self.state.portfolio_value += amount;
        if self.state.portfolio_value > self.state.peak_equity {
            self.state.peak_equity = self.state.portfolio_value;
        }
    }

    /// Remove a closed position by market and token ID.
    ///
    /// Returns the removed position if found, or `None` if no match.
    /// Recalculates total exposure after removal.
    pub fn close_position(&mut self, market_id: &str, token_id: &str) -> Option<Position> {
        let idx = self
            .state
            .open_positions
            .iter()
            .position(|p| p.market_id == market_id && p.token_id == token_id)?;
        let removed = self.state.open_positions.swap_remove(idx);
        self.recalculate_exposure();
        Some(removed)
    }

    /// Activate the kill switch — halts all trading until manual reset.
    pub fn kill_switch(&mut self, reason: &str) {
        self.halted = true;
        self.halt_reason = Some(reason.to_string());
        tracing::error!(reason, "KILL SWITCH ACTIVATED — all trading halted");
    }

    /// Check whether trading is currently halted.
    pub fn is_halted(&self) -> bool {
        self.halted
    }

    /// Reset daily P&L counter (call at start of each trading day).
    pub fn reset_daily_pnl(&mut self) {
        self.state.daily_pnl = dec!(0);
    }

    /// Read-only access to current risk state.
    pub fn state(&self) -> &RiskState {
        &self.state
    }

    // --- Internal helpers ---

    /// Sum of all open position sizes in USDC.
    fn total_exposure_usd(&self) -> Decimal {
        self.state
            .open_positions
            .iter()
            .map(|p| p.size)
            .sum()
    }

    /// Recalculate total exposure as a fraction of portfolio value.
    fn recalculate_exposure(&mut self) {
        if self.state.portfolio_value > dec!(0) {
            self.state.total_exposure =
                self.total_exposure_usd() / self.state.portfolio_value;
        }
    }
}
