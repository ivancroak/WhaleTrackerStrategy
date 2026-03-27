"""Strategy-agnostic signal source interface.

Any trading strategy implements ``SignalSource`` to plug into the bot's
risk-gate-to-executor pipeline. The bot calls ``poll()`` on a timer and
forwards emitted signals through risk validation and execution.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from whale_tracker.models import Signal


class SignalSource(ABC):
    """Abstract base class for signal generation strategies.

    Subclass this and implement ``poll()`` to create a new strategy.
    The bot treats all signals identically after generation —
    risk management and execution are strategy-independent.
    """

    @abstractmethod
    async def poll(self) -> list[Signal]:
        """Generate signals from the strategy's data source.

        Called every poll interval by the bot. Should return an empty
        list if no signals are detected.

        Returns:
            List of Signals ready for risk validation.
        """
        ...

    @abstractmethod
    def name(self) -> str:
        """Human-readable strategy name for logging."""
        ...
