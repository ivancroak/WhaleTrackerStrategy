"""Automatic whale scoring and watchlist selection pipeline.

Wallet selection:

1. Discover candidates from merged leaderboard slices (all categories).
2. Use recent activity for freshness checks.
3. Score wallet quality from resolved positions first (closed-positions API).
4. Fall back to FIFO trade matching if closed-position data is missing.
5. Rank passing wallets by weighted ROI, then realized PnL.

No bot filtering — profitable bots add signal strength to the consensus system.
Category filtering is optional (default: "all" = score across all categories).
"""

from __future__ import annotations

import sys
from collections import defaultdict, deque
from dataclasses import dataclass
from dataclasses import field as dataclass_field
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import BaseModel, Field

from whale_tracker.data_api import (
    ActivityEntry,
    ClosedPositionEntry,
    PolymarketDataClient,
)
from whale_tracker.monitor import WatchedWallet

# Optional analytics/ML stack. pandas/numpy/scikit-learn are declared
# dependencies, but we guard the import so the core scoring pipeline (and its
# tests) keep importing even if the data-science extras are absent in some
# minimal environment. ``_ANALYTICS_AVAILABLE`` gates the feature-frame and
# model-reranking helpers below.
try:
    from whale_tracker import analytics as _analytics

    _ANALYTICS_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without pandas/numpy
    _analytics = None  # type: ignore[assignment]
    _ANALYTICS_AVAILABLE = False

if TYPE_CHECKING:  # pragma: no cover - type-only imports
    import pandas as pd

    from whale_tracker.model import WhalePerformanceModel

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

ZERO = Decimal("0")
HUNDRED = Decimal("100")
SEVEN = Decimal("7")

_DISCOVERY_QUERIES: tuple[tuple[str, str, str], ...] = (
    ("PNL", "ALL", "all_time_pnl"),
    ("PNL", "MONTH", "monthly_pnl"),
)
_SOFT_FAILURE_CODES = {"low_return"}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ScoringConfig:
    """Scoring parameters loaded from the ``[scoring]`` TOML section."""

    category_focus: str = "all"
    min_markets_traded: int = 10
    min_total_pnl: Decimal = Decimal("10000")
    min_avg_pct_return: Decimal = Decimal("8")
    max_avg_daily_transactions_7d: int = 100
    scoring_lookback_months: int = 6
    activity_recency_days: int = 4
    max_tracked_wallets: int = 25
    rescan_interval_hours: int = 24
    removal_threshold_days: int = 7
    leaderboard_scan_size: int = 100

    @classmethod
    def from_toml(cls, path: str | Path) -> ScoringConfig:
        """Load scoring config from a TOML file.

        Args:
            path: Path to engine.toml.

        Returns:
            ScoringConfig populated from the ``[scoring]`` section.
        """
        with open(path, "rb") as f:
            data = tomllib.load(f)

        scoring = data.get("scoring", {})
        return cls(
            category_focus=str(scoring.get("category_focus", "sports")),
            min_markets_traded=int(scoring.get("min_markets_traded", 20)),
            min_total_pnl=Decimal(str(scoring.get("min_total_pnl", "10000"))),
            min_avg_pct_return=Decimal(str(scoring.get("min_avg_pct_return", "8"))),
            max_avg_daily_transactions_7d=int(scoring.get("max_avg_daily_transactions_7d", 100)),
            scoring_lookback_months=int(scoring.get("scoring_lookback_months", 6)),
            activity_recency_days=int(scoring.get("activity_recency_days", 4)),
            max_tracked_wallets=int(scoring.get("max_tracked_wallets", 25)),
            rescan_interval_hours=int(scoring.get("rescan_interval_hours", 24)),
            removal_threshold_days=int(scoring.get("removal_threshold_days", 7)),
            leaderboard_scan_size=int(scoring.get("leaderboard_scan_size", 100)),
        )


# ---------------------------------------------------------------------------
# Trade matching models
# ---------------------------------------------------------------------------


class RoundTripTrade(BaseModel):
    """A matched BUY→SELL or BUY→resolution round-trip trade."""

    asset: str
    buy_price: Decimal
    sell_price: Decimal
    size: Decimal
    pct_return: Decimal
    condition_id: str = ""
    resolved: bool = False


@dataclass
class _FifoEntry:
    """Internal pending BUY in the FIFO queue."""

    size: Decimal
    price: Decimal
    timestamp: datetime
    condition_id: str


@dataclass
class DiscoveryCandidate:
    """Merged leaderboard candidate before full wallet scoring."""

    address: str
    display_name: str = ""
    leaderboard_pnl: Decimal = ZERO
    discovery_sources: list[str] = dataclass_field(default_factory=list)


class WalletScore(BaseModel):
    """Full scoring result for a single wallet."""

    address: str
    total_pnl: Decimal
    avg_pct_return: Decimal
    markets_traded: int
    total_trades: int
    avg_daily_transactions_7d: Decimal
    last_trade_at: datetime | None = None
    round_trips: int = 0
    realized_pnl: Decimal = ZERO
    scoring_basis: str = "closed_positions"
    discovery_sources: list[str] = Field(default_factory=list)
    passes_filters: bool = False
    rejection_reasons: list[str] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# FIFO trade matcher
# ---------------------------------------------------------------------------


def match_round_trips(
    activities: list[ActivityEntry],
    market_categories: dict[str, str],
    market_resolutions: dict[str, Decimal | None],
    category_focus: str,
) -> list[RoundTripTrade]:
    """Match BUY→SELL pairs using FIFO per-asset queues.

    ``market_resolutions`` prefers asset/token IDs. Condition IDs are still
    accepted as a backward-compatible fallback for tests and older callers.

    Args:
        activities: Trade activities to match.
        market_categories: Mapping of condition_id to category string.
        market_resolutions: Mapping of asset or condition_id to resolution price.
        category_focus: Only include trades in this category.

    Returns:
        Matched round-trip trades.
    """
    if category_focus.lower() == "all":
        focused = list(activities)
    else:
        focused = [
            activity
            for activity in activities
            if market_categories.get(activity.condition_id, "").lower() == category_focus.lower()
        ]
    focused.sort(key=lambda activity: activity.timestamp)

    buy_queues: dict[str, deque[_FifoEntry]] = defaultdict(deque)
    round_trips: list[RoundTripTrade] = []

    for activity in focused:
        if activity.side.upper() == "BUY":
            buy_queues[activity.asset].append(
                _FifoEntry(
                    size=activity.size,
                    price=activity.price,
                    timestamp=activity.timestamp,
                    condition_id=activity.condition_id,
                )
            )
            continue

        if activity.side.upper() != "SELL":
            continue

        queue = buy_queues.get(activity.asset)
        if not queue:
            continue

        remaining_sell = activity.size
        while remaining_sell > ZERO and queue:
            entry = queue[0]
            matched_size = min(entry.size, remaining_sell)
            pct_ret = ZERO
            if entry.price > ZERO:
                pct_ret = (activity.price - entry.price) / entry.price * HUNDRED

            round_trips.append(
                RoundTripTrade(
                    asset=activity.asset,
                    buy_price=entry.price,
                    sell_price=activity.price,
                    size=matched_size,
                    pct_return=pct_ret,
                    condition_id=activity.condition_id,
                )
            )

            entry.size -= matched_size
            remaining_sell -= matched_size
            if entry.size <= ZERO:
                queue.popleft()

    for asset, queue in buy_queues.items():
        for entry in queue:
            resolution_price, known = _resolution_for_entry(
                asset,
                entry.condition_id,
                market_resolutions,
            )
            if not known or resolution_price is None:
                continue

            pct_ret = ZERO
            if entry.price > ZERO:
                pct_ret = (resolution_price - entry.price) / entry.price * HUNDRED

            round_trips.append(
                RoundTripTrade(
                    asset=asset,
                    buy_price=entry.price,
                    sell_price=resolution_price,
                    size=entry.size,
                    pct_return=pct_ret,
                    condition_id=entry.condition_id,
                    resolved=True,
                )
            )

    return round_trips


# ---------------------------------------------------------------------------
# Scoring pipeline
# ---------------------------------------------------------------------------


async def score_wallet(
    wallet_address: str,
    total_pnl: Decimal,
    client: PolymarketDataClient,
    config: ScoringConfig,
    market_cache: dict[str, object] | None = None,
) -> WalletScore:
    """Score a wallet against the current selection criteria.

    Uses slug and title-based category detection — **no Gamma API calls**.
    Combines closed-position round-trips with FIFO BUY→SELL pairs for
    maximum data coverage.

    Args:
        wallet_address: Proxy wallet address.
        total_pnl: Leaderboard seed PnL used for discovery metadata.
        client: Data API client.
        config: Scoring parameters.
        market_cache: Unused, kept for API compatibility.

    Returns:
        WalletScore with the computed metrics and filter decisions.
    """
    rejection_reasons: list[str] = []

    all_activities = await client.get_activity_all(wallet_address)
    try:
        closed_positions = await client.get_closed_positions(wallet_address)
    except Exception:
        closed_positions = []

    if not all_activities and not closed_positions:
        return WalletScore(
            address=wallet_address,
            total_pnl=total_pnl,
            avg_pct_return=ZERO,
            markets_traded=0,
            total_trades=0,
            avg_daily_transactions_7d=ZERO,
            rejection_reasons=["no_activity"],
        )

    now = datetime.now(tz=UTC)

    # Filter activities to the scoring lookback window
    lookback_cutoff = now - timedelta(days=config.scoring_lookback_months * 30)
    activities = [a for a in all_activities if _ensure_utc(a.timestamp) >= lookback_cutoff]
    avg_daily_txns = ZERO
    last_trade_at: datetime | None = None

    if activities:
        seven_days_ago = now - timedelta(days=7)
        recent_count = sum(
            1 for activity in activities if _ensure_utc(activity.timestamp) >= seven_days_ago
        )
        avg_daily_txns = Decimal(str(recent_count)) / SEVEN

        last_trade_at = max(_ensure_utc(activity.timestamp) for activity in activities)
        recency_cutoff = now - timedelta(days=config.activity_recency_days)
        if last_trade_at < recency_cutoff:
            rejection_reasons.append(
                f"inactive: last trade {last_trade_at.date()} > {config.activity_recency_days}d ago"
            )
    else:
        rejection_reasons.append("no_activity")

    # --- Build round trips (closed positions primary, FIFO supplemental) ---
    focus = config.category_focus.lower()
    if focus == "all":
        # All categories — no filtering needed
        filtered_closed = closed_positions
        market_categories: dict[str, str] = {}
    else:
        # Category-specific — detect categories from local data
        market_categories = _build_categories_from_local_data(activities, closed_positions)
        filtered_closed = [
            pos
            for pos in closed_positions
            if market_categories.get(pos.condition_id, "").lower() == focus
        ]

    closed_rts = _round_trips_from_closed_positions(filtered_closed)

    # FIFO adds BUY→SELL matched pairs from activity (no resolution needed)
    fifo_rts: list[RoundTripTrade] = []
    if activities:
        fifo_rts = match_round_trips(activities, market_categories, {}, config.category_focus)

    # Merge: closed positions are authoritative, FIFO adds new markets only
    closed_cids = {rt.condition_id for rt in closed_rts}
    supplemental = [rt for rt in fifo_rts if rt.condition_id not in closed_cids]
    round_trips = closed_rts + supplemental

    if closed_rts and supplemental:
        scoring_basis = "closed+fifo"
    elif closed_rts:
        scoring_basis = "closed_positions"
    elif fifo_rts:
        scoring_basis = "fifo_activity"
    else:
        scoring_basis = "none"

    market_count = len({trade.condition_id for trade in round_trips})
    if market_count < config.min_markets_traded:
        rejection_reasons.append(
            "too_few_markets: "
            f"{market_count} {config.category_focus} markets "
            f"< {config.min_markets_traded}"
        )

    realized_pnl, weighted_return_pct = _weighted_return_from_round_trips(round_trips)
    if weighted_return_pct < config.min_avg_pct_return:
        rejection_reasons.append(
            f"low_return: {weighted_return_pct:.1f}% avg < {config.min_avg_pct_return}%"
        )

    return WalletScore(
        address=wallet_address,
        total_pnl=total_pnl,
        avg_pct_return=weighted_return_pct,
        markets_traded=market_count,
        total_trades=len(activities),
        avg_daily_transactions_7d=avg_daily_txns,
        last_trade_at=last_trade_at,
        round_trips=len(round_trips),
        realized_pnl=realized_pnl,
        scoring_basis=scoring_basis,
        passes_filters=not rejection_reasons,
        rejection_reasons=rejection_reasons,
    )


async def discover_candidates(
    client: PolymarketDataClient,
    config: ScoringConfig,
) -> list[DiscoveryCandidate]:
    """Fetch and merge leaderboard candidates before full scoring.

    Args:
        client: Data API client.
        config: Scoring parameters.

    Returns:
        Deduplicated leaderboard candidates that pass the seed-PnL filter.
    """
    focus = config.category_focus.lower()
    category_api = "OVERALL" if focus == "all" else config.category_focus.upper()
    merged: dict[str, DiscoveryCandidate] = {}

    print(
        f"[scorer] Fetching candidate leaderboards (top {config.leaderboard_scan_size}, "
        f"category={category_api})..."
    )

    for order_by, time_period, source_name in _DISCOVERY_QUERIES:
        entries = await client.get_leaderboard(
            order_by=order_by,
            limit=config.leaderboard_scan_size,
            time_period=time_period,
            category=category_api,
        )
        print(
            f"[scorer]   source={source_name} order_by={order_by} "
            f"time_period={time_period}: {len(entries)} entries"
        )

        for entry in entries:
            candidate = merged.get(entry.proxy_wallet)
            if candidate is None:
                merged[entry.proxy_wallet] = DiscoveryCandidate(
                    address=entry.proxy_wallet,
                    display_name=entry.display_name or "",
                    leaderboard_pnl=entry.pnl,
                    discovery_sources=[source_name],
                )
                continue

            if entry.display_name and not candidate.display_name:
                candidate.display_name = entry.display_name
            if entry.pnl > candidate.leaderboard_pnl:
                candidate.leaderboard_pnl = entry.pnl
            if source_name not in candidate.discovery_sources:
                candidate.discovery_sources.append(source_name)

    candidates = [
        candidate
        for candidate in merged.values()
        if candidate.leaderboard_pnl >= config.min_total_pnl
    ]
    candidates.sort(
        key=lambda candidate: (candidate.leaderboard_pnl, len(candidate.discovery_sources)),
        reverse=True,
    )

    print(
        f"[scorer] {len(candidates)}/{len(merged)} merged candidates pass seed PNL pre-filter "
        f"(>= ${config.min_total_pnl})"
    )
    return candidates


# ---------------------------------------------------------------------------
# Discovery orchestrator
# ---------------------------------------------------------------------------


async def discover_whales(
    client: PolymarketDataClient,
    config: ScoringConfig,
) -> list[WatchedWallet]:
    """Run the full discovery and selection pipeline.

    Args:
        client: Data API client.
        config: Scoring parameters.

    Returns:
        Watchlist entries ready to be written to ``wallets.json``.
    """
    candidates = await discover_candidates(client, config)
    if not candidates:
        print("[scorer] No leaderboard candidates qualified for scoring.")
        return []

    market_cache: dict[str, object] = {}
    candidate_map = {candidate.address: candidate for candidate in candidates}
    scored: list[WalletScore] = []
    now = datetime.now(tz=UTC)

    for index, candidate in enumerate(candidates, 1):
        name = candidate.display_name or candidate.address[:12]
        source_text = ",".join(candidate.discovery_sources)
        print(
            f"[scorer] Scoring {index}/{len(candidates)}: {name} (sources={source_text})...",
            end=" ",
        )

        try:
            score = await score_wallet(
                candidate.address,
                candidate.leaderboard_pnl,
                client,
                config,
                market_cache,
            )
            score.discovery_sources = candidate.discovery_sources.copy()
            scored.append(score)

            if score.passes_filters:
                print(
                    f"PASS ({score.avg_pct_return:.1f}% weighted ROI, "
                    f"${score.realized_pnl:,.0f} realized, "
                    f"{score.markets_traded} markets)"
                )
            else:
                reasons = ", ".join(score.rejection_reasons)
                print(f"FAIL ({reasons})")
        except Exception as exc:
            print(f"ERROR ({exc})")

    passing = [score for score in scored if score.passes_filters]
    passing.sort(key=_wallet_rank_key, reverse=True)
    top = passing[: config.max_tracked_wallets]

    print(f"\n[scorer] {len(passing)} wallets passed all filters, keeping top {len(top)}")

    wallets: list[WatchedWallet] = []
    for score in top:
        candidate = candidate_map[score.address]
        wallets.append(
            WatchedWallet(
                address=score.address,
                label=candidate.display_name or score.address[:12],
                category=config.category_focus,
                notes=_wallet_notes(score),
                avg_pct_return=score.avg_pct_return,
                markets_traded=score.markets_traded,
                total_pnl=score.total_pnl,
                realized_pnl=score.realized_pnl,
                discovery_sources=score.discovery_sources,
                last_scored_at=now,
                added_at=now,
            )
        )

    return wallets


# ---------------------------------------------------------------------------
# Auto-removal logic
# ---------------------------------------------------------------------------


def apply_auto_removal(
    wallets: list[WatchedWallet],
    scores: dict[str, WalletScore],
    config: ScoringConfig,
) -> tuple[list[WatchedWallet], list[WatchedWallet]]:
    """Apply auto-removal state transitions to existing wallets.

    Soft failure:
    - ``low_return`` only → grace period before removal.

    Hard failure:
    - inactivity, no activity, or insufficient market history
      → remove immediately.

    Args:
        wallets: Current watchlist.
        scores: Fresh wallet scores by address.
        config: Scoring parameters.

    Returns:
        Tuple of kept and removed wallets.
    """
    now = datetime.now(tz=UTC)
    kept: list[WatchedWallet] = []
    removed: list[WatchedWallet] = []

    for wallet in wallets:
        score = scores.get(wallet.address)
        if score is None:
            kept.append(wallet)
            continue

        _update_wallet_from_score(wallet, score, now)

        if score.passes_filters:
            wallet.below_threshold_since = None
            kept.append(wallet)
            continue

        if not _is_soft_failure(score):
            removed.append(wallet)
            continue

        if wallet.below_threshold_since is None:
            wallet.below_threshold_since = now
            kept.append(wallet)
            continue

        days_below = (now - wallet.below_threshold_since).days
        if days_below >= config.removal_threshold_days:
            removed.append(wallet)
        else:
            kept.append(wallet)

    return kept, removed


# ---------------------------------------------------------------------------
# Feature engineering + optional model reranking (pandas/NumPy/scikit-learn)
# ---------------------------------------------------------------------------


def build_feature_frame(
    scored_wallets: list[WalletScore],
    *,
    round_trips_by_address: dict[str, list[RoundTripTrade]] | None = None,
    activities_by_address: dict[str, list[ActivityEntry]] | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Build a pandas feature matrix (one row per wallet) from scored wallets.

    This is the genuine entry point through which the scoring layer invokes
    pandas/NumPy: it delegates to :func:`whale_tracker.analytics.build_feature_frame`,
    which engineers ROI, realized-PnL, win-rate, volatility, a Sharpe-like
    ratio, an exponential recency weight, and a composite score.

    Args:
        scored_wallets: Wallets already scored by :func:`score_wallet`.
        round_trips_by_address: Optional per-wallet matched round-trips, used to
            compute the distribution-based columns (win rate, volatility,
            Sharpe-like) from real per-trade returns.
        activities_by_address: Optional per-wallet raw activity, used to derive
            ``avg_hold_hours``.
        now: Reference time for the recency decay (defaults to now, UTC).

    Returns:
        A :class:`pandas.DataFrame` indexed by wallet address.

    Raises:
        RuntimeError: If the analytics extras (pandas/NumPy) are unavailable.
    """
    if not _ANALYTICS_AVAILABLE or _analytics is None:
        raise RuntimeError(
            "build_feature_frame requires the analytics extras (pandas/numpy); "
            "install them via `pip install -e '.[dev]'`."
        )
    return _analytics.build_feature_frame(
        scored_wallets,
        round_trips_by_address=round_trips_by_address,
        activities_by_address=activities_by_address,
        now=now,
    )


def rank_candidates(
    scored_wallets: list[WalletScore],
    *,
    use_model: bool = False,
    model: WhalePerformanceModel | None = None,
    round_trips_by_address: dict[str, list[RoundTripTrade]] | None = None,
    activities_by_address: dict[str, list[ActivityEntry]] | None = None,
    now: datetime | None = None,
) -> list[WalletScore]:
    """Rank scored wallets, optionally reranking with a scikit-learn model.

    Default behaviour (``use_model=False``) is intentionally identical to the
    rest of the pipeline: wallets are sorted by :func:`_wallet_rank_key`
    (weighted ROI, then realized PnL, depth, seed PnL), best first. This path
    touches neither pandas nor the model, so existing behaviour is unchanged.

    When ``use_model=True`` a :class:`whale_tracker.model.WhalePerformanceModel`
    drives the order instead. A feature frame is built via
    :func:`build_feature_frame` and the model's predicted score per wallet
    determines the ranking. The model itself degrades gracefully to a
    deterministic heuristic when it has not been trained, so this never throws
    on an unfit model.

    Args:
        scored_wallets: Wallets to rank.
        use_model: Opt in to model-based reranking. ``False`` by default.
        model: A (typically pre-fitted) model. Required when ``use_model`` is
            ``True``.
        round_trips_by_address: Optional per-wallet round-trips for richer
            features (only used when ``use_model`` is ``True``).
        activities_by_address: Optional per-wallet activity for ``avg_hold_hours``.
        now: Reference time for the recency decay.

    Returns:
        A new list of the same ``WalletScore`` objects, ordered best-first.

    Raises:
        ValueError: If ``use_model`` is ``True`` but no ``model`` is supplied.
        RuntimeError: If ``use_model`` is ``True`` but the analytics extras are
            unavailable.
    """
    if not use_model:
        return sorted(scored_wallets, key=_wallet_rank_key, reverse=True)

    if model is None:
        raise ValueError("use_model=True requires a WhalePerformanceModel instance")
    if not _ANALYTICS_AVAILABLE:
        raise RuntimeError(
            "model reranking requires the analytics extras (pandas/numpy/scikit-learn)."
        )

    frame = build_feature_frame(
        scored_wallets,
        round_trips_by_address=round_trips_by_address,
        activities_by_address=activities_by_address,
        now=now,
    )
    ranked = model.rank(frame)
    order = {entry.address: position for position, entry in enumerate(ranked)}
    # Stable: wallets the model never saw (shouldn't happen) fall to the end.
    return sorted(
        scored_wallets,
        key=lambda score: order.get(score.address, len(order)),
    )


# ---------------------------------------------------------------------------
# Internal scoring helpers
# ---------------------------------------------------------------------------


def _round_trips_from_closed_positions(
    closed_positions: list[ClosedPositionEntry],
) -> list[RoundTripTrade]:
    """Convert settled positions into synthetic round-trip trades."""
    round_trips: list[RoundTripTrade] = []

    for position in closed_positions:
        if position.avg_price <= ZERO or position.size <= ZERO:
            continue

        cost_basis = position.avg_price * position.size
        pct_return = ZERO
        if cost_basis > ZERO:
            pct_return = position.realized_pnl / cost_basis * HUNDRED

        sell_price = position.avg_price + (position.realized_pnl / position.size)
        round_trips.append(
            RoundTripTrade(
                asset=position.asset,
                buy_price=position.avg_price,
                sell_price=sell_price,
                size=position.size,
                pct_return=pct_return,
                condition_id=position.condition_id,
                resolved=True,
            )
        )

    return round_trips


def _weighted_return_from_round_trips(
    round_trips: list[RoundTripTrade],
) -> tuple[Decimal, Decimal]:
    """Compute realized PnL and weighted return percentage."""
    realized_pnl = ZERO
    cost_basis = ZERO

    for trade in round_trips:
        realized_pnl += (trade.sell_price - trade.buy_price) * trade.size
        cost_basis += trade.buy_price * trade.size

    if cost_basis <= ZERO:
        return ZERO, ZERO
    return realized_pnl, realized_pnl / cost_basis * HUNDRED


def _build_categories_from_local_data(
    activities: list[ActivityEntry],
    closed_positions: list[ClosedPositionEntry],
) -> dict[str, str]:
    """Detect market categories from slugs and titles — no API calls.

    Priority:
    1. Activity slugs (most reliable — Polymarket slug prefixes)
    2. Closed-position titles (keyword matching for sports)
    3. Default to "other"
    """
    categories: dict[str, str] = {}

    # Activity slugs are the primary source
    for activity in activities:
        cid = activity.condition_id
        if cid in categories:
            continue
        slug = activity.slug or activity.event_slug or ""
        categories[cid] = _category_from_slug(slug)

    # Closed positions may have condition IDs not in activities
    for position in closed_positions:
        cid = position.condition_id
        if cid in categories:
            continue
        categories[cid] = _category_from_title(position.title)

    return categories


def _category_from_title(title: str) -> str:
    """Infer sports category from a closed-position title.

    Matches patterns like "Falcons vs. Buccaneers", "Spread: Seahawks (-4.5)",
    "Australian Open Men's: Carlos Alcaraz vs Alexander Zverev".
    """
    if not title:
        return "other"

    lower = title.lower()

    # Betting format indicators (almost always sports)
    if any(kw in lower for kw in ("spread:", "o/u ", "moneyline")):
        return "sports"

    # "vs." or "vs " pattern (team/player matchups)
    if " vs." in lower or " vs " in lower:
        # Check it's not a political/crypto "vs" (e.g., "Trump vs Biden")
        for sports_kw in _SPORTS_TITLE_KEYWORDS:
            if sports_kw in lower:
                return "sports"
        # "vs." with team-like names is usually sports
        # Conservative: require at least one sports keyword
        return "other"

    # Direct league/tournament mentions
    for sports_kw in _SPORTS_TITLE_KEYWORDS:
        if sports_kw in lower:
            return "sports"

    return "other"


def _wallet_rank_key(score: WalletScore) -> tuple[Decimal, Decimal, int, Decimal]:
    """Sort passing wallets by weighted ROI, realized PnL, depth, then seed PnL."""
    return (
        score.avg_pct_return,
        score.realized_pnl,
        score.markets_traded,
        score.total_pnl,
    )


def _wallet_notes(score: WalletScore) -> str:
    """Render a short note string for the stored watchlist."""
    sources = ", ".join(score.discovery_sources) if score.discovery_sources else "manual"
    return (
        f"Seed PNL: ${score.total_pnl:,.0f}, "
        f"Weighted ROI: {score.avg_pct_return:.1f}%, "
        f"Realized PnL: ${score.realized_pnl:,.0f}, "
        f"{score.markets_traded} resolved markets, "
        f"basis={score.scoring_basis}, "
        f"sources={sources}"
    )


def _update_wallet_from_score(
    wallet: WatchedWallet,
    score: WalletScore,
    scored_at: datetime,
) -> None:
    """Refresh stored watchlist metadata from a fresh score."""
    wallet.avg_pct_return = score.avg_pct_return
    wallet.markets_traded = score.markets_traded
    wallet.realized_pnl = score.realized_pnl
    wallet.last_scored_at = scored_at
    if score.total_pnl > ZERO:
        wallet.total_pnl = score.total_pnl
    if score.discovery_sources:
        wallet.discovery_sources = score.discovery_sources.copy()
    wallet.notes = _wallet_notes(score)


def _is_soft_failure(score: WalletScore) -> bool:
    """Return True when a wallet only fails the rolling performance rule."""
    if score.passes_filters or not score.rejection_reasons:
        return False
    codes = {_rejection_code(reason) for reason in score.rejection_reasons}
    return codes.issubset(_SOFT_FAILURE_CODES)


def _rejection_code(reason: str) -> str:
    """Extract the stable rejection code prefix from a reason string."""
    return reason.partition(":")[0]


def _resolution_for_entry(
    asset: str,
    condition_id: str,
    market_resolutions: dict[str, Decimal | None],
) -> tuple[Decimal | None, bool]:
    """Get the best available resolution price for a matched position."""
    if asset in market_resolutions:
        return market_resolutions[asset], True
    if condition_id in market_resolutions:
        return market_resolutions[condition_id], True
    return None, False


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _ensure_utc(dt: datetime) -> datetime:
    """Ensure a datetime is timezone-aware (UTC)."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=UTC)
    return dt


_SPORTS_PREFIXES = (
    "nhl-",
    "nba-",
    "nfl-",
    "mlb-",
    "mls-",
    "ufc-",
    "epl-",
    "f1-",
    "la-liga-",
    "serie-a-",
    "bundesliga-",
    "ligue-1-",
    "ucl-",
    "uel-",
    "ncaa-",
    "wnba-",
    "pga-",
    "atp-",
    "wta-",
    "ipl-",
    "nrl-",
    "afl-",
    "cricket-",
    "boxing-",
    "tennis-",
    "will-",
    "spread-",
)

_SPORTS_TITLE_KEYWORDS = (
    # Leagues
    "nhl",
    "nba",
    "nfl",
    "mlb",
    "mls",
    "ufc",
    "epl",
    "premier league",
    "la liga",
    "serie a",
    "bundesliga",
    "ligue 1",
    "champions league",
    "europa league",
    "ncaa",
    "wnba",
    "pga",
    "atp",
    "wta",
    "ipl",
    "nrl",
    "afl",
    # Sports
    "football",
    "basketball",
    "baseball",
    "hockey",
    "soccer",
    "tennis",
    "cricket",
    "boxing",
    "mma",
    "golf",
    "f1",
    "grand prix",
    "formula",
    "racing",
    # Betting terms in titles
    "win on 20",
    "will win",
    "to win",
    # Team patterns (common in Polymarket titles)
    "fc ",
    " fc",
    "united",
    "city ",
    "rovers",
    "chiefs",
    "eagles",
    "cowboys",
    "packers",
    "ravens",
    "lakers",
    "celtics",
    "warriors",
    "nets",
    "knicks",
    "yankees",
    "dodgers",
    "red sox",
    "cubs",
    "mets",
    "oilers",
    "bruins",
    "rangers",
    "penguins",
    "maple leafs",
    "open men",
    "open women",
    "grand slam",
    "australian open",
    "french open",
    "wimbledon",
    "us open",
)


def _category_from_slug(slug: str) -> str:
    """Infer market category from a Polymarket slug."""
    if not slug:
        return "other"

    lower = slug.lower()
    for prefix in _SPORTS_PREFIXES:
        if lower.startswith(prefix):
            return "sports"

    # Broader slug keyword check (catches "will-villarreal-cf-win")
    for kw in _SPORTS_TITLE_KEYWORDS:
        if kw in lower:
            return "sports"

    return "other"
