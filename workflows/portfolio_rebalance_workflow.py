"""
Portfolio Rebalancer Workflow - Temporal.io Implementation

This workflow implements a classic 60/40 portfolio rebalancer using SPY (equities)
and TLT (bonds). It runs on a schedule and rebalances when drift exceeds threshold.

Key Temporal concepts demonstrated:
- Durable workflow execution (survives worker restarts)
- Activity retries with backoff
- Signal handling (manual trigger)
- Scheduled cron-style execution
- Saga pattern for compensating transactions
"""

from datetime import timedelta
from temporalio import workflow
from temporalio.common import RetryPolicy
from dataclasses import dataclass

# Import activity stubs (resolved at runtime by Temporal)
with workflow.unsafe.imports_passed_through():
    from activities.portfolio_activities import (
        get_portfolio_positions,
        get_market_prices,
        calculate_rebalance_orders,
        execute_orders,
        send_notification,
        PortfolioSnapshot,
        RebalanceResult,
    )


# ---------------------------------------------------------------------------
# Workflow input/output types
# ---------------------------------------------------------------------------

@dataclass
class RebalanceConfig:
    target_equity_pct: float = 0.60   # SPY target weight
    target_bond_pct: float = 0.40     # TLT target weight
    drift_threshold: float = 0.05     # rebalance if any weight drifts > 5%
    dry_run: bool = False              # if True, calculate but don't execute
    alpaca_base_url: str = "https://paper-api.alpaca.markets"


@dataclass
class RebalanceWorkflowResult:
    status: str
    snapshot_before: dict
    orders_placed: list
    snapshot_after: dict
    message: str


# ---------------------------------------------------------------------------
# The Workflow
# ---------------------------------------------------------------------------

@workflow.defn
class PortfolioRebalanceWorkflow:
    """
    Durable portfolio rebalancing workflow.

    This workflow:
    1. Fetches current portfolio positions from Alpaca
    2. Gets live market prices for SPY and TLT
    3. Calculates whether drift exceeds the threshold
    4. If rebalancing needed, executes orders using a saga pattern
       (so we can compensate/roll back if a partial failure occurs)
    5. Sends a notification with the result

    Temporal ensures this entire flow is durable — if the worker crashes
    mid-execution, Temporal replays the workflow from its event history,
    skipping already-completed activities. No manual checkpointing needed.

    Signals:
        force_rebalance: Trigger an immediate rebalance regardless of drift
        update_config:   Update thresholds without stopping the workflow

    Queries:
        get_status: Returns the current workflow status and last run info
    """

    def __init__(self):
        self._force_rebalance = False
        self._config_override: RebalanceConfig | None = None
        self._last_result: RebalanceWorkflowResult | None = None
        self._status = "idle"

    # ------------------------------------------------------------------
    # Signals — external systems can send these to influence the workflow
    # ------------------------------------------------------------------

    @workflow.signal
    def force_rebalance(self):
        """Signal to trigger an immediate rebalance, bypassing drift check."""
        workflow.logger.info("Received force_rebalance signal")
        self._force_rebalance = True

    @workflow.signal
    def update_config(self, new_config: RebalanceConfig):
        """Signal to update drift threshold or targets without restarting."""
        workflow.logger.info(f"Received config update: drift_threshold={new_config.drift_threshold}")
        self._config_override = new_config

    # ------------------------------------------------------------------
    # Queries — read-only inspection of workflow state
    # ------------------------------------------------------------------

    @workflow.query
    def get_status(self) -> dict:
        """Return current status for dashboards / monitoring."""
        return {
            "status": self._status,
            "last_result": self._last_result.__dict__ if self._last_result else None,
            "force_pending": self._force_rebalance,
        }

    # ------------------------------------------------------------------
    # Main workflow logic
    # ------------------------------------------------------------------

    @workflow.run
    async def run(self, config: RebalanceConfig) -> RebalanceWorkflowResult:
        """
        Main entry point. Designed to be called on a schedule (cron trigger
        from the Temporal client) or via signal for immediate execution.
        """
        # Allow signal-based config override
        effective_config = self._config_override or config

        workflow.logger.info(
            f"Starting portfolio rebalance | target={effective_config.target_equity_pct}/{effective_config.target_bond_pct} "
            f"| drift_threshold={effective_config.drift_threshold} | dry_run={effective_config.dry_run}"
        )
        self._status = "running"

        # Shared retry policy — Temporal will automatically retry transient
        # failures (network blips, rate limits) with exponential backoff.
        retry_policy = RetryPolicy(
            initial_interval=timedelta(seconds=2),
            backoff_coefficient=2.0,
            maximum_interval=timedelta(minutes=2),
            maximum_attempts=5,
        )
        activity_opts = {
            "start_to_close_timeout": timedelta(seconds=30),
            "retry_policy": retry_policy,
            "schedule_to_close_timeout": timedelta(minutes=5),
        }

        # ----------------------------------------------------------------
        # Step 1: Get current portfolio state
        # ----------------------------------------------------------------
        snapshot: PortfolioSnapshot = await workflow.execute_activity(
            get_portfolio_positions,
            args=[effective_config.alpaca_base_url],
            **activity_opts,
        )
        workflow.logger.info(f"Portfolio value: ${snapshot.total_value:,.2f}")

        # ----------------------------------------------------------------
        # Step 2: Get live prices
        # ----------------------------------------------------------------
        prices: dict = await workflow.execute_activity(
            get_market_prices,
            args=[["SPY", "TLT"]],
            **activity_opts,
        )

        # ----------------------------------------------------------------
        # Step 3: Calculate rebalance orders (pure business logic)
        # ----------------------------------------------------------------
        rebalance: RebalanceResult = await workflow.execute_activity(
            calculate_rebalance_orders,
            args=[snapshot, prices, effective_config.target_equity_pct, effective_config.target_bond_pct, effective_config.drift_threshold],
            **activity_opts,
        )

        # ----------------------------------------------------------------
        # Step 4: Check drift threshold (or honor force signal)
        # ----------------------------------------------------------------
        needs_rebalance = rebalance.needs_rebalance or self._force_rebalance
        self._force_rebalance = False  # reset signal flag

        if not needs_rebalance:
            workflow.logger.info(
                f"No rebalance needed. SPY drift={rebalance.spy_drift:.2%}, TLT drift={rebalance.tlt_drift:.2%}"
            )
            result = RebalanceWorkflowResult(
                status="skipped",
                snapshot_before=snapshot.__dict__,
                orders_placed=[],
                snapshot_after=snapshot.__dict__,
                message=f"Drift within threshold. SPY={rebalance.spy_drift:.2%}, TLT={rebalance.tlt_drift:.2%}",
            )
            self._last_result = result
            self._status = "idle"
            return result

        # ----------------------------------------------------------------
        # Step 5: Execute orders — Saga pattern
        #
        # Temporal doesn't have native transactions, but we implement a
        # compensation (saga) pattern: if the TLT order fails after SPY
        # succeeded, we attempt to reverse the SPY order.
        # ----------------------------------------------------------------
        placed_orders = []
        compensation_needed = False

        if not effective_config.dry_run:
            for i, order in enumerate(rebalance.orders):
                try:
                    result_order = await workflow.execute_activity(
                        execute_orders,
                        args=[[order], effective_config.alpaca_base_url],
                        **activity_opts,
                    )
                    placed_orders.extend(result_order)
                except Exception as e:
                    workflow.logger.error(f"Order {i} failed: {e}. Triggering compensation.")
                    compensation_needed = True
                    break

            if compensation_needed and placed_orders:
                # Saga compensation: reverse already-placed orders
                compensating = [
                    {**o, "side": "sell" if o["side"] == "buy" else "buy"}
                    for o in placed_orders
                ]
                try:
                    await workflow.execute_activity(
                        execute_orders,
                        args=[compensating, effective_config.alpaca_base_url],
                        **activity_opts,
                    )
                    workflow.logger.info("Compensation orders placed successfully")
                except Exception as comp_e:
                    workflow.logger.error(f"Compensation also failed: {comp_e} — MANUAL INTERVENTION REQUIRED")
        else:
            # Dry run: simulate the orders
            placed_orders = [
                {**o, "status": "simulated", "id": f"dry-run-{i}"}
                for i, o in enumerate(rebalance.orders)
            ]

        # ----------------------------------------------------------------
        # Step 6: Get updated snapshot after execution
        # ----------------------------------------------------------------
        if placed_orders and not effective_config.dry_run and not compensation_needed:
            # Brief wait for orders to settle before re-fetching
            await workflow.sleep(timedelta(seconds=5))
            snapshot_after: PortfolioSnapshot = await workflow.execute_activity(
                get_portfolio_positions,
                args=[effective_config.alpaca_base_url],
                **activity_opts,
            )
        else:
            snapshot_after = snapshot

        # ----------------------------------------------------------------
        # Step 7: Notify
        # ----------------------------------------------------------------
        status_str = "compensated" if compensation_needed else ("dry_run" if effective_config.dry_run else "completed")
        final_result = RebalanceWorkflowResult(
            status=status_str,
            snapshot_before=snapshot.__dict__,
            orders_placed=placed_orders,
            snapshot_after=snapshot_after.__dict__,
            message=f"Rebalanced: {len(placed_orders)} orders | SPY drift was {rebalance.spy_drift:.2%}",
        )

        await workflow.execute_activity(
            send_notification,
            args=[final_result.__dict__],
            start_to_close_timeout=timedelta(seconds=10),
        )

        self._last_result = final_result
        self._status = "idle"
        workflow.logger.info(f"Rebalance complete: {status_str}")
        return final_result
