"""Tests for the risk validation gate.

Risk checks:
1. Kill switch
2. Drawdown breaker (12%)
3. Daily loss stop (5%)
4. Max open positions (10)
5. Position size (10%)
6. Exposure cap (90%)
"""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from whale_tracker.models import ConfidenceTier, Position, Side, Signal
from whale_tracker.risk_gate import (
    Approval,
    DenialReason,
    RiskConfig,
    RiskDenial,
    RiskGate,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def default_config() -> RiskConfig:
    """Current risk parameters."""
    return RiskConfig(
        max_position_pct=Decimal("0.10"),
        max_open_positions=10,
        total_exposure_cap=Decimal("0.90"),
        daily_loss_stop_pct=Decimal("0.05"),
        drawdown_breaker_pct=Decimal("0.12"),
    )


def make_signal(category: str, size: Decimal) -> Signal:
    """Build a signal with the given category and size."""
    return Signal(
        signal_id=uuid4(),
        basket_category=category,
        consensus_pct=Decimal("0.75"),
        confidence_tier=ConfidenceTier.MEDIUM,
        kelly_multiplier=Decimal("0.50"),
        recommended_size=size,
        wallets_agreeing=4,
        market_id="market_1",
        token_id="token_1",
        side=Side.BUY,
        detected_at=datetime.now(tz=UTC),
    )


def make_position(category: str, size: Decimal) -> Position:
    """Build a position in a given category with the given size."""
    return make_position_with_market(category, size, "market", "token")


def make_position_with_market(
    category: str,
    size: Decimal,
    market_id: str,
    token_id: str,
) -> Position:
    """Build a position with specific market/token IDs."""
    return Position(
        market_id=market_id,
        token_id=token_id,
        side=Side.BUY,
        entry_price=Decimal("0.60"),
        size=size,
        category=category,
        opened_at=datetime.now(tz=UTC),
        source_signal=uuid4(),
    )


# ============================================================
# Core validation tests
# ============================================================


class TestCoreValidation:
    """Core risk gate checks."""

    def test_trade_approved_within_all_limits(self) -> None:
        """10% of $10k = $1000 — exactly at limit, should pass."""
        rg = RiskGate(default_config(), Decimal("10000"))
        signal = make_signal("crypto", Decimal("1000"))

        result = rg.validate(signal)
        assert isinstance(result, Approval)
        assert result.approved_size == Decimal("1000")

    def test_denied_position_too_large(self) -> None:
        """11% > 10% limit — rejected."""
        rg = RiskGate(default_config(), Decimal("10000"))
        signal = make_signal("crypto", Decimal("1100"))

        result = rg.validate(signal)
        assert isinstance(result, RiskDenial)
        assert result.reason == DenialReason.POSITION_TOO_LARGE

    def test_denied_daily_loss_stop(self) -> None:
        """5.5% daily loss > 5% limit."""
        rg = RiskGate(default_config(), Decimal("10000"))
        rg.record_pnl(Decimal("-550"))

        signal = make_signal("crypto", Decimal("500"))
        result = rg.validate(signal)
        assert isinstance(result, RiskDenial)
        assert result.reason == DenialReason.DAILY_LOSS_STOP

    def test_kill_switch_on_drawdown(self) -> None:
        """13% drawdown > 12% limit."""
        rg = RiskGate(default_config(), Decimal("10000"))
        rg.record_pnl(Decimal("-1300"))  # portfolio now $8700

        signal = make_signal("crypto", Decimal("500"))
        result = rg.validate(signal)
        assert isinstance(result, RiskDenial)
        assert result.reason == DenialReason.DRAWDOWN_BREAKER


# ============================================================
# Edge case tests
# ============================================================


class TestEdgeCases:
    """Boundary and state mutation tests."""

    def test_denied_max_open_positions(self) -> None:
        """11th position rejected when 10 are open."""
        rg = RiskGate(default_config(), Decimal("10000"))

        for i in range(10):
            rg.record_trade(
                make_position_with_market("all", Decimal("500"), f"market_{i}", f"token_{i}")
            )

        signal = make_signal("all", Decimal("500"))
        result = rg.validate(signal)
        assert isinstance(result, RiskDenial)
        assert result.reason == DenialReason.TOO_MANY_POSITIONS
        assert result.details["count"] == 10
        assert result.details["max"] == 10

    def test_denied_exposure_cap(self) -> None:
        """91% exposure > 90% cap."""
        rg = RiskGate(default_config(), Decimal("10000"))

        # Add $9000 exposure (90%)
        for i in range(9):
            rg.record_trade(
                make_position_with_market("all", Decimal("1000"), f"market_{i}", f"token_{i}")
            )

        # $100 more → 91% > 90%
        signal = make_signal("all", Decimal("100"))
        result = rg.validate(signal)
        assert isinstance(result, RiskDenial)
        assert result.reason == DenialReason.EXPOSURE_CAP_EXCEEDED

    def test_halted_blocks_all_trades(self) -> None:
        """Kill switch blocks everything."""
        rg = RiskGate(default_config(), Decimal("10000"))
        rg.kill_switch("manual shutdown")

        assert rg.is_halted()

        signal = make_signal("crypto", Decimal("500"))
        result = rg.validate(signal)
        assert isinstance(result, RiskDenial)
        assert result.reason == DenialReason.HALTED
        assert "manual shutdown" in result.message

    def test_record_trade_updates_state(self) -> None:
        """Position tracking works."""
        rg = RiskGate(default_config(), Decimal("10000"))

        assert len(rg.state.open_positions) == 0
        assert rg.state.total_exposure == Decimal("0")

        rg.record_trade(make_position("crypto", Decimal("1000")))

        assert len(rg.state.open_positions) == 1
        assert rg.state.total_exposure == Decimal("0.10")  # $1000 / $10000

    def test_close_position_frees_capacity(self) -> None:
        """Closing a position frees a slot for new trades."""
        rg = RiskGate(default_config(), Decimal("10000"))

        for i in range(10):
            rg.record_trade(
                make_position_with_market("all", Decimal("500"), f"market_{i}", f"token_{i}")
            )

        # Full — 10/10 positions
        signal = make_signal("all", Decimal("500"))
        assert isinstance(rg.validate(signal), RiskDenial)

        # Close one
        removed = rg.close_position("market_1", "token_1")
        assert removed is not None

        # Now has capacity
        signal = make_signal("all", Decimal("500"))
        assert isinstance(rg.validate(signal), Approval)

    def test_peak_equity_only_increases(self) -> None:
        """Peak equity tracks ATH."""
        rg = RiskGate(default_config(), Decimal("10000"))

        rg.record_pnl(Decimal("500"))
        assert rg.state.peak_equity == Decimal("10500")

        rg.record_pnl(Decimal("-200"))
        assert rg.state.peak_equity == Decimal("10500")
        assert rg.state.portfolio_value == Decimal("10300")

        rg.record_pnl(Decimal("300"))
        assert rg.state.peak_equity == Decimal("10600")

    def test_exactly_at_limit_allowed(self) -> None:
        """Boundary: 10% = 10% should pass (<=, not <)."""
        rg = RiskGate(default_config(), Decimal("10000"))
        signal = make_signal("crypto", Decimal("1000"))
        assert isinstance(rg.validate(signal), Approval)

    def test_daily_pnl_reset(self) -> None:
        """Daily counter reset re-enables trading."""
        rg = RiskGate(default_config(), Decimal("10000"))

        rg.record_pnl(Decimal("-550"))
        signal = make_signal("crypto", Decimal("500"))
        assert isinstance(rg.validate(signal), RiskDenial)

        rg.reset_daily_pnl()

        # Portfolio is $9450, 10% = $945 → $500 should pass
        signal = make_signal("crypto", Decimal("500"))
        assert isinstance(rg.validate(signal), Approval)


# ============================================================
# $100 portfolio sizing test
# ============================================================


class TestSmallPortfolio:
    """Tests specific to $100 portfolio."""

    @pytest.mark.parametrize(
        "kelly,expected_size",
        [
            (Decimal("0.25"), Decimal("2.50")),   # LOW: $100 * 0.10 * 0.25
            (Decimal("0.50"), Decimal("5.00")),   # MED: $100 * 0.10 * 0.50
            (Decimal("0.75"), Decimal("7.50")),   # HIGH: $100 * 0.10 * 0.75
        ],
    )
    def test_100_dollar_portfolio_sizes(
        self,
        kelly: Decimal,
        expected_size: Decimal,
    ) -> None:
        """With $100 portfolio, verify Kelly-scaled sizes pass risk gate."""
        rg = RiskGate(default_config(), Decimal("100"))

        signal = Signal(
            signal_id=uuid4(),
            basket_category="all",
            consensus_pct=Decimal("0.75"),
            confidence_tier=ConfidenceTier.MEDIUM,
            kelly_multiplier=kelly,
            recommended_size=expected_size,
            wallets_agreeing=4,
            market_id="market_1",
            token_id="token_1",
            side=Side.BUY,
            detected_at=datetime.now(tz=UTC),
        )

        result = rg.validate(signal)
        assert isinstance(result, Approval)
        assert result.approved_size == expected_size

    def test_100_dollar_max_size_rejected(self) -> None:
        """$100 * 0.11 = $11 should be rejected (> 10% limit)."""
        rg = RiskGate(default_config(), Decimal("100"))
        signal = make_signal("all", Decimal("11"))  # 11% of $100

        result = rg.validate(signal)
        assert isinstance(result, RiskDenial)
        assert result.reason == DenialReason.POSITION_TOO_LARGE
