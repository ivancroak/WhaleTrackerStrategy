"""Tests for the runtime-trained whale performance ranker.

These build a small, clearly-separable synthetic dataset, fit the model, and
assert:
* predict() returns a finite array of the right shape,
* ranking is sensible (high-label wallets rank above low-label wallets),
* the insufficient-data path falls back to the deterministic heuristic without
  raising, and predict() still works on an unfit model.
"""

from datetime import UTC, datetime

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")

from whale_tracker.analytics import build_feature_frame  # noqa: E402
from whale_tracker.model import (  # noqa: E402
    MIN_SAMPLES,
    RankedWallet,
    WhalePerformanceModel,
)
from whale_tracker.scorer import WalletScore  # noqa: E402

NOW = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)


def _score(address: str, *, roi: float, realized: float, markets: int, trades: int) -> WalletScore:
    """Build a WalletScore whose strength scales with `roi`/`realized`."""
    from decimal import Decimal

    return WalletScore(
        address=address,
        total_pnl=Decimal(str(realized * 1.5)),
        avg_pct_return=Decimal(str(roi)),
        markets_traded=markets,
        total_trades=trades,
        avg_daily_transactions_7d=Decimal("5"),
        last_trade_at=NOW,
        round_trips=markets,
        realized_pnl=Decimal(str(realized)),
        passes_filters=True,
    )


def _separable_dataset(n: int) -> tuple[pd.DataFrame, np.ndarray]:
    """Build `n` wallets whose realized PnL grows linearly with strength.

    The label is realized PnL; features (roi, realized_pnl, etc.) increase with
    the same index, so the relationship is trivially learnable.
    """
    scores: list[WalletScore] = []
    labels: list[float] = []
    for i in range(n):
        realized = 1000.0 * (i + 1)
        scores.append(
            _score(
                f"0x{i:04d}",
                roi=5.0 + 2.0 * i,
                realized=realized,
                markets=10 + i,
                trades=50 + 5 * i,
            )
        )
        labels.append(realized)
    frame = build_feature_frame(scores, now=NOW)
    return frame, np.asarray(labels, dtype=float)


class TestFitPredict:
    """Training and prediction on a separable toy dataset."""

    def test_predict_shape_and_finite(self) -> None:
        """predict() returns a finite 1-D array, one entry per wallet."""
        frame, labels = _separable_dataset(12)
        model = WhalePerformanceModel(random_state=0).fit(frame, labels)

        assert model.is_fitted
        preds = model.predict(frame)
        assert isinstance(preds, np.ndarray)
        assert preds.shape == (12,)
        assert np.all(np.isfinite(preds))

    def test_ranking_orders_strong_above_weak(self) -> None:
        """The highest-label wallet ranks above the lowest-label wallet."""
        frame, labels = _separable_dataset(12)
        model = WhalePerformanceModel(random_state=0).fit(frame, labels)

        ranked = model.rank(frame)
        assert all(isinstance(r, RankedWallet) for r in ranked)
        assert len(ranked) == 12
        # Predictions are non-increasing down the ranking.
        preds = [r.predicted for r in ranked]
        assert preds == sorted(preds, reverse=True)
        # Strongest synthetic wallet (highest index) ranks above the weakest.
        order = [r.address for r in ranked]
        assert order.index("0x0011") < order.index("0x0000")
        # A trained model reports model provenance.
        assert all(r.used_model for r in ranked)

    def test_predictions_correlate_with_labels(self) -> None:
        """Predicted scores are strongly rank-correlated with true labels."""
        frame, labels = _separable_dataset(16)
        model = WhalePerformanceModel(random_state=0).fit(frame, labels)
        preds = model.predict(frame)
        # Pearson correlation on a clean monotonic relationship should be high.
        corr = float(np.corrcoef(preds, labels)[0, 1])
        assert corr > 0.9

    def test_score_series_aligned_to_index(self) -> None:
        """score() returns a Series aligned to the feature-frame index."""
        frame, labels = _separable_dataset(10)
        model = WhalePerformanceModel(random_state=0).fit(frame, labels)
        series = model.score(frame)
        assert isinstance(series, pd.Series)
        assert list(series.index) == list(frame.index)


class TestFallback:
    """Graceful heuristic fallback when data is insufficient or unfit."""

    def test_insufficient_data_stays_unfit(self) -> None:
        """Fewer than MIN_SAMPLES rows → model stays in fallback mode."""
        n = MIN_SAMPLES - 1
        frame, labels = _separable_dataset(n)
        model = WhalePerformanceModel(random_state=0).fit(frame, labels)

        assert not model.is_fitted
        preds = model.predict(frame)
        assert preds.shape == (n,)
        assert np.all(np.isfinite(preds))

    def test_predict_before_fit_uses_fallback(self) -> None:
        """predict() on a never-fitted model falls back, does not raise."""
        frame, _ = _separable_dataset(12)
        model = WhalePerformanceModel(random_state=0)

        preds, used_model = model.predict_with_provenance(frame)
        assert used_model is False
        assert preds.shape == (12,)
        assert np.all(np.isfinite(preds))

    def test_fallback_ranking_is_sensible(self) -> None:
        """Even unfit, the heuristic ranks the strong wallet above the weak."""
        frame, _ = _separable_dataset(12)
        model = WhalePerformanceModel(random_state=0)  # never fitted
        ranked = model.rank(frame)
        order = [r.address for r in ranked]
        assert order.index("0x0011") < order.index("0x0000")
        assert all(not r.used_model for r in ranked)

    def test_empty_frame_predicts_empty(self) -> None:
        """An empty feature frame yields an empty prediction array."""
        empty = build_feature_frame([], now=NOW)
        model = WhalePerformanceModel(random_state=0)
        preds = model.predict(empty)
        assert preds.shape == (0,)


class TestValidation:
    """Input validation."""

    def test_label_length_mismatch_raises(self) -> None:
        """fit() rejects a label vector that does not match the row count."""
        frame, labels = _separable_dataset(12)
        model = WhalePerformanceModel(random_state=0)
        with pytest.raises(ValueError):
            model.fit(frame, labels[:-1])


class TestScorerWiring:
    """The pandas/model entry points exposed on the scorer module."""

    def test_scorer_build_feature_frame(self) -> None:
        """scorer.build_feature_frame delegates to analytics (pandas path)."""
        from whale_tracker import scorer

        scores = [
            _score("0xAAA", roi=10.0, realized=5000.0, markets=20, trades=80),
            _score("0xBBB", roi=4.0, realized=1000.0, markets=12, trades=40),
        ]
        built = scorer.build_feature_frame(scores, now=NOW)
        assert list(built.index) == ["0xAAA", "0xBBB"]
        assert "score" in built.columns

    def test_rank_candidates_default_is_unchanged(self) -> None:
        """use_model=False ranks by the existing _wallet_rank_key order."""
        from whale_tracker import scorer

        weak = _score("0xWEAK", roi=4.0, realized=1000.0, markets=12, trades=40)
        strong = _score("0xSTRONG", roi=30.0, realized=80000.0, markets=50, trades=200)
        # Default path must match a direct sort by the rank key — no pandas/model.
        ranked = scorer.rank_candidates([weak, strong])
        expected = sorted([weak, strong], key=scorer._wallet_rank_key, reverse=True)
        assert [s.address for s in ranked] == [s.address for s in expected]
        assert ranked[0].address == "0xSTRONG"

    def test_rank_candidates_with_model(self) -> None:
        """use_model=True reranks via the fitted model on a separable set."""
        from whale_tracker import scorer

        # Train a model on a clean separable dataset.
        train_frame, train_labels = _separable_dataset(12)
        model = WhalePerformanceModel(random_state=0).fit(train_frame, train_labels)
        assert model.is_fitted

        weak = _score("0x0000", roi=5.0, realized=1000.0, markets=10, trades=50)
        strong = _score("0x0011", roi=27.0, realized=12000.0, markets=21, trades=105)
        ranked = scorer.rank_candidates([weak, strong], use_model=True, model=model)
        # The model-predicted-stronger wallet should come first.
        assert ranked[0].address == "0x0011"

    def test_rank_candidates_requires_model_when_enabled(self) -> None:
        """use_model=True without a model is a usage error."""
        from whale_tracker import scorer

        score = _score("0xAAA", roi=10.0, realized=5000.0, markets=20, trades=80)
        with pytest.raises(ValueError):
            scorer.rank_candidates([score], use_model=True)
