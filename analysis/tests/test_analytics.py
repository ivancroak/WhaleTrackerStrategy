"""Tests for the pandas/NumPy feature-engineering layer.

These assert hand-computed expected values for the engineered metrics, not
just smoke behaviour: a known win rate, a known mean/std (Sharpe-like and
volatility), and the exact exponential recency-decay weight.
"""

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")

from whale_tracker.analytics import (  # noqa: E402  (after importorskip guard)
    FEATURE_COLUMNS,
    RECENCY_HALF_LIFE_DAYS,
    build_feature_frame,
)
from whale_tracker.scorer import RoundTripTrade, WalletScore  # noqa: E402

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _score(
    address: str,
    *,
    avg_pct_return: str,
    realized_pnl: str,
    total_pnl: str,
    markets_traded: int,
    total_trades: int,
    round_trips: int,
    last_trade_at: datetime | None,
) -> WalletScore:
    """Build a WalletScore with the fields analytics reads."""
    return WalletScore(
        address=address,
        total_pnl=Decimal(total_pnl),
        avg_pct_return=Decimal(avg_pct_return),
        markets_traded=markets_traded,
        total_trades=total_trades,
        avg_daily_transactions_7d=Decimal("5"),
        last_trade_at=last_trade_at,
        round_trips=round_trips,
        realized_pnl=Decimal(realized_pnl),
        passes_filters=True,
    )


def _rt(pct_return: str) -> RoundTripTrade:
    """Build a round-trip carrying only the fields analytics needs."""
    return RoundTripTrade(
        asset="token_A",
        buy_price=Decimal("0.50"),
        sell_price=Decimal("0.60"),
        size=Decimal("100"),
        pct_return=Decimal(pct_return),
    )


class TestFrameShape:
    """Structural guarantees of the feature frame."""

    def test_columns_and_index(self) -> None:
        """One row per wallet, address index, exact column set."""
        scores = [
            _score(
                "0xAAA",
                avg_pct_return="12",
                realized_pnl="5000",
                total_pnl="40000",
                markets_traded=30,
                total_trades=120,
                round_trips=50,
                last_trade_at=NOW,
            ),
            _score(
                "0xBBB",
                avg_pct_return="8",
                realized_pnl="2000",
                total_pnl="20000",
                markets_traded=15,
                total_trades=60,
                round_trips=25,
                last_trade_at=NOW,
            ),
        ]
        frame = build_feature_frame(scores, now=NOW)

        assert list(frame.columns) == list(FEATURE_COLUMNS)
        assert frame.index.name == "address"
        assert list(frame.index) == ["0xAAA", "0xBBB"]
        assert frame.shape == (2, len(FEATURE_COLUMNS))

    def test_empty_input_keeps_columns(self) -> None:
        """Empty input yields a 0-row frame with the full column set."""
        frame = build_feature_frame([], now=NOW)
        assert frame.shape[0] == 0
        assert list(frame.columns) == list(FEATURE_COLUMNS)

    def test_passthrough_columns_match_score(self) -> None:
        """roi/realized_pnl/total_pnl mirror the WalletScore Decimals as float."""
        score = _score(
            "0xAAA",
            avg_pct_return="12.5",
            realized_pnl="5000",
            total_pnl="40000",
            markets_traded=30,
            total_trades=120,
            round_trips=50,
            last_trade_at=NOW,
        )
        frame = build_feature_frame([score], now=NOW)
        assert frame.loc["0xAAA", "roi"] == pytest.approx(12.5)
        assert frame.loc["0xAAA", "realized_pnl"] == pytest.approx(5000.0)
        assert frame.loc["0xAAA", "total_pnl"] == pytest.approx(40000.0)
        assert frame.loc["0xAAA", "trade_count"] == pytest.approx(120.0)
        assert frame.loc["0xAAA", "markets_traded"] == pytest.approx(30.0)


class TestEngineeredStatistics:
    """Hand-computed win rate, volatility, and Sharpe-like ratio."""

    def test_win_rate_volatility_sharpe(self) -> None:
        """Returns [+20, -50, +40]: win_rate=2/3, std/mean match NumPy."""
        returns = [20.0, -50.0, 40.0]
        score = _score(
            "0xAAA",
            avg_pct_return="3.3333",
            realized_pnl="1000",
            total_pnl="40000",
            markets_traded=3,
            total_trades=6,
            round_trips=3,
            last_trade_at=NOW,
        )
        rts = {"0xAAA": [_rt("20"), _rt("-50"), _rt("40")]}
        frame = build_feature_frame([score], round_trips_by_address=rts, now=NOW)

        # win_rate: 2 of 3 trips are positive.
        assert frame.loc["0xAAA", "win_rate"] == pytest.approx(2.0 / 3.0)

        # pnl_volatility: population std of the per-trade returns.
        expected_std = float(np.std(np.asarray(returns)))
        assert frame.loc["0xAAA", "pnl_volatility"] == pytest.approx(expected_std)

        # sharpe_like: mean / std of the per-trade returns.
        expected_sharpe = float(np.mean(returns) / np.std(returns))
        assert frame.loc["0xAAA", "sharpe_like"] == pytest.approx(expected_sharpe)

    def test_distribution_columns_nan_without_round_trips(self) -> None:
        """Without round-trips, distribution columns are NaN (not 0)."""
        score = _score(
            "0xAAA",
            avg_pct_return="12",
            realized_pnl="5000",
            total_pnl="40000",
            markets_traded=30,
            total_trades=120,
            round_trips=50,
            last_trade_at=NOW,
        )
        frame = build_feature_frame([score], now=NOW)
        assert np.isnan(frame.loc["0xAAA", "win_rate"])
        assert np.isnan(frame.loc["0xAAA", "pnl_volatility"])
        assert np.isnan(frame.loc["0xAAA", "sharpe_like"])

    def test_single_round_trip_volatility_nan(self) -> None:
        """A single round-trip cannot define dispersion → NaN volatility."""
        score = _score(
            "0xAAA",
            avg_pct_return="20",
            realized_pnl="100",
            total_pnl="40000",
            markets_traded=1,
            total_trades=2,
            round_trips=1,
            last_trade_at=NOW,
        )
        frame = build_feature_frame([score], round_trips_by_address={"0xAAA": [_rt("20")]}, now=NOW)
        # win_rate is defined (1/1 = 1.0) but volatility/sharpe need >= 2 trips.
        assert frame.loc["0xAAA", "win_rate"] == pytest.approx(1.0)
        assert np.isnan(frame.loc["0xAAA", "pnl_volatility"])
        assert np.isnan(frame.loc["0xAAA", "sharpe_like"])

    def test_all_losses_zero_win_rate(self) -> None:
        """All-negative round-trips yield a genuine 0.0 win rate."""
        score = _score(
            "0xAAA",
            avg_pct_return="-30",
            realized_pnl="-100",
            total_pnl="40000",
            markets_traded=2,
            total_trades=4,
            round_trips=2,
            last_trade_at=NOW,
        )
        frame = build_feature_frame(
            [score],
            round_trips_by_address={"0xAAA": [_rt("-20"), _rt("-40")]},
            now=NOW,
        )
        assert frame.loc["0xAAA", "win_rate"] == pytest.approx(0.0)


class TestRecencyWeight:
    """Exponential decay on days-since-last-trade."""

    def test_zero_days_weight_is_one(self) -> None:
        """A wallet that traded exactly at `now` has recency weight 1.0."""
        score = _score(
            "0xAAA",
            avg_pct_return="12",
            realized_pnl="5000",
            total_pnl="40000",
            markets_traded=30,
            total_trades=120,
            round_trips=50,
            last_trade_at=NOW,
        )
        frame = build_feature_frame([score], now=NOW)
        assert frame.loc["0xAAA", "recency_weight"] == pytest.approx(1.0)

    def test_half_life_weight_is_half(self) -> None:
        """At exactly one half-life old, the weight equals 0.5."""
        old = NOW - timedelta(days=RECENCY_HALF_LIFE_DAYS)
        score = _score(
            "0xAAA",
            avg_pct_return="12",
            realized_pnl="5000",
            total_pnl="40000",
            markets_traded=30,
            total_trades=120,
            round_trips=50,
            last_trade_at=old,
        )
        frame = build_feature_frame([score], now=NOW)
        assert frame.loc["0xAAA", "recency_weight"] == pytest.approx(0.5)

    def test_no_last_trade_weight_is_zero(self) -> None:
        """A wallet with no last-trade timestamp is fully decayed (0.0)."""
        score = _score(
            "0xAAA",
            avg_pct_return="12",
            realized_pnl="5000",
            total_pnl="40000",
            markets_traded=30,
            total_trades=120,
            round_trips=50,
            last_trade_at=None,
        )
        frame = build_feature_frame([score], now=NOW)
        assert frame.loc["0xAAA", "recency_weight"] == pytest.approx(0.0)


class TestAvgHoldHours:
    """avg_hold_hours derived from raw activity timestamps."""

    def test_mean_gap_between_trades(self) -> None:
        """Three trades 2h apart → mean consecutive gap of 2.0 hours."""
        from whale_tracker.data_api import ActivityEntry

        def _act(offset_hours: int) -> ActivityEntry:
            return ActivityEntry.model_validate(
                {
                    "proxyWallet": "0xAAA",
                    "conditionId": "cond_1",
                    "asset": "token_A",
                    "side": "BUY",
                    "size": "100",
                    "usdcSize": "50",
                    "price": "0.5",
                    "timestamp": NOW + timedelta(hours=offset_hours),
                }
            )

        score = _score(
            "0xAAA",
            avg_pct_return="12",
            realized_pnl="5000",
            total_pnl="40000",
            markets_traded=30,
            total_trades=3,
            round_trips=2,
            last_trade_at=NOW,
        )
        acts = {"0xAAA": [_act(0), _act(2), _act(4)]}
        frame = build_feature_frame([score], activities_by_address=acts, now=NOW)
        assert frame.loc["0xAAA", "avg_hold_hours"] == pytest.approx(2.0)

    def test_nan_without_activities(self) -> None:
        """No activity stream → avg_hold_hours is NaN (not derivable)."""
        score = _score(
            "0xAAA",
            avg_pct_return="12",
            realized_pnl="5000",
            total_pnl="40000",
            markets_traded=30,
            total_trades=120,
            round_trips=50,
            last_trade_at=NOW,
        )
        frame = build_feature_frame([score], now=NOW)
        assert np.isnan(frame.loc["0xAAA", "avg_hold_hours"])


class TestCompositeScore:
    """The heuristic composite score column."""

    def test_higher_pnl_wallet_scores_higher(self) -> None:
        """On a clearly separable pair, the stronger wallet scores higher."""
        strong = _score(
            "0xSTRONG",
            avg_pct_return="40",
            realized_pnl="90000",
            total_pnl="120000",
            markets_traded=60,
            total_trades=300,
            round_trips=120,
            last_trade_at=NOW,
        )
        weak = _score(
            "0xWEAK",
            avg_pct_return="2",
            realized_pnl="500",
            total_pnl="11000",
            markets_traded=11,
            total_trades=20,
            round_trips=8,
            last_trade_at=NOW - timedelta(days=20),
        )
        rts = {
            "0xSTRONG": [_rt("40"), _rt("50"), _rt("30")],
            "0xWEAK": [_rt("2"), _rt("-5"), _rt("4")],
        }
        frame = build_feature_frame([strong, weak], round_trips_by_address=rts, now=NOW)
        assert frame.loc["0xSTRONG", "score"] > frame.loc["0xWEAK", "score"]
