"""Tests for the held-out ranker evaluation (:mod:`whale_tracker.evaluation`).

These cover the metric primitives (rank-IC, quantile lift), the reproducibility
of the synthetic corpus, and the end-to-end held-out evaluation: split sizing,
determinism, and that the trained model shows real out-of-sample skill and is at
least competitive with the heuristic baseline.
"""

import math

import pytest

pd = pytest.importorskip("pandas")
np = pytest.importorskip("numpy")
pytest.importorskip("sklearn")

from whale_tracker.evaluation import (  # noqa: E402
    EvaluationReport,
    evaluate_holdout,
    make_synthetic_corpus,
    quantile_lift,
    rank_ic,
)


class TestRankIC:
    """Spearman rank information coefficient."""

    def test_perfectly_ordered_is_one(self) -> None:
        """Identical ordering gives +1."""
        ic = rank_ic(np.array([1.0, 2.0, 3.0, 4.0]), np.array([10.0, 20.0, 30.0, 40.0]))
        assert ic == pytest.approx(1.0)

    def test_reversed_is_minus_one(self) -> None:
        """Exactly reversed ordering gives -1."""
        ic = rank_ic(np.array([1.0, 2.0, 3.0, 4.0]), np.array([40.0, 30.0, 20.0, 10.0]))
        assert ic == pytest.approx(-1.0)

    def test_monotonic_nonlinear_still_one(self) -> None:
        """Rank-IC is scale-free: a monotone nonlinear map still scores +1."""
        x = np.array([1.0, 2.0, 3.0, 4.0, 5.0])
        assert rank_ic(x, x**3) == pytest.approx(1.0)

    def test_constant_input_is_nan(self) -> None:
        """Correlation with a constant vector is undefined → NaN, not 0."""
        assert math.isnan(rank_ic(np.array([5.0, 5.0, 5.0]), np.array([1.0, 2.0, 3.0])))

    def test_too_few_points_is_nan(self) -> None:
        """Fewer than two points → NaN."""
        assert math.isnan(rank_ic(np.array([1.0]), np.array([2.0])))


class TestQuantileLift:
    """Top-vs-bottom quantile lift."""

    def test_lift_positive_when_aligned(self) -> None:
        """When predictions track the label, top picks beat bottom picks."""
        pred = np.arange(10, dtype=float)
        actual = np.arange(10, dtype=float) * 100.0
        top, bottom, lift = quantile_lift(pred, actual, quantile=0.2)
        assert top > bottom
        assert lift == pytest.approx(top - bottom)

    def test_empty_is_nan(self) -> None:
        """An empty input yields NaNs rather than raising."""
        top, bottom, lift = quantile_lift(np.empty(0), np.empty(0))
        assert math.isnan(top)
        assert math.isnan(bottom)
        assert math.isnan(lift)


class TestSyntheticCorpus:
    """Reproducible labelled wallet population."""

    def test_reproducible_for_same_seed(self) -> None:
        """Same seed → byte-identical labels, addresses, and field values."""
        a = make_synthetic_corpus(50, seed=3)
        b = make_synthetic_corpus(50, seed=3)
        assert np.array_equal(a.labels, b.labels)
        assert [s.address for s in a.scores] == [s.address for s in b.scores]
        assert a.scores[0].realized_pnl == b.scores[0].realized_pnl

    def test_different_seed_differs(self) -> None:
        """A different seed produces a different corpus."""
        a = make_synthetic_corpus(50, seed=3)
        b = make_synthetic_corpus(50, seed=4)
        assert not np.array_equal(a.labels, b.labels)

    def test_shapes_and_round_trips(self) -> None:
        """Right counts, unique addresses, and real round-trips per wallet."""
        corpus = make_synthetic_corpus(30, seed=1)
        assert len(corpus.scores) == 30
        assert corpus.labels.shape == (30,)
        assert len(corpus.round_trips_by_address) == 30
        assert len({s.address for s in corpus.scores}) == 30
        assert all(len(corpus.round_trips_by_address[s.address]) > 0 for s in corpus.scores)


class TestEvaluateHoldout:
    """End-to-end held-out evaluation."""

    def test_split_sizes(self) -> None:
        """Train/test are disjoint and sum to the corpus size."""
        report = evaluate_holdout(n_wallets=200, test_size=0.25)
        assert report.n_train + report.n_test == 200
        assert report.n_test == 50

    def test_deterministic(self) -> None:
        """Same seed → identical report (frozen dataclass equality)."""
        a = evaluate_holdout(n_wallets=200, seed=11)
        b = evaluate_holdout(n_wallets=200, seed=11)
        assert a == b

    def test_model_generalises_and_is_competitive(self) -> None:
        """Trained model shows real OOS skill and is competitive with baseline.

        Conservative bounds (actual run: rank-IC ~0.54, baseline ~0.53) so the
        test is robust to minor scikit-learn version differences.
        """
        report = evaluate_holdout()
        assert isinstance(report, EvaluationReport)
        assert report.used_model is True
        assert -1.0 <= report.model_rank_ic <= 1.0
        assert report.model_rank_ic > 0.35
        # At least competitive with — here, slightly better than — the heuristic.
        assert report.model_rank_ic >= report.baseline_rank_ic - 0.08
        # Acting on the model's top picks beats its bottom picks out-of-sample.
        assert report.lift > 0.0
