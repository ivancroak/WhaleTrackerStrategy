"""Out-of-sample evaluation for the whale-performance ranker.

The unit tests in ``test_model.py`` prove the model *fits* a separable toy set
(in-sample correlation). That says nothing about **generalisation**. This module
adds the missing piece: a held-out evaluation that reports how well the trained
:class:`~whale_tracker.model.WhalePerformanceModel` ranks wallets it has never
seen, using metrics a quant actually cares about for a *ranking* signal.

What it does:

* :func:`make_synthetic_corpus` builds a reproducible population of wallets
  whose **forward** performance is a noisy function of a hidden ``skill`` latent.
  Each wallet emits real round-trips, so the engineered ``win_rate`` /
  ``sharpe_like`` / ``pnl_volatility`` columns are produced by the genuine
  :mod:`whale_tracker.analytics` pipeline rather than hand-set.
* :func:`evaluate_holdout` splits wallets into disjoint train/test sets, fits the
  model on the training wallets only (the scaler is fit on train inside the
  pipeline — no leakage), predicts the held-out wallets, and scores the ranking.

Reported metrics:

* **rank-IC** — the Spearman rank correlation between predicted score and
  realised forward performance on the held-out wallets. This is *the* metric for
  a ranking model: it measures whether we order wallets correctly, independent of
  the prediction's scale.
* **R²** — out-of-sample coefficient of determination (can be negative; reported
  honestly).
* **quantile lift** — mean realised forward performance of the wallets the model
  ranks in its top quintile minus that of its bottom quintile. The practical
  "if I copied the model's top picks, how much better did they actually do?".
* **baseline rank-IC** — the same rank-IC for the deterministic heuristic
  (:func:`whale_tracker.analytics.composite_score`) the model falls back to, so
  the trained model is measured against a non-trivial baseline rather than zero.

Honest framing: the corpus is **synthetic** — the repository ships no proprietary
historical wallet data, and these numbers are not a live-trading track record.
Everything is seeded, so the figures in ``metrics.md`` reproduce exactly. The
purpose is to demonstrate sound evaluation methodology and that the model learns
a signal that generalises out-of-sample.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import numpy as np
import pandas as pd
from sklearn.metrics import r2_score  # type: ignore[import-untyped]

from whale_tracker.analytics import (
    MODEL_FEATURE_COLUMNS,
    build_feature_frame,
    composite_score,
)
from whale_tracker.model import WhalePerformanceModel
from whale_tracker.scorer import RoundTripTrade, WalletScore

#: Fixed reference "now" so recency features (and therefore the whole report)
#: are deterministic. Matches the convention used by the model unit tests.
DEFAULT_NOW: datetime = datetime(2026, 3, 1, 12, 0, 0, tzinfo=UTC)

#: Feature columns used for the *forward-performance* evaluation. The default
#: :data:`~whale_tracker.analytics.MODEL_FEATURE_COLUMNS` omits ``realized_pnl``
#: because the live pipeline can use it as the training *label*. Here the label
#: is a wallet's **future** performance, so its **past** ``realized_pnl`` is a
#: legitimate predictor — it is known at ranking time and is not the target —
#: and is therefore included. (Excluding it is what made the gradient-boosting
#: model trail the heuristic baseline; including it is both correct and lets the
#: model use the same strong signal the baseline does.)
FORWARD_FEATURE_COLUMNS: tuple[str, ...] = (*MODEL_FEATURE_COLUMNS, "realized_pnl")


@dataclass(frozen=True)
class SyntheticCorpus:
    """A reproducible population of labelled wallets for evaluation.

    Attributes:
        scores: One :class:`~whale_tracker.scorer.WalletScore` per wallet.
        round_trips_by_address: Per-wallet round-trips, so the analytics layer
            can derive the trade-distribution features for real.
        labels: Forward-performance target per wallet, aligned to ``scores``.
        now: Reference time used for recency features.
    """

    scores: list[WalletScore]
    round_trips_by_address: dict[str, list[RoundTripTrade]]
    labels: np.ndarray
    now: datetime


@dataclass(frozen=True)
class EvaluationReport:
    """Held-out evaluation result for the ranker.

    Attributes:
        n_train: Number of training wallets.
        n_test: Number of held-out (test) wallets.
        model_rank_ic: Spearman rank-IC of the trained model on the test set.
        model_r2: Out-of-sample R² of the trained model.
        baseline_rank_ic: Spearman rank-IC of the heuristic fallback baseline.
        top_quantile_mean: Mean realised label of the model's top-quintile picks.
        bottom_quantile_mean: Mean realised label of the bottom-quintile picks.
        lift: ``top_quantile_mean - bottom_quantile_mean``.
        used_model: Whether a trained model (not the fallback) produced the
            predictions — ``True`` for a healthy run.
    """

    n_train: int
    n_test: int
    model_rank_ic: float
    model_r2: float
    baseline_rank_ic: float
    top_quantile_mean: float
    bottom_quantile_mean: float
    lift: float
    used_model: bool

    def summary(self) -> str:
        """Human-readable multi-line report for the example script / CLI."""
        edge = self.model_rank_ic - self.baseline_rank_ic
        return (
            "Whale-ranker out-of-sample evaluation (synthetic, seeded)\n"
            "----------------------------------------------------------\n"
            f"train wallets         : {self.n_train}\n"
            f"test wallets (holdout): {self.n_test}\n"
            f"trained model used    : {self.used_model}\n"
            "\n"
            f"model rank-IC (Spearman) : {self.model_rank_ic:+.3f}\n"
            f"baseline rank-IC (heur.) : {self.baseline_rank_ic:+.3f}\n"
            f"model edge over baseline : {edge:+.3f}\n"
            f"model R² (out-of-sample) : {self.model_r2:+.3f}\n"
            "\n"
            f"top-quintile mean label  : {self.top_quantile_mean:,.0f}\n"
            f"bottom-quintile mean     : {self.bottom_quantile_mean:,.0f}\n"
            f"lift (top - bottom)      : {self.lift:,.0f}\n"
        )


def rank_ic(predicted: np.ndarray | pd.Series, actual: np.ndarray | pd.Series) -> float:
    """Spearman rank information coefficient between two score vectors.

    The rank-IC is the Pearson correlation of the *ranks* of ``predicted`` and
    ``actual`` — i.e. how well the predicted ordering matches the realised
    ordering. ``+1`` is a perfect ranking, ``0`` is no skill, ``-1`` is exactly
    reversed. Ties are handled with average ranks (pandas' ``"spearman"``).

    Returns ``nan`` for fewer than two points or a constant input (undefined
    correlation), which keeps the metric honest rather than silently 0.
    """
    pred = pd.Series(np.asarray(predicted, dtype=float))
    act = pd.Series(np.asarray(actual, dtype=float))
    # Correlation is undefined for fewer than two points or a constant vector;
    # return NaN directly (rather than letting the rank correlation warn on it).
    if len(pred) < 2 or pred.nunique() < 2 or act.nunique() < 2:
        return float("nan")
    return float(pred.corr(act, method="spearman"))


def quantile_lift(
    predicted: np.ndarray,
    actual: np.ndarray,
    *,
    quantile: float = 0.2,
) -> tuple[float, float, float]:
    """Mean realised ``actual`` for the top vs bottom ``quantile`` of predictions.

    Sorts wallets by ``predicted`` and compares the average realised label of the
    best-ranked slice against the worst-ranked slice — the practical payoff of
    acting on the model's ordering.

    Returns ``(top_mean, bottom_mean, top_mean - bottom_mean)``.
    """
    pred = np.asarray(predicted, dtype=float)
    act = np.asarray(actual, dtype=float)
    if pred.size == 0:
        return (float("nan"), float("nan"), float("nan"))
    order = np.argsort(pred)  # ascending: worst first, best last
    k = max(1, int(round(pred.size * quantile)))
    bottom_mean = float(np.mean(act[order[:k]]))
    top_mean = float(np.mean(act[order[-k:]]))
    return (top_mean, bottom_mean, top_mean - bottom_mean)


def make_synthetic_corpus(
    n_wallets: int = 240,
    *,
    seed: int = 7,
    label_noise: float = 0.85,
    now: datetime = DEFAULT_NOW,
) -> SyntheticCorpus:
    """Build a reproducible, labelled wallet population for evaluation.

    Each wallet has a hidden ``skill`` latent drawn from a standard normal. Its
    observable features (round-trip returns, ROI, realised PnL, recency, …) are
    *noisy* reflections of that skill, and its **forward-performance label** is a
    separate noisy reflection of the same skill. A model therefore has to recover
    the latent skill from the features to predict the held-out label — there is a
    real, learnable signal but no trivial leakage.

    Args:
        n_wallets: Number of wallets to generate.
        seed: PRNG seed; identical seeds give byte-identical corpora.
        label_noise: Std of the label noise as a multiple of the skill signal
            (higher → harder problem, lower rank-IC ceiling).
        now: Reference time for recency features.

    Returns:
        A :class:`SyntheticCorpus`.
    """
    rng = np.random.default_rng(seed)
    skill = rng.normal(0.0, 1.0, size=n_wallets)

    scores: list[WalletScore] = []
    rt_map: dict[str, list[RoundTripTrade]] = {}
    labels = np.empty(n_wallets, dtype=float)

    for i in range(n_wallets):
        s = float(skill[i])
        address = f"0x{i:040x}"

        # Round-trips: per-trade return mean rises with skill; fixed dispersion.
        n_trips = int(rng.integers(12, 45))
        returns = rng.normal(0.02 + 0.05 * s, 0.18, size=n_trips)
        rt_map[address] = [
            RoundTripTrade(
                asset=f"{address[:8]}-{j}",
                buy_price=Decimal("0.50"),
                sell_price=Decimal(str(round(0.50 * (1.0 + float(r)), 6))),
                size=Decimal("100"),
                pct_return=Decimal(str(round(float(r), 6))),
            )
            for j, r in enumerate(returns)
        ]

        roi = float(np.mean(returns) * 100.0)
        realized = 4000.0 * s + float(rng.normal(0.0, 1500.0))
        total = realized * 1.4 + float(rng.normal(0.0, 800.0))
        last_days = float(rng.uniform(0.0, 18.0))

        scores.append(
            WalletScore(
                address=address,
                total_pnl=Decimal(str(round(total, 2))),
                avg_pct_return=Decimal(str(round(roi, 4))),
                markets_traded=n_trips,
                total_trades=int(n_trips * 2 + int(rng.integers(0, 20))),
                avg_daily_transactions_7d=Decimal(str(round(float(rng.uniform(1.0, 9.0)), 2))),
                last_trade_at=now - timedelta(days=last_days),
                round_trips=n_trips,
                realized_pnl=Decimal(str(round(realized, 2))),
                passes_filters=True,
            )
        )

        # Forward performance: same latent skill, independent noise.
        labels[i] = 10_000.0 * s + float(rng.normal(0.0, label_noise * 10_000.0))

    return SyntheticCorpus(
        scores=scores,
        round_trips_by_address=rt_map,
        labels=labels,
        now=now,
    )


def evaluate_holdout(
    *,
    n_wallets: int = 240,
    test_size: float = 0.3,
    seed: int = 7,
    model_random_state: int = 0,
    now: datetime = DEFAULT_NOW,
) -> EvaluationReport:
    """Train on a wallet subset, score the held-out wallets, and report metrics.

    Train and test wallets are **disjoint**; the model's ``StandardScaler`` is
    fit on the training rows only (inside the pipeline), so there is no
    train/test leakage. The heuristic baseline is scored on the same held-out
    rows for a like-for-like comparison.

    Args:
        n_wallets: Total wallets in the synthetic corpus.
        test_size: Fraction held out for evaluation.
        seed: Seed for both the corpus and the train/test split.
        model_random_state: Seed for the gradient-boosting estimator.
        now: Reference time for recency features.

    Returns:
        An :class:`EvaluationReport`.
    """
    corpus = make_synthetic_corpus(n_wallets, seed=seed, now=now)
    n = len(corpus.scores)

    # Deterministic disjoint split.
    perm = np.random.default_rng(seed + 1).permutation(n)
    n_test = max(1, int(round(n * test_size)))
    test_idx = perm[:n_test]
    train_idx = perm[n_test:]

    def _frame(indices: np.ndarray) -> pd.DataFrame:
        subset = [corpus.scores[i] for i in indices]
        return build_feature_frame(
            subset,
            round_trips_by_address=corpus.round_trips_by_address,
            now=now,
        )

    train_frame = _frame(train_idx)
    test_frame = _frame(test_idx)
    train_labels = corpus.labels[train_idx]
    test_labels = corpus.labels[test_idx]

    model = WhalePerformanceModel(
        feature_columns=FORWARD_FEATURE_COLUMNS,
        random_state=model_random_state,
    ).fit(train_frame, train_labels)
    predictions, used_model = model.predict_with_provenance(test_frame)

    baseline = composite_score(test_frame).to_numpy(dtype=float)
    top_mean, bottom_mean, lift = quantile_lift(predictions, test_labels)

    return EvaluationReport(
        n_train=int(train_idx.size),
        n_test=int(test_idx.size),
        model_rank_ic=rank_ic(predictions, test_labels),
        model_r2=float(r2_score(test_labels, predictions)),
        baseline_rank_ic=rank_ic(baseline, test_labels),
        top_quantile_mean=top_mean,
        bottom_quantile_mean=bottom_mean,
        lift=lift,
        used_model=used_model,
    )
