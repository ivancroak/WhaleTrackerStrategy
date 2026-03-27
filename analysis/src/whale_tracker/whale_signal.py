"""Whale consensus copy-trading signal source.

Wraps the existing ``WhaleMonitor`` to implement the ``SignalSource``
interface. This is the first (default) strategy implementation.
"""

from __future__ import annotations

from whale_tracker.models import Signal
from whale_tracker.monitor import WhaleMonitor
from whale_tracker.signal_source import SignalSource


class WhaleSignalSource(SignalSource):
    """Signal source that detects whale consensus on Polymarket.

    Delegates to ``WhaleMonitor.poll_once()`` for trade detection and
    ``WhaleMonitor.build_signals()`` for consensus-based signal generation.

    Args:
        monitor: An initialized WhaleMonitor instance.
    """

    def __init__(self, monitor: WhaleMonitor) -> None:
        self._monitor = monitor

    async def poll(self) -> list[Signal]:
        """Poll watched wallets and build consensus signals.

        Returns:
            List of Signals from whale consensus detection.
        """
        trades = await self._monitor.poll_once()
        if not trades:
            return []
        return self._monitor.build_signals(trades)

    def name(self) -> str:
        """Strategy name."""
        return "whale_consensus"
