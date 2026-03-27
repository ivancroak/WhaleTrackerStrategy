"""Risk validation gate — Python port of engine/src/risk_manager.rs.

Every order must pass through ``RiskGate.validate()`` before execution.
Check order (cheapest checks first):

1. Kill switch halted?
2. Drawdown breaker (12% from peak equity)
3. Daily loss stop (5% of portfolio)
4. Max open positions (10)
5. Position size (10% of portfolio)
6. Total exposure cap (90%)

All monetary values use ``Decimal`` — NEVER ``float`` for money.
"""

from __future__ import annotations

import sys
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from uuid import UUID

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from whale_tracker.models import Position, RiskState, Signal

# ---------------------------------------------------------------------------
# Denial reasons (mirrors Rust RiskDenial enum)
# ---------------------------------------------------------------------------


class DenialReason(StrEnum):
    """Why a trade was denied."""

    POSITION_TOO_LARGE = "POSITION_TOO_LARGE"
    TOO_MANY_POSITIONS = "TOO_MANY_POSITIONS"
    EXPOSURE_CAP_EXCEEDED = "EXPOSURE_CAP_EXCEEDED"
    DAILY_LOSS_STOP = "DAILY_LOSS_STOP"
    DRAWDOWN_BREAKER = "DRAWDOWN_BREAKER"
    HALTED = "HALTED"


@dataclass(frozen=True)
class RiskDenial:
    """Detailed denial with reason and context."""

    reason: DenialReason
    message: str
    details: dict[str, Decimal | int | str] = field(default_factory=dict)


@dataclass(frozen=True)
class Approval:
    """Approval token returned when all checks pass."""

    signal_id: UUID
    approved_size: Decimal


# ---------------------------------------------------------------------------
# Configuration (loads from engine.toml [risk] section)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RiskConfig:
    """Risk parameters — loaded from config/engine.toml."""

    max_position_pct: Decimal = Decimal("0.10")
    max_open_positions: int = 10
    total_exposure_cap: Decimal = Decimal("0.90")
    daily_loss_stop_pct: Decimal = Decimal("0.05")
    drawdown_breaker_pct: Decimal = Decimal("0.12")

    @classmethod
    def from_toml(cls, path: str | Path) -> RiskConfig:
        """Load risk config from a TOML file.

        Args:
            path: Path to the TOML config file.

        Returns:
            RiskConfig populated from the [risk] section.
        """
        with open(path, "rb") as f:
            data = tomllib.load(f)

        risk = data.get("risk", {})
        return cls(
            max_position_pct=Decimal(str(risk.get("max_position_pct", "0.10"))),
            max_open_positions=int(risk.get("max_open_positions", 10)),
            total_exposure_cap=Decimal(str(risk.get("total_exposure_cap", "0.90"))),
            daily_loss_stop_pct=Decimal(str(risk.get("daily_loss_stop_pct", "0.05"))),
            drawdown_breaker_pct=Decimal(str(risk.get("drawdown_breaker_pct", "0.12"))),
        )


# ---------------------------------------------------------------------------
# Risk Gate
# ---------------------------------------------------------------------------

HUNDRED = Decimal("100")
ZERO = Decimal("0")


class RiskGate:
    """Five-layer risk validation gate.

    Faithful Python port of ``engine/src/risk_manager.rs::RiskManager``.
    """

    def __init__(self, config: RiskConfig, portfolio_value: Decimal) -> None:
        self._config = config
        self._state = RiskState(
            portfolio_value=portfolio_value,
            peak_equity=portfolio_value,
            daily_pnl=ZERO,
            open_positions=[],
            total_exposure=ZERO,
        )
        self._halted = False
        self._halt_reason: str | None = None

    # -- Validation -----------------------------------------------------------

    def validate(self, signal: Signal) -> Approval | RiskDenial:
        """Validate a signal through all 7 risk checks.

        Args:
            signal: The trade signal to validate.

        Returns:
            Approval if all checks pass, RiskDenial otherwise.
        """
        # 1. Kill switch
        if self._halted:
            return RiskDenial(
                reason=DenialReason.HALTED,
                message=f"Trading halted: {self._halt_reason or 'Unknown'}",
                details={"halt_reason": self._halt_reason or "Unknown"},
            )

        # 2. Drawdown breaker
        if self._state.peak_equity > ZERO:
            drawdown_pct = (
                (self._state.peak_equity - self._state.portfolio_value)
                / self._state.peak_equity
            )
            if drawdown_pct > self._config.drawdown_breaker_pct:
                return RiskDenial(
                    reason=DenialReason.DRAWDOWN_BREAKER,
                    message=(
                        f"Drawdown breaker: {drawdown_pct * HUNDRED:.1f}% from peak "
                        f"(limit {self._config.drawdown_breaker_pct * HUNDRED:.0f}%)"
                    ),
                    details={
                        "drawdown_pct": drawdown_pct * HUNDRED,
                        "limit_pct": self._config.drawdown_breaker_pct * HUNDRED,
                    },
                )

        # 3. Daily loss stop
        daily_loss_limit = self._state.portfolio_value * self._config.daily_loss_stop_pct
        if self._state.daily_pnl < ZERO and abs(self._state.daily_pnl) > daily_loss_limit:
            loss_pct = (abs(self._state.daily_pnl) / self._state.portfolio_value) * HUNDRED
            return RiskDenial(
                reason=DenialReason.DAILY_LOSS_STOP,
                message=(
                    f"Daily loss stop: lost ${abs(self._state.daily_pnl)} today "
                    f"({loss_pct:.1f}% of portfolio, limit "
                    f"{self._config.daily_loss_stop_pct * HUNDRED:.0f}%)"
                ),
                details={
                    "loss": abs(self._state.daily_pnl),
                    "loss_pct": loss_pct,
                    "limit_pct": self._config.daily_loss_stop_pct * HUNDRED,
                },
            )

        # 4. Position count
        open_count = len(self._state.open_positions)
        if open_count >= self._config.max_open_positions:
            return RiskDenial(
                reason=DenialReason.TOO_MANY_POSITIONS,
                message=(
                    f"Max open positions reached "
                    f"({open_count}/{self._config.max_open_positions})"
                ),
                details={
                    "count": open_count,
                    "max": self._config.max_open_positions,
                },
            )

        # 5. Position size
        max_size = self._state.portfolio_value * self._config.max_position_pct
        if signal.recommended_size > max_size:
            return RiskDenial(
                reason=DenialReason.POSITION_TOO_LARGE,
                message=(
                    f"Position size ${signal.recommended_size} exceeds "
                    f"{self._config.max_position_pct * HUNDRED:.0f}% max "
                    f"(${max_size} allowed)"
                ),
                details={
                    "size": signal.recommended_size,
                    "pct": self._config.max_position_pct * HUNDRED,
                    "limit": max_size,
                },
            )

        # 6. Exposure cap
        new_exposure_usd = self._total_exposure_usd() + signal.recommended_size
        new_exposure_pct = new_exposure_usd / self._state.portfolio_value
        if new_exposure_pct > self._config.total_exposure_cap:
            return RiskDenial(
                reason=DenialReason.EXPOSURE_CAP_EXCEEDED,
                message=(
                    f"Exposure would be {new_exposure_pct * HUNDRED:.1f}%, "
                    f"exceeding {self._config.total_exposure_cap * HUNDRED:.0f}% cap"
                ),
                details={
                    "new_exposure_pct": new_exposure_pct * HUNDRED,
                    "cap_pct": self._config.total_exposure_cap * HUNDRED,
                },
            )

        return Approval(
            signal_id=signal.signal_id,
            approved_size=signal.recommended_size,
        )

    # -- State mutation -------------------------------------------------------

    def record_trade(self, position: Position) -> None:
        """Record a new open position after trade execution.

        Args:
            position: The position to add.
        """
        self._state.open_positions.append(position)
        self._recalculate_exposure()

    def record_pnl(self, amount: Decimal) -> None:
        """Record realized P&L.

        Args:
            amount: P&L amount (negative for losses).
        """
        self._state.daily_pnl += amount
        self._state.portfolio_value += amount
        if self._state.portfolio_value > self._state.peak_equity:
            self._state.peak_equity = self._state.portfolio_value

    def close_position(self, market_id: str, token_id: str) -> Position | None:
        """Remove a closed position by market and token ID.

        Args:
            market_id: Market identifier.
            token_id: Token identifier.

        Returns:
            The removed Position, or None if not found.
        """
        for i, p in enumerate(self._state.open_positions):
            if p.market_id == market_id and p.token_id == token_id:
                removed = self._state.open_positions.pop(i)
                self._recalculate_exposure()
                return removed
        return None

    def kill_switch(self, reason: str) -> None:
        """Halt all trading until manual reset.

        Args:
            reason: Why trading was halted.
        """
        self._halted = True
        self._halt_reason = reason

    def is_halted(self) -> bool:
        """Check whether trading is currently halted."""
        return self._halted

    def reset_daily_pnl(self) -> None:
        """Reset daily P&L counter (call at start of each trading day)."""
        self._state.daily_pnl = ZERO

    @property
    def state(self) -> RiskState:
        """Read-only access to current risk state."""
        return self._state

    # -- Internal helpers -----------------------------------------------------

    def _total_exposure_usd(self) -> Decimal:
        """Sum of all open position sizes in USDC."""
        return sum((p.size for p in self._state.open_positions), ZERO)

    def _recalculate_exposure(self) -> None:
        """Recalculate total exposure as a fraction of portfolio value."""
        if self._state.portfolio_value > ZERO:
            self._state.total_exposure = (
                self._total_exposure_usd() / self._state.portfolio_value
            )
