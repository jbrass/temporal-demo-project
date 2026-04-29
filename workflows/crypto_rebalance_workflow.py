"""
Crypto Portfolio Rebalancer Workflow — 24/7 Perpetual Loop

Key Temporal concepts demonstrated beyond the SPY/TLT version:
- Perpetual workflow with continue_as_new (avoids infinite history growth)
- Durable timers via workflow.sleep() (survives worker restarts mid-sleep)
- Signal-adjustable interval (change cadence without restarting workflow)
- Activity heartbeating (long-running order-fill waits are recoverable)
"""

import asyncio
from datetime import timedelta
from dataclasses import dataclass, field
from temporalio import workflow
from temporalio.common import RetryPolicy

with workflow.unsafe.imports_passed_through():
    from activities.crypto_activities import (
        get_crypto_positions,
        get_crypto_prices,
        calculate_crypto_rebalance,
        execute_crypto_orders,
        send_crypto_notification,
        CryptoSnapshot,
        CryptoRebalanceResult,
    )

# ---------------------------------------------------------------------------
# How many cycles before we continue_as_new to reset event history.
# Temporal's default history limit is 50k events; each cycle is ~10 events,
# so 500 cycles = ~5000 events — well within limits but reset periodically.
# ---------------------------------------------------------------------------
MAX_CYCLES_BEFORE_RESET = 200


@dataclass
class CryptoRebalanceConfig:
    target_btc_pct: float = 0.60
    target_eth_pct: float = 0.40
    drift_threshold: float = 0.05
    interval_hours: float = 2.0       # sleep between cycles
    dry_run: bool = False
    alpaca_base_url: str = "https://paper-api.alpaca.markets"


@dataclass
class CryptoWorkflowState:
    """Carried across continue_as_new boundaries."""
    cycle_count: int = 0
    total_rebalances: int = 0
    last_rebalance_time: str = ""
    last_snapshot: dict = field(default_factory=dict)


# ---------------------------------------------------------------------------
# Perpetual Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class CryptoRebalanceWorkflow:
    """
    Perpetual 24/7 crypto rebalancing workflow.

    Runs continuously:
      check → evaluate → (rebalance if needed) → sleep(interval) → repeat

    Uses continue_as_new every MAX_CYCLES_BEFORE_RESET cycles to keep
    event history bounded — this is the standard Temporal pattern for
    long-running workflows.

    Signals:
        set_interval      — change sleep duration without restarting
        force_rebalance   — trigger immediate rebalance, skip sleep
        pause / resume    — halt cycling without stopping the workflow
        update_config     — update thresholds

    Queries:
        get_status        — current cycle, drift, last rebalance info
    """

    def __init__(self):
        self._force_rebalance = False
        self._paused = False
        self._config_override: CryptoRebalanceConfig | None = None
        self._base_config: CryptoRebalanceConfig | None = None
        self._state = CryptoWorkflowState()
        self._status = "starting"

    # ------------------------------------------------------------------
    # Signals
    # ------------------------------------------------------------------

    @workflow.signal
    def set_interval(self, hours: float):
        """Adjust the sleep interval between cycles dynamically."""
        workflow.logger.info(f"Interval updated to {hours}h")
        if self._config_override is None:
            base = self._base_config or CryptoRebalanceConfig()
            self._config_override = CryptoRebalanceConfig(**base.__dict__)
        self._config_override.interval_hours = hours

    @workflow.signal
    def force_rebalance(self):
        """Skip the current sleep and trigger an immediate rebalance."""
        workflow.logger.info("force_rebalance signal received")
        self._force_rebalance = True

    @workflow.signal
    def pause(self):
        """Pause cycling — workflow stays alive but won't trade."""
        workflow.logger.info("Workflow paused")
        self._paused = True

    @workflow.signal
    def resume(self):
        """Resume cycling after a pause."""
        workflow.logger.info("Workflow resumed")
        self._paused = False

    @workflow.signal
    def update_config(self, config: CryptoRebalanceConfig):
        workflow.logger.info(f"Config updated: drift={config.drift_threshold} interval={config.interval_hours}h")
        self._config_override = config

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    @workflow.query
    def get_status(self) -> dict:
        return {
            "status": self._status,
            "paused": self._paused,
            "cycle_count": self._state.cycle_count,
            "total_rebalances": self._state.total_rebalances,
            "last_rebalance_time": self._state.last_rebalance_time,
            "last_snapshot": self._state.last_snapshot,
            "force_pending": self._force_rebalance,
        }

    # ------------------------------------------------------------------
    # Main loop
    # ------------------------------------------------------------------

    @workflow.run
    async def run(
        self,
        config: CryptoRebalanceConfig,
        state: CryptoWorkflowState | None = None,
    ) -> None:
        """
        Entry point. Loops indefinitely, calling continue_as_new
        every MAX_CYCLES_BEFORE_RESET cycles to reset event history.

        `state` is passed in from continue_as_new so we preserve
        cycle counts and last-run info across history resets.
        """
        # Temporal's JSON converter can fall back to dict when type resolution
        # fails (e.g. on continue_as_new boundaries or Python 3.14+ hint changes).
        if isinstance(config, dict):
            config = CryptoRebalanceConfig(**config)
        if isinstance(state, dict):
            state = CryptoWorkflowState(**state)

        # Restore carried-over state if this is a continued execution
        if state:
            self._state = state

        self._base_config = config
        effective_config = config

        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=3),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=5),
            maximum_attempts=5,
        )
        activity_opts = {
            "start_to_close_timeout": timedelta(seconds=45),
            "retry_policy": retry_policy,
        }

        # ----------------------------------------------------------------
        # Perpetual loop
        # ----------------------------------------------------------------
        while True:
            effective_config = self._config_override or config
            self._state.cycle_count += 1

            workflow.logger.info(
                f"Cycle {self._state.cycle_count} | "
                f"total rebalances: {self._state.total_rebalances} | "
                f"paused: {self._paused}"
            )

            # ── Pause check ─────────────────────────────────────────────
            if self._paused:
                self._status = "paused"
                # Wait until resumed (checks every 60s to avoid busy-loop)
                await workflow.wait_condition(
                    lambda: not self._paused,
                    timeout=timedelta(hours=24),
                )

            self._status = "checking"

            # ── Step 1: Fetch positions ──────────────────────────────────
            snapshot: CryptoSnapshot = await workflow.execute_activity(
                get_crypto_positions,
                args=[effective_config.alpaca_base_url],
                **activity_opts,
            )
            self._state.last_snapshot = snapshot.__dict__

            # ── Step 2: Live prices ──────────────────────────────────────
            prices: dict = await workflow.execute_activity(
                get_crypto_prices,
                args=[["BTC/USD", "ETH/USD"]],
                **activity_opts,
            )

            # ── Step 3: Calculate ────────────────────────────────────────
            rebalance: CryptoRebalanceResult = await workflow.execute_activity(
                calculate_crypto_rebalance,
                args=[
                    snapshot,
                    prices,
                    effective_config.target_btc_pct,
                    effective_config.target_eth_pct,
                    effective_config.drift_threshold,
                ],
                **activity_opts,
            )

            # ── Step 4: Execute if needed ────────────────────────────────
            needs_rebalance = rebalance.needs_rebalance or self._force_rebalance
            self._force_rebalance = False

            if needs_rebalance:
                self._status = "rebalancing"
                workflow.logger.info(
                    f"Rebalancing — BTC drift={rebalance.btc_drift:.2%} ETH drift={rebalance.eth_drift:.2%}"
                )

                placed_orders = []
                compensation_needed = False

                if not effective_config.dry_run:
                    for i, order in enumerate(rebalance.orders):
                        try:
                            result = await workflow.execute_activity(
                                execute_crypto_orders,
                                args=[[order], effective_config.alpaca_base_url],
                                **activity_opts,
                                heartbeat_timeout=timedelta(minutes=2),
                            )
                            placed_orders.extend(result)
                        except Exception as e:
                            workflow.logger.error(f"Order {i} failed: {e} — compensating")
                            compensation_needed = True
                            break

                    # Saga compensation if partial failure
                    if compensation_needed and placed_orders:
                        compensating = [
                            {**o, "side": "sell" if o["side"] == "buy" else "buy"}
                            for o in placed_orders
                        ]
                        try:
                            await workflow.execute_activity(
                                execute_crypto_orders,
                                args=[compensating, effective_config.alpaca_base_url],
                                **activity_opts,
                                heartbeat_timeout=timedelta(minutes=2),
                            )
                        except Exception as e:
                            workflow.logger.error(f"Compensation failed: {e} — MANUAL INTERVENTION REQUIRED")
                else:
                    placed_orders = [
                        {**o, "status": "simulated", "id": f"dry-{i}"}
                        for i, o in enumerate(rebalance.orders)
                    ]

                self._state.total_rebalances += 1
                self._state.last_rebalance_time = workflow.now().isoformat()

                await workflow.execute_activity(
                    send_crypto_notification,
                    args=[{
                        "cycle": self._state.cycle_count,
                        "status": "dry_run" if effective_config.dry_run else ("compensated" if compensation_needed else "completed"),
                        "orders": placed_orders,
                        "btc_drift": rebalance.btc_drift,
                        "eth_drift": rebalance.eth_drift,
                        "total_rebalances": self._state.total_rebalances,
                    }],
                    start_to_close_timeout=timedelta(seconds=10),
                )
            else:
                workflow.logger.info(
                    f"No rebalance needed — BTC drift={rebalance.btc_drift:.2%} ETH drift={rebalance.eth_drift:.2%}"
                )

            # ── Step 5: continue_as_new check ────────────────────────────
            # Reset event history every MAX_CYCLES_BEFORE_RESET cycles.
            # We pass current state forward so nothing is lost.
            if self._state.cycle_count >= MAX_CYCLES_BEFORE_RESET:
                workflow.logger.info(
                    f"Reached {MAX_CYCLES_BEFORE_RESET} cycles — continuing as new to reset history"
                )
                workflow.continue_as_new(
                    args=[effective_config, self._state],
                )

            # ── Step 6: Durable sleep until next cycle ───────────────────
            # workflow.sleep() is persisted in Temporal's event log.
            # If the worker crashes during this sleep, Temporal will
            # resume the timer correctly when the worker restarts —
            # unlike asyncio.sleep() which would be lost.
            self._status = "sleeping"
            sleep_duration = timedelta(hours=effective_config.interval_hours)
            workflow.logger.info(f"Sleeping for {effective_config.interval_hours}h until next cycle")

            # Use wait_condition with timeout so force_rebalance signal
            # can interrupt the sleep immediately
            try:
                await workflow.wait_condition(
                    lambda: self._force_rebalance,
                    timeout=sleep_duration,
                )
                workflow.logger.info("Sleep interrupted by force_rebalance signal")
            except asyncio.TimeoutError:
                pass
