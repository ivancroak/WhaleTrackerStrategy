//! Integration tests for the five-layer risk manager.

use chrono::Utc;
use rust_decimal_macros::dec;
use uuid::Uuid;
use whale_engine::config::RiskConfig;
use whale_engine::models::{ConfidenceTier, Position, Side, Signal};
use whale_engine::risk_manager::{RiskDenial, RiskManager};

/// Build a default RiskConfig with v2 parameters.
fn default_config() -> RiskConfig {
    RiskConfig {
        max_position_pct: dec!(0.03),
        max_open_positions: 8,
        max_per_category: 3,
        total_exposure_cap: dec!(0.25),
        daily_loss_stop_pct: dec!(0.03),
        drawdown_breaker_pct: dec!(0.10),
    }
}

/// Build a signal with the given category and size.
fn make_signal(category: &str, size: rust_decimal::Decimal) -> Signal {
    Signal {
        signal_id: Uuid::new_v4(),
        basket_category: category.to_string(),
        consensus_pct: dec!(0.75),
        confidence_tier: ConfidenceTier::Medium,
        kelly_multiplier: dec!(0.50),
        recommended_size: size,
        wallets_agreeing: 4,
        market_id: "market_1".to_string(),
        token_id: "token_1".to_string(),
        side: Side::Buy,
        detected_at: Utc::now(),
    }
}

/// Build a position in a given category with the given size.
fn make_position(category: &str, size: rust_decimal::Decimal) -> Position {
    make_position_with_market(category, size, "market", "token")
}

/// Build a position with specific market/token IDs.
fn make_position_with_market(
    category: &str,
    size: rust_decimal::Decimal,
    market_id: &str,
    token_id: &str,
) -> Position {
    Position {
        market_id: market_id.to_string(),
        token_id: token_id.to_string(),
        side: Side::Buy,
        entry_price: dec!(0.60),
        size,
        category: category.to_string(),
        opened_at: Utc::now(),
        source_signal: Uuid::new_v4(),
    }
}

// ============================================================
// Core validation tests (from INITIAL_PROMPT Phase 2)
// ============================================================

#[test]
fn trade_approved_within_all_limits() {
    let rm = RiskManager::new(default_config(), dec!(10000));
    let signal = make_signal("crypto", dec!(300)); // 3% of $10k — exactly at limit

    let result = rm.validate(&signal);
    assert!(result.is_ok());
    let approval = result.unwrap();
    assert_eq!(approval.approved_size, dec!(300));
}

#[test]
fn denied_position_too_large() {
    let rm = RiskManager::new(default_config(), dec!(10000));
    let signal = make_signal("crypto", dec!(400)); // 4% > 3% limit

    let result = rm.validate(&signal);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        RiskDenial::PositionTooLarge { .. }
    ));
}

#[test]
fn denied_category_full() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));

    // Fill "crypto" category to capacity (3 positions)
    for i in 0..3 {
        rm.record_trade(make_position_with_market(
            "crypto",
            dec!(100),
            &format!("market_{i}"),
            &format!("token_{i}"),
        ));
    }

    let signal = make_signal("crypto", dec!(100)); // 4th crypto position
    let result = rm.validate(&signal);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        RiskDenial::CategoryFull {
            category,
            count: 3,
            max: 3,
            ..
        } if category == "crypto"
    ));
}

#[test]
fn denied_daily_loss_stop() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));

    // Lose $350 = 3.5% of $10k (exceeds 3% daily loss stop)
    rm.record_pnl(dec!(-350));

    let signal = make_signal("crypto", dec!(100));
    let result = rm.validate(&signal);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        RiskDenial::DailyLossStop { .. }
    ));
}

#[test]
fn kill_switch_on_drawdown() {
    // Peak = $10k, current = $8900 → 11% drawdown > 10% limit
    let mut rm = RiskManager::new(default_config(), dec!(10000));
    rm.record_pnl(dec!(-1100)); // portfolio now $8900

    let signal = make_signal("crypto", dec!(100));
    let result = rm.validate(&signal);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        RiskDenial::DrawdownBreaker { .. }
    ));
}

// ============================================================
// Additional edge case tests
// ============================================================

#[test]
fn denied_max_open_positions() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));

    // Fill up to 8 positions (max) across different categories
    for i in 0..8 {
        let cat = format!("cat_{}", i / 3); // spread across categories
        rm.record_trade(make_position_with_market(
            &cat,
            dec!(100),
            &format!("market_{i}"),
            &format!("token_{i}"),
        ));
    }

    let signal = make_signal("new_cat", dec!(100)); // 9th position
    let result = rm.validate(&signal);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        RiskDenial::TooManyPositions { count: 8, max: 8 }
    ));
}

#[test]
fn denied_exposure_cap() {
    // Use relaxed per-position limit so exposure cap is the binding constraint
    let mut config = default_config();
    config.max_position_pct = dec!(0.10); // 10% per position
    config.max_per_category = 10;
    let mut rm = RiskManager::new(config, dec!(10000));

    // Add positions totaling $2,400 (24% exposure)
    for i in 0..3 {
        rm.record_trade(make_position_with_market(
            &format!("cat_{i}"),
            dec!(800),
            &format!("market_{i}"),
            &format!("token_{i}"),
        ));
    }

    // Try to add $200 more → 26% > 25% cap
    let signal = make_signal("new_cat", dec!(200));
    let result = rm.validate(&signal);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        RiskDenial::ExposureCapExceeded { .. }
    ));
}

#[test]
fn halted_blocks_all_trades() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));
    rm.kill_switch("manual shutdown");

    assert!(rm.is_halted());

    let signal = make_signal("crypto", dec!(100));
    let result = rm.validate(&signal);
    assert!(result.is_err());
    assert!(matches!(
        result.unwrap_err(),
        RiskDenial::Halted { reason } if reason == "manual shutdown"
    ));
}

#[test]
fn record_trade_updates_state() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));

    assert_eq!(rm.state().open_positions.len(), 0);
    assert_eq!(rm.state().total_exposure, dec!(0));

    rm.record_trade(make_position("crypto", dec!(300)));

    assert_eq!(rm.state().open_positions.len(), 1);
    assert_eq!(rm.state().total_exposure, dec!(0.03)); // $300 / $10000
}

#[test]
fn close_position_frees_capacity() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));

    // Fill crypto category
    for i in 0..3 {
        rm.record_trade(make_position_with_market(
            "crypto",
            dec!(100),
            &format!("market_{i}"),
            &format!("token_{i}"),
        ));
    }

    // Verify crypto is full
    let signal = make_signal("crypto", dec!(100));
    assert!(rm.validate(&signal).is_err());

    // Close one position
    let removed = rm.close_position("market_1", "token_1");
    assert!(removed.is_some());

    // Now crypto has capacity again
    let signal = make_signal("crypto", dec!(100));
    assert!(rm.validate(&signal).is_ok());
}

#[test]
fn peak_equity_only_increases() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));

    // Gain → peak updates
    rm.record_pnl(dec!(500));
    assert_eq!(rm.state().peak_equity, dec!(10500));

    // Loss → peak stays
    rm.record_pnl(dec!(-200));
    assert_eq!(rm.state().peak_equity, dec!(10500));
    assert_eq!(rm.state().portfolio_value, dec!(10300));

    // Another gain past old peak → peak updates
    rm.record_pnl(dec!(300));
    assert_eq!(rm.state().peak_equity, dec!(10600));
}

#[test]
fn exactly_at_limit_allowed() {
    let rm = RiskManager::new(default_config(), dec!(10000));

    // Exactly 3% = $300 — should be approved (boundary: <=, not <)
    let signal = make_signal("crypto", dec!(300));
    assert!(rm.validate(&signal).is_ok());
}

#[test]
fn daily_pnl_reset() {
    let mut rm = RiskManager::new(default_config(), dec!(10000));

    // Lose enough to trigger daily loss stop
    rm.record_pnl(dec!(-350));
    let signal = make_signal("crypto", dec!(100));
    assert!(rm.validate(&signal).is_err());

    // Reset daily P&L (new trading day)
    rm.reset_daily_pnl();

    // Same trade should now pass (portfolio is $9650, 3% = $289.50)
    let signal = make_signal("crypto", dec!(200));
    assert!(rm.validate(&signal).is_ok());
}
