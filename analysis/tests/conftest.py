"""Shared pytest fixtures for whale-tracker tests."""

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest

from whale_tracker.models import (
    ConfidenceTier,
    Position,
    Side,
    Signal,
    Trade,
    Wallet,
)


@pytest.fixture
def sample_wallet() -> Wallet:
    """A scored whale wallet that meets all thresholds."""
    return Wallet(
        address="0x1234567890abcdef1234567890abcdef12345678",
        category="crypto",
        sharpe=Decimal("2.1"),
        kelly_fraction=Decimal("0.15"),
        rolling_wr=Decimal("0.65"),
        ev_per_trade=Decimal("0.03"),
        last_scored=datetime(2026, 3, 1, tzinfo=UTC),
    )


@pytest.fixture
def sample_trade() -> Trade:
    """A whale trade on a crypto market."""
    return Trade(
        wallet_address="0x1234567890abcdef1234567890abcdef12345678",
        market_id="0xmarket123",
        token_id="71321045863271958559064580309813014985928018621011757384287197810444593537592",
        side=Side.BUY,
        amount=Decimal("500.00"),
        price=Decimal("0.62"),
        timestamp=datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_signal() -> Signal:
    """A medium-confidence consensus signal."""
    return Signal(
        signal_id=uuid4(),
        basket_category="crypto",
        consensus_pct=Decimal("0.75"),
        confidence_tier=ConfidenceTier.MEDIUM,
        kelly_multiplier=Decimal("0.50"),
        recommended_size=Decimal("450.00"),
        wallets_agreeing=6,
        market_id="0xmarket123",
        token_id="71321045863271958559064580309813014985928018621011757384287197810444593537592",
        side=Side.BUY,
        detected_at=datetime(2026, 3, 1, 12, 5, 0, tzinfo=UTC),
    )


@pytest.fixture
def sample_position(sample_signal: Signal) -> Position:
    """An open position derived from the sample signal."""
    return Position(
        market_id=sample_signal.market_id,
        token_id=sample_signal.token_id,
        side=sample_signal.side,
        entry_price=Decimal("0.62"),
        size=Decimal("450.00"),
        category=sample_signal.basket_category,
        opened_at=sample_signal.detected_at,
        source_signal=sample_signal.signal_id,
    )
