"""Train the whale-performance ranker and print its out-of-sample metrics.

A runnable demonstration of :mod:`whale_tracker.evaluation`: it builds a seeded
synthetic wallet corpus, trains the model on a training split, and reports
held-out ranking metrics (Spearman rank-IC, out-of-sample R², top-vs-bottom
quantile lift) alongside a heuristic baseline.

Run it from the ``analysis`` directory::

    uv run --extra dev python examples/evaluate_ranker.py
    # or, with the package already installed in your environment:
    python examples/evaluate_ranker.py

The corpus is synthetic and fully seeded, so the printed figures are
reproducible and match ``metrics.md``.
"""

from __future__ import annotations

from whale_tracker.evaluation import evaluate_holdout


def main() -> None:
    """Run the held-out evaluation and print a readable report."""
    report = evaluate_holdout()
    print(report.summary())


if __name__ == "__main__":
    main()
