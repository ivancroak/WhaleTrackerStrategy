"""Pandas/NumPy feature engineering for scored whale wallets.

This module turns the analysis layer's existing ``WalletScore`` objects (and,
when available, their matched :class:`~whale_tracker.scorer.RoundTripTrade`
round-trips and raw :class:`~whale_tracker.data_api.ActivityEntry` activity)
into a tabular :class:`pandas.DataFrame` feature matrix — one row per wallet.

The engineered columns are intended as inputs to a downstream ranking model
(see :mod:`whale_tracker.model`) and as a human-inspectable summary of why a
wallet ranks where it does.

Design notes:

* All statistics are computed with NumPy (``np.mean``, ``np.std``, ``np.exp``)
  over the per-trade ``pct_return`` values of a wallet's round-trips. Columns
  that need round-trip detail (``win_rate``, ``pnl_volatility``, ``sharpe_like``)
  are ``NaN`` when no round-trips are supplied for that wallet, so callers can
  always tell "not derivable" apart from a real zero.
* ``avg_hold_hours`` is only derivable when raw activity is provided; it is the
  mean gap (in hours) between a wallet's consecutive trades. It is ``NaN``
  otherwise — this module never fabricates a placeholder hold time.
* Everything is pure, deterministic, fully typed, and performs no network I/O.

``Decimal`` money/ratio fields from the pydantic models are converted to
``float`` exactly once, here at the analytics boundary, because pandas/NumPy
operate on floats. The upstream scoring pipeline keeps using ``Decimal``.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import UTC, datetime
from decimal import Decimal
from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # pragma: no cover - import only for type checking
    from whale_tracker.data_api import ActivityEntry
    from whale_tracker.scorer import RoundTripTrade, WalletScore

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

#: Half-life (in days) used for the exponential recency decay weight. A wallet
#: that last traded ``RECENCY_HALF_LIFE_DAYS`` ago gets a recency weight of 0.5.
RECENCY_HALF_LIFE_DAYS: float = 7.0

#: Ordered feature columns produced by :func:`build_feature_frame`. The wallet
#: address is used as the DataFrame index, not a column.
FEATURE_COLUMNS: tuple[str, ...] = (
    "roi",
    "realized_pnl",
    "total_pnl",
    "win_rate",
    "trade_count",
    "round_trips",
    "markets_traded",
    "avg_hold_hours",
    "pnl_volatility",
    "sharpe_like",
    "recency_weight",
    "score",
)

#: Subset of :data:`FEATURE_COLUMNS` that the default model treats as inputs.
#: ``score`` and ``realized_pnl`` are excluded here because they are commonly
#: used as a heuristic fallback / training label respectively.
MODEL_FEATURE_COLUMNS: tuple[str, ...] = (
    "roi",
    "total_pnl",
    "win_rate",
    "trade_count",
    "round_trips",
    "markets_traded",
    "avg_hold_hours",
    "pnl_volatility",
    "sharpe_like",
    "recency_weight",
)


def _to_float(value: Decimal | float | int | None) -> float:
    """Convert a ``Decimal``/number to ``float``; ``None`` becomes ``NaN``."""
    if value is None:
        return float("nan")
    return float(value)


def _ensure_utc(moment: datetime) -> datetime:
    """Return a timezone-aware (UTC) copy of ``moment``."""
    if moment.tzinfo is None:
        return moment.replace(tzinfo=UTC)
    return moment


def _per_trade_returns(round_trips: Sequence[RoundTripTrade]) -> np.ndarray:
    """Extract per-round-trip percentage returns as a float NumPy array."""
    if not round_trips:
        return np.empty(0, dtype=float)
    return np.asarray([float(rt.pct_return) for rt in round_trips], dtype=float)


def _win_rate(returns: np.ndarray) -> float:
    """Fraction of round-trips with a strictly positive return.

    Returns ``NaN`` when there are no round-trips so that "unknown" is
    distinguishable from a genuine 0% win rate.
    """
    if returns.size == 0:
        return float("nan")
    return float(np.mean(returns > 0.0))


def _pnl_volatility(returns: np.ndarray) -> float:
    """Population standard deviation of per-trade returns via NumPy.

    Needs at least two round-trips to be meaningful; otherwise ``NaN``.
    """
    if returns.size < 2:
        return float("nan")
    return float(np.std(returns))


def _sharpe_like(returns: np.ndarray) -> float:
    """Mean-over-std of per-trade returns (a Sharpe-like ratio).

    This is a per-trade reward-to-variability ratio, NOT an annualised Sharpe
    ratio — it is deliberately named ``*_like``. Returns ``NaN`` when there are
    fewer than two trips, and ``0.0`` when the returns have zero dispersion
    (a degenerate but well-defined case).
    """
    if returns.size < 2:
        return float("nan")
    std = float(np.std(returns))
    if std == 0.0:
        return 0.0
    return float(np.mean(returns) / std)


def _avg_hold_hours(activities: Sequence[ActivityEntry]) -> float:
    """Mean hours between a wallet's consecutive trades.

    A pragmatic, honestly-labelled proxy for holding time: round-trips do not
    carry timestamps, so this uses the raw activity stream when supplied.
    Returns ``NaN`` when fewer than two activities are available.
    """
    if len(activities) < 2:
        return float("nan")
    stamps = sorted(_ensure_utc(a.timestamp) for a in activities)
    deltas = np.asarray(
        [(b - a).total_seconds() / 3600.0 for a, b in zip(stamps[:-1], stamps[1:], strict=False)],
        dtype=float,
    )
    if deltas.size == 0:
        return float("nan")
    return float(np.mean(deltas))


def _recency_weight(last_trade_at: datetime | None, *, now: datetime) -> float:
    """Exponential half-life decay weight on days since the last trade.

    ``weight = exp(-ln(2) * days_since_last_trade / RECENCY_HALF_LIFE_DAYS)``
    using NumPy, so the weight is exactly ``0.5`` at one half-life, ``0.25`` at
    two, and so on. A wallet with no recorded last trade gets ``0.0`` (fully
    decayed). Future-dated timestamps are clamped to 0 days (weight ``1.0``).
    """
    if last_trade_at is None:
        return 0.0
    days = (now - _ensure_utc(last_trade_at)).total_seconds() / 86_400.0
    days = max(days, 0.0)
    return float(np.exp(-np.log(2.0) * days / RECENCY_HALF_LIFE_DAYS))


def composite_score(frame: pd.DataFrame) -> pd.Series:
    """Deterministic heuristic blend used as the model's fallback ranking.

    Combines z-scored ROI, realized PnL, win rate, and the recency weight into
    a single number. This is the same idea the scorer's rank key encodes
    (favour high ROI / realized PnL / fresh wallets), expressed continuously so
    it can act as a graceful fallback when no trained model is available.

    NaN feature values are treated as the column mean (i.e. neutral) so a
    wallet is never penalised purely for missing round-trip detail.

    Args:
        frame: A feature frame as returned by :func:`build_feature_frame`.

    Returns:
        A float :class:`pandas.Series` aligned to ``frame.index`` — higher is
        better. An all-zero series is returned for an empty frame.
    """
    if frame.empty:
        return pd.Series(dtype=float, index=frame.index)

    weights: dict[str, float] = {
        "roi": 0.40,
        "realized_pnl": 0.30,
        "win_rate": 0.20,
        "recency_weight": 0.10,
    }

    score = pd.Series(0.0, index=frame.index, dtype=float)
    for column, weight in weights.items():
        col = frame[column].astype(float)
        mean = col.mean(skipna=True)
        filled = col.fillna(mean if pd.notna(mean) else 0.0)
        std = filled.std(ddof=0)
        if std and not np.isnan(std):
            z = (filled - filled.mean()) / std
        else:
            z = pd.Series(0.0, index=frame.index, dtype=float)
        score = score + weight * z
    return score


def build_feature_frame(
    scores: Sequence[WalletScore],
    *,
    round_trips_by_address: Mapping[str, Sequence[RoundTripTrade]] | None = None,
    activities_by_address: Mapping[str, Sequence[ActivityEntry]] | None = None,
    now: datetime | None = None,
) -> pd.DataFrame:
    """Build a one-row-per-wallet feature matrix from scored wallets.

    Args:
        scores: Scored wallets (``WalletScore``) to turn into feature rows.
        round_trips_by_address: Optional per-wallet matched round-trips. When
            provided, ``win_rate``, ``pnl_volatility`` and ``sharpe_like`` are
            computed from the real per-trade ``pct_return`` distribution via
            NumPy. When omitted for a wallet, those columns are ``NaN``.
        activities_by_address: Optional per-wallet raw activity, used solely to
            derive ``avg_hold_hours`` (mean hours between consecutive trades).
        now: Reference "current time" for the recency decay. Defaults to
            ``datetime.now(UTC)``. Pass an explicit value for deterministic
            tests.

    Returns:
        A :class:`pandas.DataFrame` indexed by wallet address whose columns are
        exactly :data:`FEATURE_COLUMNS`. The frame has zero rows (but the full
        column set) when ``scores`` is empty.
    """
    reference_now = _ensure_utc(now) if now is not None else datetime.now(tz=UTC)
    rt_map = round_trips_by_address or {}
    act_map = activities_by_address or {}

    records: list[dict[str, float]] = []
    index: list[str] = []

    for score in scores:
        round_trips = rt_map.get(score.address, ())
        activities = act_map.get(score.address, ())
        returns = _per_trade_returns(round_trips)

        row: dict[str, float] = {
            "roi": _to_float(score.avg_pct_return),
            "realized_pnl": _to_float(score.realized_pnl),
            "total_pnl": _to_float(score.total_pnl),
            "win_rate": _win_rate(returns),
            "trade_count": float(score.total_trades),
            "round_trips": float(score.round_trips),
            "markets_traded": float(score.markets_traded),
            "avg_hold_hours": _avg_hold_hours(activities),
            "pnl_volatility": _pnl_volatility(returns),
            "sharpe_like": _sharpe_like(returns),
            "recency_weight": _recency_weight(score.last_trade_at, now=reference_now),
        }
        records.append(row)
        index.append(score.address)

    frame = pd.DataFrame(records, index=pd.Index(index, name="address"))

    # Guarantee a stable, fully-populated column set even for an empty input.
    for column in FEATURE_COLUMNS:
        if column == "score":
            continue
        if column not in frame.columns:
            frame[column] = pd.Series(dtype=float)

    # The composite score depends on the other columns, so compute it last.
    frame["score"] = composite_score(frame)

    return frame.reindex(columns=list(FEATURE_COLUMNS))
