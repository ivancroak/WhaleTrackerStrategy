"""Main bot loop — orchestrates monitor → risk gate → executor.

Includes automatic whale discovery and 24h rescan cycle.

Usage:
    python3 -m whale_tracker.bot --portfolio 100
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:
    import tomli as tomllib  # type: ignore[no-redef]

from whale_tracker.data_api import PolymarketDataClient
from whale_tracker.executor import Executor
from whale_tracker.models import Position
from whale_tracker.monitor import ExecutionConfig, WalletList, WatchedWallet, WhaleMonitor
from whale_tracker.risk_gate import Approval, RiskConfig, RiskGate
from whale_tracker.scorer import (
    ScoringConfig,
    apply_auto_removal,
    discover_whales,
    score_wallet,
)
from whale_tracker.signal_source import SignalSource
from whale_tracker.whale_signal import WhaleSignalSource

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------

PROJECT_ROOT = Path(__file__).resolve().parents[4]  # WhaleTrackerStrategy/
CONFIG_PATH = PROJECT_ROOT / "config" / "engine.toml"
WALLETS_PATH = PROJECT_ROOT / "config" / "wallets.json"
TRADES_LOG = PROJECT_ROOT / "data" / "trades.jsonl"


# ---------------------------------------------------------------------------
# Bot
# ---------------------------------------------------------------------------


class WhaleBot:
    """Orchestrator: monitor → risk gate → executor loop with auto-discovery."""

    def __init__(
        self,
        config_path: Path = CONFIG_PATH,
        wallets_path: Path = WALLETS_PATH,
        portfolio_value: Decimal = Decimal("100"),
    ) -> None:
        self._config_path = config_path
        self._wallets_path = wallets_path
        self._portfolio_value = portfolio_value

        # Load config
        with open(config_path, "rb") as f:
            toml_data = tomllib.load(f)

        risk_section = toml_data.get("risk", {})
        execution_section = toml_data.get("execution", {})

        self._risk_config = RiskConfig(
            max_position_pct=Decimal(str(risk_section.get("max_position_pct", "0.10"))),
            max_open_positions=int(risk_section.get("max_open_positions", 10)),
            total_exposure_cap=Decimal(str(risk_section.get("total_exposure_cap", "0.90"))),
            daily_loss_stop_pct=Decimal(str(risk_section.get("daily_loss_stop_pct", "0.05"))),
            drawdown_breaker_pct=Decimal(str(risk_section.get("drawdown_breaker_pct", "0.12"))),
        )

        self._exec_config = ExecutionConfig(
            max_slippage_from_whale=Decimal(
                str(execution_section.get("max_slippage_from_whale", "0.03"))
            ),
            min_spread_from_resolution=Decimal(
                str(execution_section.get("min_spread_from_resolution", "0.05"))
            ),
        )

        # Scoring config
        self._scoring_config = ScoringConfig.from_toml(config_path)

        # Load wallets
        wallet_list = WalletList.load(wallets_path) if wallets_path.exists() else WalletList()
        self._wallets = wallet_list.wallets

        # Components
        self._client = PolymarketDataClient()
        self._risk_gate = RiskGate(self._risk_config, portfolio_value)
        self._executor = Executor(TRADES_LOG)
        self._monitor: WhaleMonitor | None = None
        self._signal_source: SignalSource | None = None
        self._last_rescan: datetime | None = None

        self._init_monitor()

    def _init_monitor(self) -> None:
        """Initialize (or re-initialize) the whale monitor and signal source."""
        self._monitor = WhaleMonitor(
            wallets=self._wallets,
            client=self._client,
            execution_config=self._exec_config,
            portfolio_value=self._portfolio_value,
            max_position_pct=self._risk_config.max_position_pct,
        )
        self._signal_source = WhaleSignalSource(self._monitor)

    async def run(self) -> None:
        """Main loop: auto-discover → poll → process → rescan → repeat."""
        self._print_banner()

        # Auto-discover if watchlist is empty
        if not self._wallets:
            print("[*] No wallets in watchlist — running automatic discovery...")
            await self._run_rescan()

        if not self._wallets:
            print(
                "[!] Discovery found no qualifying wallets. "
                "Check scoring parameters in engine.toml."
            )
            await self._client.close()
            return

        assert self._monitor is not None
        print(
            f"[*] Monitoring {len(self._wallets)} wallets, polling every "
            f"{self._monitor.poll_interval}s"
        )
        print(f"[*] Strategy: {self._signal_source.name()}")  # type: ignore[union-attr]
        print(f"[*] Rescan every {self._scoring_config.rescan_interval_hours}h")
        print("[*] Press Ctrl-C to stop\n")

        try:
            while True:
                # Check if rescan is due
                if self._is_rescan_due():
                    await self._run_rescan()

                await self._poll_and_process()
                await asyncio.sleep(self._monitor.poll_interval)
        except KeyboardInterrupt:
            print("\n[*] Shutting down...")
        finally:
            await self._client.close()
            print("[*] Bot stopped.")

    def _is_rescan_due(self) -> bool:
        """Check if enough time has elapsed for a rescan."""
        if self._last_rescan is None:
            return False  # First rescan is done in run() if needed
        elapsed = datetime.now(tz=UTC) - self._last_rescan
        return elapsed >= timedelta(hours=self._scoring_config.rescan_interval_hours)

    async def _run_rescan(self) -> None:
        """Run the full discovery + scoring + auto-removal cycle."""
        now = datetime.now(tz=UTC)
        print(f"\n{'=' * 60}")
        print(f"  RESCAN — {now.strftime('%Y-%m-%d %H:%M:%S UTC')}")
        print(f"{'=' * 60}")

        try:
            # Discover new whales
            new_wallets = await discover_whales(self._client, self._scoring_config)

            # Re-score existing wallets for auto-removal
            if self._wallets:
                scores = {}
                for w in self._wallets:
                    try:
                        score = await score_wallet(
                            w.address,
                            w.total_pnl or Decimal("0"),
                            self._client,
                            self._scoring_config,
                        )
                        scores[w.address] = score
                    except Exception as e:
                        print(f"[scorer] Error re-scoring {w.address[:12]}: {e}")

                kept, removed = apply_auto_removal(self._wallets, scores, self._scoring_config)

                if removed:
                    print(f"[scorer] Auto-removed {len(removed)} wallet(s):")
                    for w in removed:
                        print(
                            f"    - {w.label} ({w.address[:12]}...): avg return {w.avg_pct_return}%"
                        )

                self._wallets = kept
            else:
                self._wallets = []

            # Merge new wallets and refresh metadata for wallets that still qualify
            refreshed_by_addr = {wallet.address: wallet for wallet in new_wallets}
            merged_wallets: list[WatchedWallet] = []
            added = 0
            refreshed = 0

            for wallet in self._wallets:
                replacement = refreshed_by_addr.pop(wallet.address, None)
                if replacement is None:
                    merged_wallets.append(wallet)
                    continue

                replacement.added_at = wallet.added_at or replacement.added_at
                merged_wallets.append(replacement)
                refreshed += 1

            for wallet in refreshed_by_addr.values():
                merged_wallets.append(wallet)
                added += 1

            self._wallets = merged_wallets

            # Enforce max_tracked_wallets cap
            if len(self._wallets) > self._scoring_config.max_tracked_wallets:
                # Sort by the same priority the scorer uses, then keep top N.
                self._wallets.sort(
                    key=lambda w: (
                        w.avg_pct_return or Decimal("0"),
                        w.realized_pnl or Decimal("0"),
                        w.markets_traded or 0,
                        w.total_pnl or Decimal("0"),
                    ),
                    reverse=True,
                )
                self._wallets = self._wallets[: self._scoring_config.max_tracked_wallets]

            # Save wallets.json
            wallet_list = WalletList(wallets=self._wallets)
            self._wallets_path.parent.mkdir(parents=True, exist_ok=True)
            wallet_list.save(self._wallets_path)

            # Refresh monitor with updated wallet list
            self._init_monitor()
            self._last_rescan = now

            print(
                f"[scorer] Watchlist: {len(self._wallets)} wallets "
                f"(+{added} new, {refreshed} refreshed)"
            )
            print(f"{'=' * 60}\n")

        except Exception as e:
            print(f"[!] Rescan error: {e}")
            self._last_rescan = now  # Don't retry immediately on error

    async def _poll_and_process(self) -> None:
        """Single poll iteration: poll signal source → validate → execute."""
        if self._signal_source is None:
            return

        try:
            signals = await self._signal_source.poll()
        except Exception as e:
            print(f"[!] Poll error: {e}")
            return

        if not signals:
            ts = datetime.now(tz=UTC).strftime("%H:%M:%S")
            print(f"[{ts}] No new signals ({self._signal_source.name()})")
            return

        print(f"[*] Generated {len(signals)} signal(s) ({self._signal_source.name()})")

        for signal in signals:
            print(
                f"\n--- Signal: {signal.confidence_tier} confidence, "
                f"{signal.wallets_agreeing} whale(s) ---"
            )
            print(f"    Market: {signal.market_id}")
            print(f"    Side: {signal.side}, Size: ${signal.recommended_size}")

            # Warn on small sizes
            if signal.recommended_size < Decimal("1"):
                print(
                    f"    [!] WARNING: Size ${signal.recommended_size} "
                    "may be below Polymarket minimum"
                )

            # Risk gate
            result = self._risk_gate.validate(signal)

            if isinstance(result, Approval):
                print(f"    [OK] Risk approved: ${result.approved_size}")

                # Fetch current price for slippage check
                current_price = await self._client.get_current_price(signal.token_id)

                # Execute
                order_result = await self._executor.execute(signal, result, current_price)

                if order_result.success:
                    print(f"    [OK] LIVE: {order_result.reason} (order: {order_result.order_id})")
                    # Record position in risk gate
                    position = Position(
                        market_id=signal.market_id,
                        token_id=signal.token_id,
                        side=signal.side,
                        entry_price=Decimal(order_result.price)
                        if order_result.price != "0"
                        else Decimal("0.50"),
                        size=Decimal(order_result.size),
                        category=signal.basket_category,
                        opened_at=order_result.timestamp,
                        source_signal=signal.signal_id,
                    )
                    self._risk_gate.record_trade(position)
                else:
                    print(f"    [X] Execution failed: {order_result.reason}")
            else:
                print(f"    [X] Risk denied: {result.message}")

    def _print_banner(self) -> None:
        """Print startup banner."""
        print()
        print("=" * 60)
        print("  WHALE TRACKER — Polymarket Copy Trading Bot (LIVE)")
        print("=" * 60)
        print(f"  Portfolio:  ${self._portfolio_value}")
        print(f"  Wallets:    {len(self._wallets)}")
        print(f"  Config:     {self._config_path}")
        print(
            f"  Max/trade:  {self._risk_config.max_position_pct * 100}%"
            f" (${self._portfolio_value * self._risk_config.max_position_pct})"
        )
        print(f"  Max pos:    {self._risk_config.max_open_positions}")
        print(f"  Exposure:   {self._risk_config.total_exposure_cap * 100}%")
        print(
            f"  Scoring:    {self._scoring_config.category_focus} focus, "
            f"rescan every {self._scoring_config.rescan_interval_hours}h"
        )
        print(f"  Log:        {TRADES_LOG}")
        print("=" * 60)


# ---------------------------------------------------------------------------
# CLI entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """CLI entry point for the bot."""
    parser = argparse.ArgumentParser(
        description="Whale Tracker — Polymarket live copy trading bot",
    )
    parser.add_argument(
        "--portfolio",
        type=float,
        default=100.0,
        help="Portfolio value in USDC (default: 100)",
    )
    parser.add_argument(
        "--config",
        type=str,
        default=str(CONFIG_PATH),
        help="Path to engine.toml config file",
    )
    parser.add_argument(
        "--wallets",
        type=str,
        default=str(WALLETS_PATH),
        help="Path to wallets.json watchlist",
    )

    args = parser.parse_args()
    portfolio = Decimal(str(args.portfolio))

    # Load .env for credentials
    try:
        from dotenv import load_dotenv

        load_dotenv()
    except ImportError:
        pass

    bot = WhaleBot(
        config_path=Path(args.config),
        wallets_path=Path(args.wallets),
        portfolio_value=portfolio,
    )

    asyncio.run(bot.run())


if __name__ == "__main__":
    main()
