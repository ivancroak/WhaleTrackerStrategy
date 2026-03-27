#!/usr/bin/env python3
"""Preview the whale scoring pipeline without updating the watchlist.

The preview mirrors the bot's wallet-selection flow:

1. Merge sports candidates from multiple leaderboard slices.
2. Score each wallet using resolved sports positions first.
3. Show pass/fail status, rank order, and rejection reasons.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from decimal import Decimal
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT / "analysis" / "src"))

from whale_tracker.data_api import PolymarketDataClient  # noqa: E402
from whale_tracker.scorer import (  # noqa: E402
    ScoringConfig,
    WalletScore,
    discover_candidates,
    score_wallet,
)

CONFIG_PATH = PROJECT_ROOT / "config" / "engine.toml"

GREEN = "\033[92m"
RED = "\033[91m"
BOLD = "\033[1m"
DIM = "\033[2m"
RESET = "\033[0m"


async def run_preview(scan_size: int | None, verbose: bool) -> None:
    """Run the full scoring preview and print the results table."""
    del verbose  # Reserved for future expanded trade-level output.

    config = ScoringConfig.from_toml(CONFIG_PATH)
    if scan_size is not None:
        config = ScoringConfig(
            category_focus=config.category_focus,
            min_markets_traded=config.min_markets_traded,
            min_total_pnl=config.min_total_pnl,
            min_avg_pct_return=config.min_avg_pct_return,
            max_avg_daily_transactions_7d=config.max_avg_daily_transactions_7d,
            activity_recency_days=config.activity_recency_days,
            max_tracked_wallets=config.max_tracked_wallets,
            rescan_interval_hours=config.rescan_interval_hours,
            removal_threshold_days=config.removal_threshold_days,
            leaderboard_scan_size=scan_size,
        )

    print()
    print(f"{BOLD}{'=' * 110}{RESET}")
    print(f"{BOLD}  WHALE SCORING PREVIEW — Dry Run (no changes to wallets.json){RESET}")
    print(f"{BOLD}{'=' * 110}{RESET}")
    print()
    print(f"  Config:          {CONFIG_PATH}")
    print(f"  Category:        {config.category_focus}")
    print(f"  Discovery:       leaderboard top {config.leaderboard_scan_size} (ALL + MONTH PNL)")
    print(f"  Seed PNL:        ${config.min_total_pnl:,}")
    print(f"  Min markets:     {config.min_markets_traded} resolved markets")
    print(f"  Min ROI:         {config.min_avg_pct_return}% weighted return")
    print(f"  Recency:         {config.activity_recency_days} days")
    print(f"  Max tracked:     {config.max_tracked_wallets}")
    print()

    async with PolymarketDataClient() as client:
        print("[1/3] Discovering merged leaderboard candidates...", flush=True)
        candidates = await discover_candidates(client, config)
        if not candidates:
            print(f"{RED}  No candidates passed the seed-PNL filter.{RESET}")
            return
        print(f"  Scoring queue:    {len(candidates)} candidate wallets")
        print()

        print(f"[2/3] Scoring {len(candidates)} candidates...", flush=True)
        market_cache: dict[str, object] = {}
        results: list[tuple[str, str, Decimal, WalletScore]] = []

        for index, candidate in enumerate(candidates, 1):
            name = candidate.display_name or candidate.address[:16]
            pct = index / len(candidates) * 100
            sources = ",".join(candidate.discovery_sources)
            print(
                f"  [{index}/{len(candidates)}] {pct:5.1f}% — {name} "
                f"(sources={sources})...",
                end="",
                flush=True,
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
                status = f"{GREEN}PASS{RESET}" if score.passes_filters else f"{RED}FAIL{RESET}"
                print(f" {status}")
                results.append((name, candidate.address, candidate.leaderboard_pnl, score))
            except Exception as exc:
                print(f" {RED}ERROR: {exc}{RESET}")

        passing = [row for row in results if row[3].passes_filters]
        failing = [row for row in results if not row[3].passes_filters]
        passing.sort(key=lambda row: (row[3].avg_pct_return, row[3].realized_pnl), reverse=True)
        failing.sort(key=lambda row: row[3].avg_pct_return, reverse=True)

        print()
        print(f"{BOLD}{'=' * 110}{RESET}")
        print(
            f"{BOLD}[3/3] RESULTS — {len(passing)} PASS / {len(failing)} FAIL "
            f"(of {len(results)} scored){RESET}"
        )
        print(f"{BOLD}{'=' * 110}{RESET}")
        print()

        header = (
            f"{'#':>3}  {'Status':<6}  {'Name':<20}  {'SeedPNL':>10}  {'ROI%':>7}  "
            f"{'RealPNL':>10}  {'Mkts':>4}  {'Closed':>6}  {'DailyTx':>7}  "
            f"{'Basis':<13}  {'Sources':<22}  {'Rejection Reasons'}"
        )
        print(f"{BOLD}{header}{RESET}")
        print("-" * 170)

        all_sorted = passing + failing
        for index, (name, _addr, _seed_pnl, score) in enumerate(all_sorted, 1):
            status = f"{GREEN}PASS{RESET}" if score.passes_filters else f"{RED}FAIL{RESET}"
            reasons = ", ".join(score.rejection_reasons) if score.rejection_reasons else "-"
            sources = ",".join(score.discovery_sources)[:22]
            row = (
                f"{index:3d}  {status:>15}  {name[:20]:<20}  "
                f"${score.total_pnl:>9,.0f}  "
                f"{score.avg_pct_return:>6.1f}%  "
                f"${score.realized_pnl:>9,.0f}  "
                f"{score.markets_traded:>4d}  "
                f"{score.round_trips:>6d}  "
                f"{score.avg_daily_transactions_7d:>6.1f}  "
                f"{score.scoring_basis:<13}  "
                f"{sources:<22}  "
                f"{reasons[:55]}{'...' if len(reasons) > 55 else ''}"
            )
            print(row)

        print()
        print(f"{BOLD}{'=' * 110}{RESET}")
        print(f"{BOLD}  SUMMARY{RESET}")
        print(f"{'=' * 110}")
        print(f"  Candidates:       {len(candidates)}")
        print(f"  Scored:           {len(results)}")
        print(f"  {GREEN}PASSED:           {len(passing)}{RESET}")
        print(f"  {RED}FAILED:           {len(failing)}{RESET}")

        if passing:
            top_name, _top_addr, _top_seed, top_score = passing[0]
            avg_roi = sum(score.avg_pct_return for _, _, _, score in passing) / Decimal(len(passing))
            print(
                f"\n  Top whale:        {top_name} "
                f"(ROI {top_score.avg_pct_return:.1f}%, realized ${top_score.realized_pnl:,.0f})"
            )
            print(f"  Avg ROI (pass):   {avg_roi:>7.1f}%")

        if failing:
            reason_counts: dict[str, int] = {}
            for _, _, _, score in failing:
                for reason in score.rejection_reasons:
                    key = reason.partition(":")[0]
                    reason_counts[key] = reason_counts.get(key, 0) + 1

            print(f"\n  {BOLD}Rejection breakdown:{RESET}")
            for reason, count in sorted(reason_counts.items(), key=lambda item: -item[1]):
                print(f"    {reason:<25} {count:>3} wallets")

        if passing:
            print(f"\n  {BOLD}Passing wallet addresses:{RESET}")
            for _, address, _seed_pnl, _score in passing[: config.max_tracked_wallets]:
                print(f"    {address}")

        print(f"\n{'=' * 110}")
        print(f"  {DIM}This was a dry run. No changes were made to wallets.json.{RESET}")
        print(f"  {DIM}To activate: python3 -m whale_tracker.bot --portfolio <VALUE>{RESET}")
        print(f"{'=' * 110}")
        print()


def main() -> None:
    """CLI entry point."""
    parser = argparse.ArgumentParser(description="Preview whale scoring pipeline (dry run)")
    parser.add_argument(
        "--top",
        type=int,
        default=None,
        help="Override leaderboard scan size (default: from config)",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="Reserved for future trade-level preview output",
    )
    args = parser.parse_args()
    asyncio.run(run_preview(args.top, args.verbose))


if __name__ == "__main__":
    main()
