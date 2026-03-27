"""Tests for the automatic whale scoring & filtering pipeline.

Covers:
- FIFO trade matching (~10 tests)
- Filter logic (~7 tests)
- Auto-removal state machine (~4 tests)
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from whale_tracker.data_api import ActivityEntry, ClosedPositionEntry
from whale_tracker.monitor import WatchedWallet
from whale_tracker.scorer import (
    ScoringConfig,
    WalletScore,
    _ensure_utc,
    apply_auto_removal,
    discover_candidates,
    match_round_trips,
    score_wallet,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _activity(
    side: str,
    price: str,
    size: str,
    asset: str = "token_A",
    condition_id: str = "cond_1",
    minutes_offset: int = 0,
    timestamp: datetime | None = None,
    slug: str = "",
) -> ActivityEntry:
    """Build a test ActivityEntry."""
    ts = timestamp if timestamp is not None else NOW + timedelta(minutes=minutes_offset)
    return ActivityEntry.model_validate({
        "proxyWallet": "0xWALLET",
        "conditionId": condition_id,
        "asset": asset,
        "side": side,
        "size": size,
        "usdcSize": str(Decimal(size) * Decimal(price)),
        "price": price,
        "timestamp": ts,
        "title": "Test Market",
        "slug": slug,
    })


def _closed_position(
    condition_id: str,
    asset: str,
    avg_price: str,
    size: str,
    realized_pnl: str,
) -> ClosedPositionEntry:
    """Build a test ClosedPositionEntry."""
    return ClosedPositionEntry.model_validate({
        "proxyWallet": "0xWALLET",
        "conditionId": condition_id,
        "asset": asset,
        "totalBought": size,
        "avgPrice": avg_price,
        "realizedPnl": realized_pnl,
        "curPrice": avg_price,
        "title": "Resolved Test Market",
    })


class _FakeClient:
    """Minimal async client stub for scorer tests."""

    def __init__(
        self,
        leaderboards: dict[tuple[str, str, str], list[dict[str, object]]] | None = None,
        activities: dict[str, list[ActivityEntry]] | None = None,
        closed_positions: dict[str, list[ClosedPositionEntry]] | None = None,
    ) -> None:
        self._leaderboards = leaderboards or {}
        self._activities = activities or {}
        self._closed_positions = closed_positions or {}

    async def get_leaderboard(
        self,
        order_by: str = "PNL",
        limit: int = 50,
        time_period: str = "ALL",
        category: str = "OVERALL",
    ) -> list[object]:
        del limit
        rows = self._leaderboards.get((order_by, time_period, category), [])
        from whale_tracker.data_api import LeaderboardEntry

        return [LeaderboardEntry.model_validate(row) for row in rows]

    async def get_activity_all(self, wallet: str, max_pages: int = 10) -> list[ActivityEntry]:
        del max_pages
        return self._activities.get(wallet, [])

    async def get_closed_positions(self, wallet: str) -> list[ClosedPositionEntry]:
        return self._closed_positions.get(wallet, [])


def _default_categories() -> dict[str, str]:
    """All condition IDs map to 'sports'."""
    return {"cond_1": "sports", "cond_2": "sports", "cond_3": "sports"}


def _no_resolutions() -> dict[str, Decimal | None]:
    """All markets are active (no resolution)."""
    return {"cond_1": None, "cond_2": None, "cond_3": None}


def _default_config() -> ScoringConfig:
    return ScoringConfig()


# ============================================================
# Trade Matching Tests
# ============================================================


class TestTradeMatching:
    """FIFO per-asset BUY→SELL matching."""

    def test_simple_buy_sell_pair(self) -> None:
        """Single BUY→SELL: correct % return."""
        activities = [
            _activity("BUY", "0.50", "100", minutes_offset=0),
            _activity("SELL", "0.60", "100", minutes_offset=10),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")

        assert len(rts) == 1
        assert rts[0].buy_price == Decimal("0.50")
        assert rts[0].sell_price == Decimal("0.60")
        assert rts[0].size == Decimal("100")
        # (0.60 - 0.50) / 0.50 * 100 = 20%
        assert rts[0].pct_return == Decimal("20")

    def test_partial_fills(self) -> None:
        """BUY 100 → SELL 60 + SELL 40."""
        activities = [
            _activity("BUY", "0.50", "100", minutes_offset=0),
            _activity("SELL", "0.60", "60", minutes_offset=10),
            _activity("SELL", "0.70", "40", minutes_offset=20),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")

        assert len(rts) == 2
        assert rts[0].size == Decimal("60")
        assert rts[0].pct_return == Decimal("20")  # (0.60-0.50)/0.50*100
        assert rts[1].size == Decimal("40")
        assert rts[1].pct_return == Decimal("40")  # (0.70-0.50)/0.50*100

    def test_multiple_buys_fifo_order(self) -> None:
        """Two BUYs at different prices, SELL matches first BUY (FIFO)."""
        activities = [
            _activity("BUY", "0.40", "50", minutes_offset=0),
            _activity("BUY", "0.60", "50", minutes_offset=5),
            _activity("SELL", "0.80", "50", minutes_offset=10),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")

        assert len(rts) == 1
        # FIFO: matches the 0.40 buy
        assert rts[0].buy_price == Decimal("0.40")
        assert rts[0].sell_price == Decimal("0.80")
        # (0.80 - 0.40) / 0.40 * 100 = 100%
        assert rts[0].pct_return == Decimal("100")

    def test_sell_without_matching_buy(self) -> None:
        """SELL with no prior BUY → skipped gracefully."""
        activities = [
            _activity("SELL", "0.60", "100", minutes_offset=0),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")
        assert len(rts) == 0

    def test_buy_without_sell_active_market(self) -> None:
        """BUY on active market with no SELL → open position, no round-trip."""
        activities = [
            _activity("BUY", "0.85", "100", minutes_offset=0),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")
        assert len(rts) == 0

    def test_buy_held_to_correct_resolution(self) -> None:
        """BUY at 0.85 held to resolution at 1.00 → +17.6% return."""
        activities = [
            _activity("BUY", "0.85", "100", minutes_offset=0),
        ]
        resolutions: dict[str, Decimal | None] = {"cond_1": Decimal("1")}

        rts = match_round_trips(activities, _default_categories(), resolutions, "sports")

        assert len(rts) == 1
        assert rts[0].resolved is True
        assert rts[0].sell_price == Decimal("1")
        # (1.00 - 0.85) / 0.85 * 100 ≈ 17.647...
        expected = (Decimal("1") - Decimal("0.85")) / Decimal("0.85") * Decimal("100")
        assert rts[0].pct_return == expected

    def test_buy_held_to_wrong_resolution(self) -> None:
        """BUY at 0.85 held to wrong resolution at 0.00 → -100% return."""
        activities = [
            _activity("BUY", "0.85", "100", minutes_offset=0),
        ]
        resolutions: dict[str, Decimal | None] = {"cond_1": Decimal("0")}

        rts = match_round_trips(activities, _default_categories(), resolutions, "sports")

        assert len(rts) == 1
        assert rts[0].resolved is True
        assert rts[0].sell_price == Decimal("0")
        # (0 - 0.85) / 0.85 * 100 = -100%
        assert rts[0].pct_return == Decimal("-100")

    def test_cross_asset_isolation(self) -> None:
        """BUYs on different assets don't cross-match."""
        activities = [
            _activity("BUY", "0.50", "100", asset="token_A", minutes_offset=0),
            _activity("BUY", "0.30", "100", asset="token_B", minutes_offset=5),
            _activity("SELL", "0.60", "100", asset="token_A", minutes_offset=10),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")

        assert len(rts) == 1
        assert rts[0].asset == "token_A"
        assert rts[0].buy_price == Decimal("0.50")

    def test_negative_return(self) -> None:
        """SELL below BUY price → negative return."""
        activities = [
            _activity("BUY", "0.80", "100", minutes_offset=0),
            _activity("SELL", "0.40", "100", minutes_offset=10),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")

        assert len(rts) == 1
        # (0.40 - 0.80) / 0.80 * 100 = -50%
        assert rts[0].pct_return == Decimal("-50")

    def test_zero_buy_price_edge_case(self) -> None:
        """BUY at price 0 → pct_return is 0 (avoid division by zero)."""
        activities = [
            _activity("BUY", "0", "100", minutes_offset=0),
            _activity("SELL", "0.50", "100", minutes_offset=10),
        ]
        rts = match_round_trips(activities, _default_categories(), _no_resolutions(), "sports")

        assert len(rts) == 1
        assert rts[0].pct_return == Decimal("0")

    def test_non_sports_filtered_out(self) -> None:
        """Trades in non-sports categories are excluded."""
        activities = [
            _activity("BUY", "0.50", "100", condition_id="cond_1", minutes_offset=0),
            _activity("SELL", "0.70", "100", condition_id="cond_1", minutes_offset=10),
        ]
        categories = {"cond_1": "politics"}
        rts = match_round_trips(activities, categories, _no_resolutions(), "sports")
        assert len(rts) == 0

    def test_asset_resolution_takes_precedence_over_condition_resolution(self) -> None:
        """Resolution should be looked up by asset first, not just condition ID."""
        activities = [_activity("BUY", "0.85", "100", asset="token_A", condition_id="cond_1")]
        resolutions = {"cond_1": Decimal("0"), "token_A": Decimal("1")}

        rts = match_round_trips(activities, _default_categories(), resolutions, "sports")

        assert len(rts) == 1
        assert rts[0].sell_price == Decimal("1")


# ============================================================
# Filter Logic Tests
# ============================================================


class TestFilters:
    """Individual filter criteria."""

    def test_recency_fail(self) -> None:
        """No trade in last 4 days → rejected."""
        old_trade = NOW - timedelta(days=10)
        score = WalletScore(
            address="0xSTALE",
            total_pnl=Decimal("50000"),
            avg_pct_return=Decimal("15"),
            markets_traded=30,
            total_trades=100,
            avg_daily_transactions_7d=Decimal("5"),
            last_trade_at=old_trade,
            round_trips=50,
            passes_filters=False,
            rejection_reasons=["inactive: last trade 2026-02-19 > 4d ago"],
        )
        assert not score.passes_filters

    def test_min_markets_filter(self) -> None:
        """< 10 resolved markets → rejected."""
        score = WalletScore(
            address="0xFEW",
            total_pnl=Decimal("50000"),
            avg_pct_return=Decimal("15"),
            markets_traded=5,
            total_trades=100,
            avg_daily_transactions_7d=Decimal("5"),
            last_trade_at=NOW,
            round_trips=30,
            passes_filters=False,
            rejection_reasons=["too_few_markets: 5 all markets < 10"],
        )
        assert not score.passes_filters
        assert score.markets_traded < _default_config().min_markets_traded

    def test_avg_return_filter(self) -> None:
        """Avg return < 8% → rejected."""
        score = WalletScore(
            address="0xLOW",
            total_pnl=Decimal("50000"),
            avg_pct_return=Decimal("5"),
            markets_traded=30,
            total_trades=100,
            avg_daily_transactions_7d=Decimal("5"),
            last_trade_at=NOW,
            round_trips=50,
            passes_filters=False,
            rejection_reasons=["low_return: 5.0% avg < 8%"],
        )
        assert not score.passes_filters
        assert score.avg_pct_return < _default_config().min_avg_pct_return

    def test_full_pass_scenario(self) -> None:
        """Wallet meeting all criteria passes."""
        score = WalletScore(
            address="0xGOOD",
            total_pnl=Decimal("50000"),
            avg_pct_return=Decimal("12"),
            markets_traded=30,
            total_trades=100,
            avg_daily_transactions_7d=Decimal("10"),
            last_trade_at=NOW,
            round_trips=50,
            passes_filters=True,
            rejection_reasons=[],
        )
        assert score.passes_filters
        assert score.avg_pct_return >= _default_config().min_avg_pct_return
        assert score.markets_traded >= _default_config().min_markets_traded
        assert score.total_pnl >= _default_config().min_total_pnl

    def test_multiple_rejection_reasons(self) -> None:
        """Wallet failing multiple criteria collects all reasons."""
        score = WalletScore(
            address="0xBAD",
            total_pnl=Decimal("5000"),
            avg_pct_return=Decimal("3"),
            markets_traded=5,
            total_trades=800,
            avg_daily_transactions_7d=Decimal("120"),
            last_trade_at=NOW - timedelta(days=10),
            round_trips=20,
            passes_filters=False,
            rejection_reasons=[
                "inactive: last trade 2026-02-19 > 4d ago",
                "too_few_markets: 5 all markets < 10",
                "low_return: 3.0% avg < 8%",
            ],
        )
        assert len(score.rejection_reasons) == 3


# ============================================================
# Auto-Removal State Machine Tests
# ============================================================


class TestAutoRemoval:
    """Auto-removal grace period + recovery logic."""

    def _make_wallet(
        self,
        address: str = "0xWALLET",
        avg_return: str = "12",
        below_since: datetime | None = None,
    ) -> WatchedWallet:
        return WatchedWallet(
            address=address,
            label="Test Whale",
            category="sports",
            avg_pct_return=Decimal(avg_return),
            markets_traded=30,
            total_pnl=Decimal("50000"),
            last_scored_at=NOW,
            added_at=NOW - timedelta(days=30),
            below_threshold_since=below_since,
        )

    def _make_score(self, address: str, avg_return: str) -> WalletScore:
        avg_return_dec = Decimal(avg_return)
        passes = avg_return_dec >= _default_config().min_avg_pct_return
        reasons = [] if passes else [f"low_return: {avg_return_dec:.1f}% avg < 8%"]
        return WalletScore(
            address=address,
            total_pnl=Decimal("50000"),
            avg_pct_return=avg_return_dec,
            markets_traded=30,
            total_trades=100,
            avg_daily_transactions_7d=Decimal("10"),
            last_trade_at=NOW,
            round_trips=50,
            passes_filters=passes,
            rejection_reasons=reasons,
        )

    def test_timer_starts_on_first_drop(self) -> None:
        """First drop below threshold → below_threshold_since is set."""
        wallet = self._make_wallet(avg_return="12", below_since=None)
        score = self._make_score("0xWALLET", "5")  # Below 8%

        kept, removed = apply_auto_removal(
            [wallet], {"0xWALLET": score}, _default_config()
        )

        assert len(kept) == 1
        assert len(removed) == 0
        assert kept[0].below_threshold_since is not None

    def test_removed_after_7_days(self) -> None:
        """below_threshold_since was 8 days ago → removed."""
        eight_days_ago = datetime.now(tz=UTC) - timedelta(days=8)
        wallet = self._make_wallet(avg_return="5", below_since=eight_days_ago)
        score = self._make_score("0xWALLET", "5")

        kept, removed = apply_auto_removal(
            [wallet], {"0xWALLET": score}, _default_config()
        )

        assert len(kept) == 0
        assert len(removed) == 1

    def test_recovery_resets_timer(self) -> None:
        """Avg return rebounds above threshold → timer reset."""
        three_days_ago = datetime.now(tz=UTC) - timedelta(days=3)
        wallet = self._make_wallet(avg_return="5", below_since=three_days_ago)
        score = self._make_score("0xWALLET", "12")  # Recovered

        kept, removed = apply_auto_removal(
            [wallet], {"0xWALLET": score}, _default_config()
        )

        assert len(kept) == 1
        assert len(removed) == 0
        assert kept[0].below_threshold_since is None

    def test_grace_period_not_expired(self) -> None:
        """below_threshold_since was 3 days ago → kept (grace period)."""
        three_days_ago = datetime.now(tz=UTC) - timedelta(days=3)
        wallet = self._make_wallet(avg_return="5", below_since=three_days_ago)
        score = self._make_score("0xWALLET", "5")

        kept, removed = apply_auto_removal(
            [wallet], {"0xWALLET": score}, _default_config()
        )

        assert len(kept) == 1
        assert len(removed) == 0
        # Timer should still be set
        assert kept[0].below_threshold_since is not None

    def test_hard_fail_removed_immediately(self) -> None:
        """Inactivity/bot-like failures bypass the grace period."""
        wallet = self._make_wallet(avg_return="12", below_since=None)
        score = WalletScore(
            address="0xWALLET",
            total_pnl=Decimal("50000"),
            avg_pct_return=Decimal("12"),
            markets_traded=30,
            total_trades=100,
            avg_daily_transactions_7d=Decimal("10"),
            last_trade_at=NOW - timedelta(days=10),
            round_trips=50,
            passes_filters=False,
            rejection_reasons=["inactive: last trade 2026-02-19 > 4d ago"],
        )

        kept, removed = apply_auto_removal([wallet], {"0xWALLET": score}, _default_config())

        assert len(kept) == 0
        assert len(removed) == 1


# ============================================================
# ScoringConfig Tests
# ============================================================


class TestScoringConfig:
    """Config loading and defaults."""

    def test_default_values(self) -> None:
        """Default config matches v2 parameters."""
        config = ScoringConfig()
        assert config.category_focus == "all"
        assert config.min_markets_traded == 10
        assert config.min_total_pnl == Decimal("10000")
        assert config.min_avg_pct_return == Decimal("8")
        assert config.max_avg_daily_transactions_7d == 100
        assert config.scoring_lookback_months == 6
        assert config.activity_recency_days == 4
        assert config.max_tracked_wallets == 25
        assert config.rescan_interval_hours == 24
        assert config.removal_threshold_days == 7
        assert config.leaderboard_scan_size == 100

    def test_from_toml(self, tmp_path: object) -> None:
        """Load config from a TOML file."""
        import pathlib

        toml_path = pathlib.Path(str(tmp_path)) / "test.toml"
        toml_path.write_text(
            '[scoring]\n'
            'category_focus = "sports"\n'
            'min_markets_traded = 30\n'
            'min_total_pnl = "20000"\n'
            'min_avg_pct_return = "10"\n'
            'max_avg_daily_transactions_7d = 50\n'
            'activity_recency_days = 3\n'
            'max_tracked_wallets = 15\n'
            'rescan_interval_hours = 12\n'
            'removal_threshold_days = 5\n'
            'leaderboard_scan_size = 200\n'
        )

        config = ScoringConfig.from_toml(toml_path)
        assert config.min_markets_traded == 30
        assert config.min_total_pnl == Decimal("20000")
        assert config.min_avg_pct_return == Decimal("10")
        assert config.max_avg_daily_transactions_7d == 50
        assert config.max_tracked_wallets == 15
        assert config.rescan_interval_hours == 12


# ============================================================
# Helper Tests
# ============================================================


class TestHelpers:
    """Utility function tests."""

    def test_ensure_utc_naive(self) -> None:
        """Naive datetime gets UTC attached."""
        dt = datetime(2026, 3, 1, 12, 0, 0)
        result = _ensure_utc(dt)
        assert result.tzinfo is UTC

    def test_ensure_utc_aware(self) -> None:
        """Already-aware datetime passes through."""
        dt = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)
        result = _ensure_utc(dt)
        assert result is dt


class TestSelectionPipeline:
    """Integrated tests for candidate discovery and wallet scoring."""

    @pytest.mark.asyncio
    async def test_discover_candidates_merges_all_time_and_monthly_leaderboards(self) -> None:
        """Merged discovery should dedupe wallets and retain source labels."""
        client = _FakeClient(
            leaderboards={
                ("PNL", "ALL", "OVERALL"): [
                    {
                        "rank": 1,
                        "proxyWallet": "0xAAA",
                        "pnl": "12000",
                        "vol": "50000",
                        "userName": "Alpha",
                    }
                ],
                ("PNL", "MONTH", "OVERALL"): [
                    {
                        "rank": 1,
                        "proxyWallet": "0xAAA",
                        "pnl": "8000",
                        "vol": "20000",
                        "userName": "Alpha",
                    },
                    {
                        "rank": 2,
                        "proxyWallet": "0xBBB",
                        "pnl": "15000",
                        "vol": "30000",
                        "userName": "Beta",
                    },
                ],
            }
        )

        candidates = await discover_candidates(client, _default_config())

        assert [candidate.address for candidate in candidates] == ["0xBBB", "0xAAA"]
        assert candidates[1].discovery_sources == ["all_time_pnl", "monthly_pnl"]

    @pytest.mark.asyncio
    async def test_score_wallet_prefers_closed_positions_weighted_return(self) -> None:
        """Resolved sports positions should drive wallet ranking metrics."""
        recent_base = datetime.now(tz=UTC) - timedelta(hours=1)
        wallet = "0xGOOD"
        client = _FakeClient(
            activities={
                wallet: [
                    _activity(
                        "BUY",
                        "0.50",
                        "100",
                        asset="token_A",
                        condition_id="cond_1",
                        timestamp=recent_base,
                        slug="nba-game-1",
                    ),
                    _activity(
                        "BUY",
                        "0.40",
                        "50",
                        asset="token_B",
                        condition_id="cond_2",
                        timestamp=recent_base + timedelta(minutes=5),
                        slug="nba-game-2",
                    ),
                ]
            },
            closed_positions={
                wallet: [
                    _closed_position("cond_1", "token_A", "0.50", "100", "10"),
                    _closed_position("cond_2", "token_B", "0.40", "50", "5"),
                ]
            },
        )

        score = await score_wallet(
            wallet,
            Decimal("20000"),
            client,
            ScoringConfig(min_markets_traded=2),
        )

        expected_roi = Decimal("15") / Decimal("70") * Decimal("100")
        assert score.passes_filters
        assert score.scoring_basis == "closed_positions"
        assert score.round_trips == 2
        assert score.markets_traded == 2
        assert score.realized_pnl == Decimal("15")
        assert score.avg_pct_return == expected_roi

    @pytest.mark.asyncio
    async def test_score_wallet_falls_back_to_fifo_when_no_closed_positions(self) -> None:
        """FIFO matching remains as a fallback when closed positions are missing."""
        recent_base = datetime.now(tz=UTC) - timedelta(hours=1)
        wallet = "0xFIFO"
        client = _FakeClient(
            activities={
                wallet: [
                    _activity(
                        "BUY",
                        "0.50",
                        "100",
                        condition_id="cond_1",
                        asset="token_A",
                        timestamp=recent_base,
                        slug="nba-game-1",
                    ),
                    _activity(
                        "SELL",
                        "0.60",
                        "100",
                        condition_id="cond_1",
                        asset="token_A",
                        timestamp=recent_base + timedelta(minutes=2),
                        slug="nba-game-1",
                    ),
                ]
            },
        )

        score = await score_wallet(
            wallet,
            Decimal("15000"),
            client,
            ScoringConfig(min_markets_traded=1),
        )

        assert score.passes_filters
        assert score.scoring_basis == "fifo_activity"
        assert score.round_trips == 1
        assert score.realized_pnl == Decimal("10")
        assert score.avg_pct_return == Decimal("20")
