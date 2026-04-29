"""
Temporal Client — Start, Schedule, and Signal Workflows

Run this to:
  python portfolio_trigger.py start          # Single run (now)
  python portfolio_trigger.py schedule       # Mon-Fri 7:05am PDT schedule
  python portfolio_trigger.py signal-force   # Force rebalance via signal
  python portfolio_trigger.py query-status   # Query workflow state
  python portfolio_trigger.py dry-run        # Simulate without placing orders
  python portfolio_trigger.py delete-schedule # Delete the existing schedule
"""

import asyncio
import sys
import os
from temporalio.client import Client, Schedule, ScheduleActionStartWorkflow, ScheduleSpec, ScheduleCalendarSpec, ScheduleRange

from workflows.portfolio_rebalance_workflow import PortfolioRebalanceWorkflow, RebalanceConfig

TASK_QUEUE = "portfolio-rebalancer"
WORKFLOW_ID = "portfolio-rebalancer-main"
SCHEDULE_ID = "portfolio-rebalancer-daily"


def get_config(dry_run: bool = False) -> RebalanceConfig:
    return RebalanceConfig(
        target_equity_pct=0.60,
        target_bond_pct=0.40,
        drift_threshold=0.05,
        dry_run=dry_run,
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
    )


async def start_single_run(dry_run: bool = False):
    """Start a one-off workflow execution."""
    client = await Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233"))

    handle = await client.start_workflow(
        PortfolioRebalanceWorkflow.run,
        get_config(dry_run),
        id=WORKFLOW_ID + ("-dry" if dry_run else "-live"),
        task_queue=TASK_QUEUE,
    )
    print(f"Started workflow: {handle.id} (run: {handle.result_run_id})")
    print("Waiting for result...")

    result = await handle.result()
    print(f"\n✅ Result:")
    print(f"  Status:  {result.status}")
    print(f"  Message: {result.message}")
    print(f"  Orders:  {len(result.orders_placed)}")
    for order in result.orders_placed:
        print(f"    {order['side'].upper():4} {order['qty']:.4f} {order['symbol']} (~${order['notional']:,.2f}) — {order['reason']}")


async def create_schedule():
    """
    Create a Temporal Schedule that runs the rebalancer Mon-Fri at 7:05 AM PDT.
    Temporal Schedules replace cron jobs with durable, observable scheduling.
    """
    client = await Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233"))

    schedule = Schedule(
        action=ScheduleActionStartWorkflow(
            PortfolioRebalanceWorkflow.run,
            get_config(),
            id=WORKFLOW_ID,
            task_queue=TASK_QUEUE,
        ),
        spec=ScheduleSpec(
            # Run Mon-Fri at 14:05 UTC (7:05am PDT)
            calendars=[
                ScheduleCalendarSpec(
                    hour=[ScheduleRange(14)],
                    minute=[ScheduleRange(5)],
                    day_of_week=[ScheduleRange(1, 5)],  # Mon(1) through Fri(5)
                ),
            ]
        ),
    )

    handle = await client.create_schedule(SCHEDULE_ID, schedule)
    print(f"✅ Schedule created: {SCHEDULE_ID}")
    print("The rebalancer will run Mon-Fri at 7:05am PDT")
    print(f"Manage at: http://localhost:8080/schedules/{SCHEDULE_ID}")


async def delete_schedule():
    """Delete the existing schedule so it can be recreated with new settings."""
    client = await Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233"))
    handle = client.get_schedule_handle(SCHEDULE_ID)
    await handle.delete()
    print(f"✅ Schedule deleted: {SCHEDULE_ID}")
    print("Run 'python portfolio_trigger.py schedule' to recreate it.")


async def signal_force_rebalance():
    """Send a signal to an existing workflow to force immediate rebalancing."""
    client = await Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233"))
    handle = client.get_workflow_handle(WORKFLOW_ID)
    await handle.signal(PortfolioRebalanceWorkflow.force_rebalance)
    print(f"✅ Sent force_rebalance signal to {WORKFLOW_ID}")


async def query_status():
    """Query the running workflow's current state."""
    client = await Client.connect(os.getenv("TEMPORAL_HOST", "localhost:7233"))
    handle = client.get_workflow_handle(WORKFLOW_ID)
    status = await handle.query(PortfolioRebalanceWorkflow.get_status)
    import json
    print(json.dumps(status, indent=2, default=str))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "dry-run"

    commands = {
        "start":        lambda: start_single_run(dry_run=False),
        "dry-run":      lambda: start_single_run(dry_run=True),
        "schedule":         create_schedule,
        "delete-schedule":  delete_schedule,
        "signal-force": signal_force_rebalance,
        "query-status": query_status,
    }

    if cmd not in commands:
        print(f"Unknown command: {cmd}")
        print(f"Available: {', '.join(commands.keys())}")
        sys.exit(1)

    asyncio.run(commands[cmd]())
