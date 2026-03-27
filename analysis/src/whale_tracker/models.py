"""Domain models mirroring Rust engine types.

These Pydantic models are serialization-compatible with the Rust types in
``engine/src/models.rs``. Both sides use the same field names and JSON
representation, ensuring lossless round-trip through config files and logs.

All monetary values use ``Decimal`` — NEVER ``float`` for money.
"""

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


class Side(StrEnum):
    """Which side of a binary market."""

    BUY = "BUY"
    SELL = "SELL"


class ConfidenceTier(StrEnum):
    """Signal confidence tier — determines Kelly multiplier."""

    LOW = "LOW"
    MEDIUM = "MEDIUM"
    HIGH = "HIGH"


class Wallet(BaseModel):
    """A scored whale wallet from the analysis layer."""

    address: str
    category: str
    sharpe: Decimal
    kelly_fraction: Decimal
    rolling_wr: Decimal
    ev_per_trade: Decimal
    last_scored: datetime


class Trade(BaseModel):
    """A single trade observed on the CLOB WebSocket feed."""

    wallet_address: str
    market_id: str
    token_id: str
    side: Side
    amount: Decimal
    price: Decimal
    timestamp: datetime


class Signal(BaseModel):
    """A consensus signal detected from a wallet basket."""

    signal_id: UUID = Field(default_factory=uuid4)
    basket_category: str
    consensus_pct: Decimal
    confidence_tier: ConfidenceTier
    kelly_multiplier: Decimal
    recommended_size: Decimal
    wallets_agreeing: int
    market_id: str
    token_id: str
    side: Side
    detected_at: datetime


class Position(BaseModel):
    """An open position held by the engine."""

    market_id: str
    token_id: str
    side: Side
    entry_price: Decimal
    size: Decimal
    category: str
    opened_at: datetime
    source_signal: UUID


class RiskState(BaseModel):
    """Real-time risk state tracked by the risk manager."""

    portfolio_value: Decimal
    peak_equity: Decimal
    daily_pnl: Decimal
    open_positions: list[Position] = Field(default_factory=list)
    total_exposure: Decimal
