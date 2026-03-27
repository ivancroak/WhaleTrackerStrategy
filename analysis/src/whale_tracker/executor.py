"""Order execution via py-clob-client.

Auto-executes within risk limits. No human confirmation required.
Always uses MAKER (GTC limit) orders — never TAKER.
"""

from __future__ import annotations

import os
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from uuid import UUID

from pydantic import BaseModel

from whale_tracker.models import Signal
from whale_tracker.risk_gate import Approval

# ---------------------------------------------------------------------------
# Types
# ---------------------------------------------------------------------------


class OrderResult(BaseModel):
    """Result of an order execution attempt."""

    signal_id: UUID
    success: bool
    order_id: str = ""
    side: str
    token_id: str
    price: str
    size: str
    reason: str = ""
    timestamp: datetime


# ---------------------------------------------------------------------------
# Executor
# ---------------------------------------------------------------------------


class Executor:
    """Live order executor wrapping py-clob-client.

    Auto-executes after risk gate approval and slippage check.
    """

    def __init__(self, log_path: Path) -> None:
        self._log_path = log_path
        self._log_path.parent.mkdir(parents=True, exist_ok=True)
        self._init_clob_client()

    def _init_clob_client(self) -> None:
        """Initialize py-clob-client from environment variables."""
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds

        private_key = os.environ.get("PRIVATE_KEY")
        if not private_key:
            raise RuntimeError("PRIVATE_KEY not set in environment")

        api_key = os.environ.get("CLOB_API_KEY", "")
        api_secret = os.environ.get("CLOB_API_SECRET", "")
        api_passphrase = os.environ.get("CLOB_API_PASSPHRASE", "")

        if not all([api_key, api_secret, api_passphrase]):
            raise RuntimeError("CLOB credentials not set. Run: python3 scripts/get_clob_creds.py")

        creds = ApiCreds(
            api_key=api_key,
            api_secret=api_secret,
            api_passphrase=api_passphrase,
        )

        self._clob_client = ClobClient(
            host="https://clob.polymarket.com",
            chain_id=137,
            key=private_key,
            creds=creds,
        )

    async def execute(
        self,
        signal: Signal,
        approval: Approval,
        current_price: Decimal | None = None,
    ) -> OrderResult:
        """Execute a trade based on an approved signal.

        Args:
            signal: The validated trade signal.
            approval: The risk gate approval.
            current_price: Current market price for slippage check.

        Returns:
            OrderResult with execution details.
        """
        now = datetime.now(tz=UTC)
        size_dec = approval.approved_size

        # Slippage check (if we have current price and whale entry price)
        whale_price = getattr(signal, "whale_entry_price", None)
        if current_price is not None and whale_price is not None:
            slippage = abs(current_price - whale_price) / whale_price
            if slippage > Decimal("0.03"):
                result = OrderResult(
                    signal_id=signal.signal_id,
                    success=False,
                    side=signal.side,
                    token_id=signal.token_id,
                    price=str(current_price),
                    size=str(size_dec),
                    reason=f"Slippage too high: {slippage * 100:.1f}% > 3%",
                    timestamp=now,
                )
                self._log_trade(result)
                return result

        # Place GTC limit order via py-clob-client
        return self._place_limit_order(signal, approval, now)

    def _place_limit_order(
        self,
        signal: Signal,
        approval: Approval,
        now: datetime,
    ) -> OrderResult:
        """Place a GTC limit order via py-clob-client.

        Args:
            signal: The trade signal.
            approval: The approved sizing.
            now: Current timestamp.

        Returns:
            OrderResult with execution details.
        """
        from py_clob_client.order_builder.constants import BUY, SELL

        try:
            # py-clob-client uses float for price/size
            side = BUY if signal.side == "BUY" else SELL
            size_f = float(approval.approved_size)

            # Price: we use the whale's entry or a slightly better price
            order_price = float(getattr(signal, "whale_entry_price", Decimal("0.50")))

            order = self._clob_client.create_and_sign_order(
                {
                    "token_id": signal.token_id,
                    "price": order_price,
                    "size": size_f,
                    "side": side,
                }
            )
            resp = self._clob_client.post_order(order)

            order_id = ""
            if isinstance(resp, dict):
                order_id = resp.get("orderID", resp.get("id", ""))
                success = resp.get("success", True)
                reason = resp.get("reason", "Order placed")
            else:
                order_id = str(resp)
                success = True
                reason = "Order placed"

            result = OrderResult(
                signal_id=signal.signal_id,
                success=success,
                order_id=str(order_id),
                side=signal.side,
                token_id=signal.token_id,
                price=str(order_price),
                size=str(approval.approved_size),
                reason=reason,
                timestamp=now,
            )
        except Exception as e:
            result = OrderResult(
                signal_id=signal.signal_id,
                success=False,
                side=signal.side,
                token_id=signal.token_id,
                price="0",
                size=str(approval.approved_size),
                reason=f"Order failed: {e}",
                timestamp=now,
            )

        self._log_trade(result)
        return result

    def _log_trade(self, result: OrderResult) -> None:
        """Append trade result to the JSONL audit log.

        Args:
            result: The order result to log.
        """
        with open(self._log_path, "a") as f:
            f.write(result.model_dump_json() + "\n")
