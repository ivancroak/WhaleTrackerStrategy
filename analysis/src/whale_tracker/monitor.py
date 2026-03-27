"""Whale activity polling and signal generation.

Polls the Polymarket Data API for each watched wallet,
detects new trades, and generates consensus-based signals.
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

from pydantic import BaseModel, Field

from whale_tracker.data_api import PolymarketDataClient
from whale_tracker.models import ConfidenceTier, Side, Signal

# ---------------------------------------------------------------------------
# Watchlist models
# ---------------------------------------------------------------------------


class WatchedWallet(BaseModel):
    """A whale wallet in the watchlist."""

    address: str
    label: str = ""
    category: str = "general"
    notes: str = ""
    # Scoring metadata (auto-populated by scorer pipeline)
    avg_pct_return: Decimal | None = None
    markets_traded: int | None = None
    total_pnl: Decimal | None = None
    realized_pnl: Decimal | None = None
    discovery_sources: list[str] = Field(default_factory=list)
    last_scored_at: datetime | None = None
    added_at: datetime | None = None
    below_threshold_since: datetime | None = None  # auto-removal tracker


class WalletList(BaseModel):
    """Serializable watchlist stored as config/wallets.json."""

    wallets: list[WatchedWallet] = Field(default_factory=list)
    updated_at: datetime | None = None

    @classmethod
    def load(cls, path: str | Path) -> WalletList:
        """Load watchlist from a JSON file.

        Args:
            path: Path to wallets.json.

        Returns:
            Parsed WalletList.
        """
        with open(path) as f:
            return cls.model_validate(json.load(f))

    def save(self, path: str | Path) -> None:
        """Save watchlist to a JSON file.

        Args:
            path: Path to write wallets.json.
        """
        self.updated_at = datetime.now(tz=UTC)
        with open(path, "w") as f:
            f.write(self.model_dump_json(indent=2) + "\n")


# ---------------------------------------------------------------------------
# New whale trade (pre-signal)
# ---------------------------------------------------------------------------


class NewWhaleTrade(BaseModel):
    """A newly detected whale trade (not yet a Signal)."""

    wallet: str
    category: str
    condition_id: str
    token_id: str
    side: str
    size: Decimal
    usdc_size: Decimal
    price: Decimal
    timestamp: datetime
    title: str = ""


# ---------------------------------------------------------------------------
# Execution config for slippage/spread gates
# ---------------------------------------------------------------------------


class ExecutionConfig(BaseModel):
    """Execution parameters for signal filtering."""

    max_slippage_from_whale: Decimal = Decimal("0.03")
    min_spread_from_resolution: Decimal = Decimal("0.05")


# ---------------------------------------------------------------------------
# Kelly tier mapping
# ---------------------------------------------------------------------------

_KELLY_TIERS: dict[ConfidenceTier, Decimal] = {
    ConfidenceTier.LOW: Decimal("0.25"),
    ConfidenceTier.MEDIUM: Decimal("0.50"),
    ConfidenceTier.HIGH: Decimal("0.75"),
}


# ---------------------------------------------------------------------------
# Monitor
# ---------------------------------------------------------------------------


class WhaleMonitor:
    """Polls Data API for watched wallets and generates signals.

    Deduplication: tracks the last-seen timestamp per wallet.
    Signal confidence:
        - 1 whale on a market = LOW (0.25 Kelly)
        - 2 whales same market/side = MEDIUM (0.50)
        - 3+ whales = HIGH (0.75)
    """

    def __init__(
        self,
        wallets: list[WatchedWallet],
        client: PolymarketDataClient,
        execution_config: ExecutionConfig | None = None,
        portfolio_value: Decimal = Decimal("100"),
        max_position_pct: Decimal = Decimal("0.03"),
        poll_interval: int = 45,
    ) -> None:
        self._wallets = wallets
        self._client = client
        self._exec_config = execution_config or ExecutionConfig()
        self._portfolio_value = portfolio_value
        self._max_position_pct = max_position_pct
        self._poll_interval = poll_interval
        # Track last-seen timestamp per wallet address
        self._last_seen: dict[str, datetime] = {
            w.address: datetime.now(tz=UTC) for w in wallets
        }

    @property
    def poll_interval(self) -> int:
        """Seconds between polling rounds."""
        return self._poll_interval

    async def poll_once(self) -> list[NewWhaleTrade]:
        """Poll all watched wallets for new trades.

        Returns:
            List of newly detected whale trades since last poll.
        """
        new_trades: list[NewWhaleTrade] = []

        for wallet in self._wallets:
            try:
                activities = await self._client.get_activity(
                    wallet.address, limit=20
                )
            except Exception:
                continue

            cutoff = self._last_seen.get(
                wallet.address, datetime.now(tz=UTC)
            )
            latest_ts = cutoff

            for act in activities:
                ts = act.timestamp
                if ts.tzinfo is None:
                    ts = ts.replace(tzinfo=UTC)

                if ts <= cutoff:
                    continue

                if ts > latest_ts:
                    latest_ts = ts

                new_trades.append(
                    NewWhaleTrade(
                        wallet=wallet.address,
                        category=wallet.category,
                        condition_id=act.condition_id,
                        token_id=act.asset,
                        side=act.side,
                        size=act.size,
                        usdc_size=act.usdc_size,
                        price=act.price,
                        timestamp=ts,
                        title=act.title,
                    )
                )

            self._last_seen[wallet.address] = latest_ts

        return new_trades

    def build_signals(
        self,
        trades: list[NewWhaleTrade],
    ) -> list[Signal]:
        """Build consensus signals from new whale trades.

        Groups trades by (condition_id, token_id, side), counts distinct
        whales, and assigns confidence tier.

        Filters out:
        - Prices within ``min_spread_from_resolution`` of 0 or 1.

        Args:
            trades: New whale trades from poll_once().

        Returns:
            List of Signals ready for risk validation.
        """
        # Group by (condition_id, token_id, side)
        groups: dict[tuple[str, str, str], list[NewWhaleTrade]] = defaultdict(list)
        for t in trades:
            key = (t.condition_id, t.token_id, t.side)
            groups[key].append(t)

        signals: list[Signal] = []
        min_spread = self._exec_config.min_spread_from_resolution

        for (condition_id, token_id, side_str), group_trades in groups.items():
            # Consensus: count distinct wallets
            distinct_wallets = {t.wallet for t in group_trades}
            whale_count = len(distinct_wallets)

            # Confidence tier based on whale count
            if whale_count >= 3:
                tier = ConfidenceTier.HIGH
            elif whale_count >= 2:
                tier = ConfidenceTier.MEDIUM
            else:
                tier = ConfidenceTier.LOW

            kelly = _KELLY_TIERS[tier]

            # Use the most recent trade's price as reference
            latest_trade = max(group_trades, key=lambda t: t.timestamp)
            ref_price = latest_trade.price

            # Spread gate: skip if price is too close to 0 or 1
            if ref_price < min_spread or ref_price > (Decimal("1") - min_spread):
                continue

            # Position size = portfolio * max_position_pct * kelly_multiplier
            recommended_size = self._portfolio_value * self._max_position_pct * kelly

            # Category from the most recent trade's wallet
            category = latest_trade.category

            side = Side.BUY if side_str.upper() == "BUY" else Side.SELL

            signal = Signal(
                signal_id=uuid4(),
                basket_category=category,
                consensus_pct=Decimal(str(whale_count)) / Decimal(str(len(self._wallets)))
                if self._wallets
                else Decimal("0"),
                confidence_tier=tier,
                kelly_multiplier=kelly,
                recommended_size=recommended_size,
                wallets_agreeing=whale_count,
                market_id=condition_id,
                token_id=token_id,
                side=side,
                detected_at=datetime.now(tz=UTC),
            )
            # Attach whale entry price for slippage check
            signal.__dict__["whale_entry_price"] = ref_price

            signals.append(signal)

        return signals
