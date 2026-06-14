# Whale-ranker evaluation

Out-of-sample evaluation of the wallet-ranking model
(`whale_tracker.model.WhalePerformanceModel`). The unit tests prove the model
*fits*; this measures whether it **generalises** — i.e. ranks wallets it has
never seen in the right order.

> **Honest framing.** The corpus below is **synthetic** — the repository ships no
> proprietary historical wallet data, and these figures are **not** a live-trading
> track record. Everything is seeded, so the numbers reproduce exactly. The point
> is to demonstrate sound evaluation methodology and that the model learns a
> signal that holds up out-of-sample.

## Pipeline

`StandardScaler → GradientBoostingRegressor` (scikit-learn), trained at runtime
on the engineered feature frame from `whale_tracker.analytics`. The scaler is fit
on the training rows only (inside the pipeline), so there is no train/test
leakage.

**Features (forward-prediction set).** The default `MODEL_FEATURE_COLUMNS` omits
`realized_pnl` because the live pipeline can use it as the training *label*. In
this evaluation the label is a wallet's **future** performance, so its **past**
`realized_pnl` is a legitimate predictor (known at ranking time, not the target)
and is included — see `FORWARD_FEATURE_COLUMNS`. Excluding it makes the model
trail the heuristic baseline; including it is both correct and lets the model use
the same strong signal the baseline does.

## Dataset

- **240** synthetic wallets, seeded (`seed=7`); regenerate via
  `whale_tracker.evaluation.make_synthetic_corpus`.
- Each wallet has a hidden `skill` latent. Its observable features (round-trip
  returns → `win_rate` / `sharpe_like` / `pnl_volatility`, ROI, realised PnL,
  recency, …) are **noisy** reflections of that skill; its **forward-performance
  label** is a separate noisy reflection of the same skill. The model must
  recover the latent skill from the features to predict the held-out label —
  real signal, no trivial leakage.
- Split: **70 / 30** disjoint → **168** train, **72** test.

## Results (held-out, 72 wallets)

| Metric | Value |
| --- | --- |
| Model rank-IC (Spearman) | **+0.544** |
| Heuristic baseline rank-IC | +0.530 |
| Model edge over baseline | **+0.014** |
| Model R² (out-of-sample) | +0.272 |
| Top-quintile mean label | +10,689 |
| Bottom-quintile mean label | −12,634 |
| Lift (top − bottom) | **23,323** |

**Reading it.** *rank-IC* is the Spearman rank correlation between predicted and
realised forward performance — the right metric for a ranking model (`+1` =
perfect order, `0` = no skill). The trained model reaches **+0.544** on wallets it
never saw and **edges out** a non-trivial heuristic baseline. The *quantile lift*
is the practical payoff: the wallets the model ranks in its top 20 % realised, on
average, ~23.3k more than its bottom 20 %.

## Reproduce

```bash
# from the analysis/ directory
uv run --extra dev python examples/evaluate_ranker.py
```

No figure is hard-coded anywhere: the script trains the model and computes every
number above from the seeded corpus. Minor scikit-learn point releases may shift
the third decimal; the relationships (model ≈ baseline, positive lift) hold.
