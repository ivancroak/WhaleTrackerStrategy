"""A runtime-trained whale performance ranker with a heuristic fallback.

:class:`WhalePerformanceModel` wraps a scikit-learn regression pipeline
(``StandardScaler`` → ``GradientBoostingRegressor``) that learns to predict a
wallet's realized PnL (or any continuous performance label you provide) from
the engineered feature columns built by :mod:`whale_tracker.analytics`.

Honest framing — please read before relying on this:

* This is **not** a pre-trained, validated model shipped with the repo. It is a
  small estimator that you train *at runtime* on whatever labelled wallet data
  you have on hand. Its quality is entirely a function of that data, and this
  module deliberately publishes **no accuracy numbers**.
* When there is too little data to train on (fewer than :data:`MIN_SAMPLES`
  rows) or :meth:`predict` is called before :meth:`fit`, the model does **not**
  raise. It falls back to a deterministic heuristic — the composite z-scored
  blend from :func:`whale_tracker.analytics.composite_score` — so the ranking
  API always returns something sensible. :attr:`is_fitted` and the
  ``used_model`` flag from :meth:`predict_with_provenance` let callers tell the
  two paths apart.

The model never performs network I/O and is deterministic given a fixed
``random_state``.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import pandas as pd
from sklearn.ensemble import GradientBoostingRegressor  # type: ignore[import-untyped]
from sklearn.pipeline import Pipeline  # type: ignore[import-untyped]
from sklearn.preprocessing import StandardScaler  # type: ignore[import-untyped]

from whale_tracker.analytics import (
    MODEL_FEATURE_COLUMNS,
    composite_score,
)

#: Minimum number of labelled rows required before :meth:`fit` will actually
#: train the regressor. Below this the model stays in heuristic-fallback mode.
MIN_SAMPLES: int = 8


@dataclass(frozen=True)
class RankedWallet:
    """One entry in a model ranking.

    Attributes:
        address: Wallet address (the feature-frame index value).
        predicted: The model's (or fallback heuristic's) score for the wallet.
        used_model: ``True`` if a trained model produced ``predicted``,
            ``False`` if it came from the heuristic fallback.
    """

    address: str
    predicted: float
    used_model: bool


class WhalePerformanceModel:
    """Train-at-runtime regressor for ranking whale wallets.

    Args:
        feature_columns: Which feature-frame columns to feed the model.
            Defaults to :data:`whale_tracker.analytics.MODEL_FEATURE_COLUMNS`.
        random_state: Seed for the gradient-boosting estimator so results are
            reproducible.
        min_samples: Override for :data:`MIN_SAMPLES` (the training-data floor
            below which the heuristic fallback is used).
    """

    def __init__(
        self,
        feature_columns: Sequence[str] | None = None,
        *,
        random_state: int = 0,
        min_samples: int = MIN_SAMPLES,
    ) -> None:
        self.feature_columns: tuple[str, ...] = tuple(
            feature_columns if feature_columns is not None else MODEL_FEATURE_COLUMNS
        )
        self.random_state = random_state
        self.min_samples = min_samples
        self._pipeline: Pipeline | None = None

    # -- State ---------------------------------------------------------------

    @property
    def is_fitted(self) -> bool:
        """Whether a real model has been trained (vs. heuristic fallback)."""
        return self._pipeline is not None

    # -- Feature extraction --------------------------------------------------

    def _matrix(self, features: pd.DataFrame) -> np.ndarray:
        """Select model columns and return a finite float matrix.

        Missing columns are created as all-``NaN``; every NaN is then replaced
        by that column's mean (or ``0.0`` for an all-NaN column) so the scaler
        and regressor never see non-finite values.
        """
        frame = features.reindex(columns=list(self.feature_columns)).astype(float)
        # Column-wise mean imputation, NaN-safe even for all-NaN columns.
        means = frame.mean(axis=0, skipna=True)
        frame = frame.fillna(means).fillna(0.0)
        return frame.to_numpy(dtype=float)

    # -- Training ------------------------------------------------------------

    def fit(
        self,
        features: pd.DataFrame,
        labels: Sequence[float] | pd.Series | np.ndarray,
    ) -> WhalePerformanceModel:
        """Train the regressor, or stay in fallback mode if data is too thin.

        Args:
            features: Feature frame (rows = wallets) from
                :func:`whale_tracker.analytics.build_feature_frame`.
            labels: The continuous target per wallet (e.g. realized/forward
                PnL or a normalized performance score), aligned row-wise to
                ``features``.

        Returns:
            ``self`` (for chaining).

        Raises:
            ValueError: If ``features`` and ``labels`` lengths disagree.
        """
        y = np.asarray(labels, dtype=float)
        if y.shape[0] != len(features):
            raise ValueError(f"labels length {y.shape[0]} != features rows {len(features)}")

        if len(features) < self.min_samples:
            # Not enough data to train responsibly — remain in fallback mode.
            self._pipeline = None
            return self

        pipeline = Pipeline(
            steps=[
                ("scaler", StandardScaler()),
                (
                    "regressor",
                    GradientBoostingRegressor(random_state=self.random_state),
                ),
            ]
        )
        pipeline.fit(self._matrix(features), y)
        self._pipeline = pipeline
        return self

    # -- Inference -----------------------------------------------------------

    def _fallback(self, features: pd.DataFrame) -> np.ndarray:
        """Deterministic heuristic scores when no model is available."""
        if features.empty:
            return np.empty(0, dtype=float)
        return composite_score(features).to_numpy(dtype=float)

    def predict(self, features: pd.DataFrame) -> np.ndarray:
        """Predict a performance score per wallet.

        Uses the trained pipeline when available; otherwise falls back to the
        deterministic composite heuristic. Never raises on an unfit model.

        Args:
            features: Feature frame (rows = wallets).

        Returns:
            A finite float ``np.ndarray`` of length ``len(features)``.
        """
        if features.empty:
            return np.empty(0, dtype=float)
        if self._pipeline is None:
            return self._fallback(features)
        preds = self._pipeline.predict(self._matrix(features))
        return np.asarray(preds, dtype=float)

    def predict_with_provenance(self, features: pd.DataFrame) -> tuple[np.ndarray, bool]:
        """Like :meth:`predict` but also report whether the model was used.

        Returns:
            ``(predictions, used_model)`` where ``used_model`` is ``True`` when
            the trained pipeline produced the values and ``False`` when the
            heuristic fallback did.
        """
        used_model = self._pipeline is not None and not features.empty
        return self.predict(features), used_model

    # -- Ranking convenience -------------------------------------------------

    def rank(self, features: pd.DataFrame) -> list[RankedWallet]:
        """Rank wallets best-first by predicted performance.

        Args:
            features: Feature frame indexed by wallet address.

        Returns:
            A list of :class:`RankedWallet`, highest predicted score first.
            Ties preserve the input order (stable sort).
        """
        predictions, used_model = self.predict_with_provenance(features)
        ranked = [
            RankedWallet(address=str(address), predicted=float(pred), used_model=used_model)
            for address, pred in zip(features.index, predictions, strict=False)
        ]
        ranked.sort(key=lambda item: item.predicted, reverse=True)
        return ranked

    def score(self, features: pd.DataFrame) -> pd.Series:
        """Predicted scores as a :class:`pandas.Series` aligned to the index.

        Convenient for joining model output back onto a feature frame.
        """
        return pd.Series(self.predict(features), index=features.index, name="model_score")

    # -- Persistence (optional) ---------------------------------------------

    def save(self, path: str) -> None:
        """Persist the fitted pipeline to ``path`` via joblib.

        Raises:
            RuntimeError: If the model has not been fitted (nothing to save).
        """
        if self._pipeline is None:
            raise RuntimeError("cannot save an unfit model (still in fallback mode)")
        import joblib  # type: ignore[import-untyped]

        joblib.dump(
            {
                "pipeline": self._pipeline,
                "feature_columns": self.feature_columns,
                "random_state": self.random_state,
                "min_samples": self.min_samples,
            },
            path,
        )

    @classmethod
    def load(cls, path: str) -> WhalePerformanceModel:
        """Load a model previously written by :meth:`save`."""
        import joblib  # type: ignore[import-untyped]

        blob = joblib.load(path)
        model = cls(
            feature_columns=blob["feature_columns"],
            random_state=blob["random_state"],
            min_samples=blob["min_samples"],
        )
        model._pipeline = blob["pipeline"]
        return model
