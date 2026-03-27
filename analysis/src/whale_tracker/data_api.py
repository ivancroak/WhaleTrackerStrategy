"""Async client for Polymarket Data API and Gamma API.

Wraps the free (no-auth) endpoints used for whale discovery,
activity polling, and market info lookups.

Data API: https://data-api.polymarket.com
Gamma API: https://gamma-api.polymarket.com
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from decimal import Decimal

import httpx
from pydantic import BaseModel, Field
from tenacity import RetryError, retry, stop_after_attempt, wait_exponential

# ---------------------------------------------------------------------------
# Response models
# ---------------------------------------------------------------------------


class LeaderboardEntry(BaseModel):
    """A trader from the v1 leaderboard endpoint."""

    rank: int
    proxy_wallet: str = Field(alias="proxyWallet")
    pnl: Decimal
    volume: Decimal = Field(default=Decimal("0"), alias="vol")
    display_name: str = Field(default="", alias="userName")

    model_config = {"populate_by_name": True}


class ActivityEntry(BaseModel):
    """A single trade from the activity endpoint."""

    proxy_wallet: str = Field(alias="proxyWallet")
    condition_id: str = Field(alias="conditionId")
    asset: str  # = CLOB token_id
    side: str  # "BUY" or "SELL"
    size: Decimal
    usdc_size: Decimal = Field(alias="usdcSize")
    price: Decimal
    timestamp: datetime
    title: str = ""
    slug: str = ""
    event_slug: str = Field(default="", alias="eventSlug")
    transaction_hash: str = Field(default="", alias="transactionHash")

    model_config = {"populate_by_name": True}


class MarketInfo(BaseModel):
    """Market metadata from Gamma API."""

    question: str = ""
    condition_id: str = Field(alias="conditionId")
    clob_token_ids: list[str] = Field(default_factory=list, alias="clobTokenIds")
    outcome_prices: list[str] = Field(default_factory=list, alias="outcomePrices")
    category: str = ""
    active: bool = True

    model_config = {"populate_by_name": True}


class PositionEntry(BaseModel):
    """An open position from the positions endpoint."""

    proxy_wallet: str = Field(default="", alias="proxyWallet")
    condition_id: str = Field(alias="conditionId")
    asset: str
    size: Decimal
    avg_price: Decimal = Field(alias="avgPrice")
    cur_price: Decimal = Field(default=Decimal("0"), alias="curPrice")
    title: str = ""

    model_config = {"populate_by_name": True}


class ClosedPositionEntry(BaseModel):
    """A settled position from the closed-positions endpoint."""

    proxy_wallet: str = Field(default="", alias="proxyWallet")
    condition_id: str = Field(alias="conditionId")
    asset: str = ""
    size: Decimal = Field(default=Decimal("0"), alias="totalBought")
    avg_price: Decimal = Field(default=Decimal("0"), alias="avgPrice")
    realized_pnl: Decimal = Field(default=Decimal("0"), alias="realizedPnl")
    cur_price: Decimal = Field(default=Decimal("0"), alias="curPrice")
    title: str = ""

    model_config = {"populate_by_name": True}


# ---------------------------------------------------------------------------
# Client
# ---------------------------------------------------------------------------

DATA_API = "https://data-api.polymarket.com"
GAMMA_API = "https://gamma-api.polymarket.com"

# Throttle: max ~2 requests per second
_THROTTLE_INTERVAL = 0.5


class PolymarketDataClient:
    """Async client for Polymarket's free Data + Gamma APIs."""

    def __init__(self, timeout: float = 15.0) -> None:
        self._http = httpx.AsyncClient(timeout=timeout)
        self._last_request: float = 0.0

    async def _throttle(self) -> None:
        """Self-throttle to ~2 req/sec."""
        now = asyncio.get_event_loop().time()
        wait = _THROTTLE_INTERVAL - (now - self._last_request)
        if wait > 0:
            await asyncio.sleep(wait)
        self._last_request = asyncio.get_event_loop().time()

    @retry(stop=stop_after_attempt(3), wait=wait_exponential(min=1, max=10))
    async def _get(self, url: str, params: dict[str, str | int] | None = None) -> httpx.Response:
        """GET with throttle + retry on transient errors."""
        await self._throttle()
        resp = await self._http.get(url, params=params)
        resp.raise_for_status()
        return resp

    # -- Leaderboard ----------------------------------------------------------

    async def get_leaderboard(
        self,
        order_by: str = "PNL",
        limit: int = 50,
        time_period: str = "ALL",
        category: str = "OVERALL",
    ) -> list[LeaderboardEntry]:
        """Fetch top traders from the v1 leaderboard.

        Args:
            order_by: Sort column — "PNL" or "VOL".
            limit: Number of entries to return (max 50 per page).
            time_period: "DAY", "WEEK", "MONTH", or "ALL".
            category: "OVERALL", "SPORTS", "POLITICS", "CRYPTO", etc.

        Returns:
            Ranked list of leaderboard entries.
        """
        all_entries: list[LeaderboardEntry] = []
        remaining = limit
        offset = 0
        page_size = min(remaining, 50)  # API max 50 per page

        while remaining > 0:
            resp = await self._get(
                f"{DATA_API}/v1/leaderboard",
                params={
                    "orderBy": order_by,
                    "timePeriod": time_period,
                    "category": category,
                    "limit": page_size,
                    "offset": offset,
                },
            )
            data = resp.json()
            items = data if isinstance(data, list) else data.get("data", [])
            entries = [LeaderboardEntry.model_validate(e) for e in items]
            all_entries.extend(entries)
            if len(entries) < page_size:
                break  # No more pages
            remaining -= len(entries)
            offset += len(entries)
            page_size = min(remaining, 50)

        return all_entries

    # -- Activity (trade history) ---------------------------------------------

    async def get_activity(
        self,
        wallet: str,
        limit: int = 100,
    ) -> list[ActivityEntry]:
        """Fetch trade history for a wallet.

        Args:
            wallet: Proxy wallet address.
            limit: Max trades to return.

        Returns:
            List of trades, most recent first.
        """
        resp = await self._get(
            f"{DATA_API}/activity",
            params={"user": wallet, "type": "TRADE", "limit": limit},
        )
        data = resp.json()
        items = data if isinstance(data, list) else data.get("history", data.get("data", []))
        return [ActivityEntry.model_validate(e) for e in items]

    async def get_activity_all(
        self,
        wallet: str,
        max_pages: int = 10,
    ) -> list[ActivityEntry]:
        """Fetch full trade history for a wallet, paginating up to max_pages.

        Pages through up to ``max_pages * 100`` trades to build a complete
        history for scoring.

        Args:
            wallet: Proxy wallet address.
            max_pages: Maximum number of 100-trade pages to fetch.

        Returns:
            All trades across pages, most recent first.
        """
        all_activities: list[ActivityEntry] = []
        for page in range(max_pages):
            resp = await self._get(
                f"{DATA_API}/activity",
                params={
                    "user": wallet,
                    "type": "TRADE",
                    "limit": 100,
                    "offset": page * 100,
                },
            )
            data = resp.json()
            items = data if isinstance(data, list) else data.get("history", data.get("data", []))
            entries = [ActivityEntry.model_validate(e) for e in items]
            all_activities.extend(entries)
            if len(entries) < 100:
                break  # No more pages
        return all_activities

    # -- Positions ------------------------------------------------------------

    async def get_positions(self, wallet: str) -> list[PositionEntry]:
        """Fetch open positions for a wallet.

        Args:
            wallet: Proxy wallet address.

        Returns:
            List of open positions.
        """
        resp = await self._get(
            f"{DATA_API}/positions",
            params={"user": wallet},
        )
        data = resp.json()
        items = data if isinstance(data, list) else data.get("positions", data.get("data", []))
        return [PositionEntry.model_validate(e) for e in items]

    # -- Closed Positions -----------------------------------------------------

    async def get_closed_positions(self, wallet: str) -> list[ClosedPositionEntry]:
        """Fetch settled positions for a wallet.

        Args:
            wallet: Proxy wallet address.

        Returns:
            List of closed positions with realized P&L.
        """
        resp = await self._get(
            f"{DATA_API}/closed-positions",
            params={"user": wallet},
        )
        data = resp.json()
        items = data if isinstance(data, list) else data.get("positions", data.get("data", []))
        return [ClosedPositionEntry.model_validate(e) for e in items]

    # -- Market info (Gamma) --------------------------------------------------

    async def get_market(self, condition_id: str) -> MarketInfo | None:
        """Fetch market metadata from Gamma API.

        Args:
            condition_id: Market condition ID.

        Returns:
            MarketInfo if found, None otherwise.
        """
        try:
            resp = await self._get(
                f"{GAMMA_API}/markets/{condition_id}",
            )
            data = resp.json()
            return MarketInfo.model_validate(data)
        except (httpx.HTTPStatusError, httpx.HTTPError, RetryError, Exception):
            # 404 = not found, 422 = invalid ID, retry exhaustion, etc.
            return None

    # -- Holders (top holders per market) -------------------------------------

    async def get_holders(
        self,
        condition_id: str,
        limit: int = 50,
    ) -> list[dict[str, str | Decimal]]:
        """Fetch top holders for a market.

        Args:
            condition_id: Market condition ID.
            limit: Max holders to return.

        Returns:
            Raw holder data (structure varies).
        """
        resp = await self._get(
            f"{DATA_API}/holders",
            params={"market": condition_id, "limit": limit},
        )
        return resp.json()  # type: ignore[no-any-return]

    # -- Current price (for slippage checks) ----------------------------------

    async def get_current_price(self, token_id: str) -> Decimal | None:
        """Fetch the current mid price for a CLOB token.

        Uses Gamma API to look up market by token, then reads outcome price.

        Args:
            token_id: CLOB token ID.

        Returns:
            Current price as Decimal, or None if unavailable.
        """
        try:
            resp = await self._get(
                f"{GAMMA_API}/markets",
                params={"clob_token_ids": token_id, "limit": 1},
            )
            data = resp.json()
            markets = data if isinstance(data, list) else data.get("data", [])
            if not markets:
                return None
            market = markets[0]
            token_ids = market.get("clobTokenIds", [])
            prices = market.get("outcomePrices", [])
            if token_id in token_ids and prices:
                idx = token_ids.index(token_id)
                if idx < len(prices):
                    return Decimal(str(prices[idx]))
            # Fallback: return first price
            if prices:
                return Decimal(str(prices[0]))
        except (httpx.HTTPError, ValueError, IndexError):
            pass
        return None

    # -- Lifecycle ------------------------------------------------------------

    async def close(self) -> None:
        """Close the underlying HTTP client."""
        await self._http.aclose()

    async def __aenter__(self) -> PolymarketDataClient:
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.close()


def _parse_timestamp(raw: str | int | float | datetime) -> datetime:
    """Parse a timestamp from the API into a timezone-aware datetime.

    Args:
        raw: Timestamp as ISO string, unix epoch, or already a datetime.

    Returns:
        Timezone-aware UTC datetime.
    """
    if isinstance(raw, datetime):
        if raw.tzinfo is None:
            return raw.replace(tzinfo=UTC)
        return raw
    if isinstance(raw, (int, float)):
        return datetime.fromtimestamp(raw, tz=UTC)
    return datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
