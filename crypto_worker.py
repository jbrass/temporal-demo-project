"""
Crypto Worker — registers the perpetual crypto rebalance workflow and activities.

Run alongside the existing SPY/TLT worker, or replace it.
Uses the same Temporal connection logic (local or cloud TLS).
"""

import asyncio
import logging
import os
from concurrent.futures import ThreadPoolExecutor
from temporalio.client import Client, TLSConfig
from temporalio.worker import Worker

from workflows.crypto_rebalance_workflow import CryptoRebalanceWorkflow
from activities.crypto_activities import (
    get_crypto_positions,
    get_crypto_prices,
    calculate_crypto_rebalance,
    execute_crypto_orders,
    send_crypto_notification,
)

TASK_QUEUE = "crypto-rebalancer"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


async def main():
    temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    tls_cert_path = os.getenv("TEMPORAL_TLS_CERT")
    tls_key_path = os.getenv("TEMPORAL_TLS_KEY")

    tls: TLSConfig | bool = False
    if tls_cert_path and tls_key_path:
        logger.info("Connecting with mTLS (Temporal Cloud)")
        with open(tls_cert_path, "rb") as f:
            client_cert = f.read()
        with open(tls_key_path, "rb") as f:
            client_key = f.read()
        tls = TLSConfig(client_cert=client_cert, client_private_key=client_key)
    else:
        logger.info("Connecting without TLS (local)")

    logger.info(f"Connecting to {temporal_host} | namespace={namespace}")
    client = await Client.connect(temporal_host, namespace=namespace, tls=tls)

    with ThreadPoolExecutor(max_workers=100) as activity_executor:
        worker = Worker(
            client,
            task_queue=TASK_QUEUE,
            workflows=[CryptoRebalanceWorkflow],
            activities=[
                get_crypto_positions,
                get_crypto_prices,
                calculate_crypto_rebalance,
                execute_crypto_orders,
                send_crypto_notification,
            ],
            activity_executor=activity_executor,
            max_concurrent_activities=10,
        )

        logger.info(f"Crypto worker started | task queue: {TASK_QUEUE}")
        await worker.run()


if __name__ == "__main__":
    asyncio.run(main())
