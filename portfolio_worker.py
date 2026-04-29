"""
Temporal Worker — Portfolio Rebalancer

The worker registers workflows and activities with the Temporal server
and polls the task queue for work. It's horizontally scalable:
run multiple worker instances for higher throughput.
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from temporalio.client import Client
from temporalio.worker import Worker

from workflows.portfolio_rebalance_workflow import PortfolioRebalanceWorkflow
from activities.portfolio_activities import (
    get_portfolio_positions,
    get_market_prices,
    calculate_rebalance_orders,
    execute_orders,
    send_notification,
)

# Task queue name — must match what the client uses to start workflows
TASK_QUEUE = "portfolio-rebalancer"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    logger.info(f"Connecting to Temporal at {temporal_host}")

    client = await Client.connect(temporal_host)

    # Worker registers both the workflow and all activities it should handle.
    # Activities can be split across multiple workers for independent scaling.
    with ThreadPoolExecutor(max_workers=100) as activity_executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[PortfolioRebalanceWorkflow],
            activities=[
                get_portfolio_positions,
                get_market_prices,
                calculate_rebalance_orders,
                execute_orders,
                send_notification,
            ],
            activity_executor=activity_executor,
            max_concurrent_activities=10,
        )

        logger.info(f"Worker started, polling task queue: {TASK_QUEUE}")
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
