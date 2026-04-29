"""
Crypto Client — Start, signal, and query the perpetual crypto rebalancer.

Usage:
  python crypto_trigger.py start              # start perpetual workflow
  python crypto_trigger.py dry-run            # start in dry-run mode
  python crypto_trigger.py force              # signal: rebalance now
  python crypto_trigger.py pause              # signal: pause cycling
  python crypto_trigger.py resume             # signal: resume cycling
  python crypto_trigger.py set-interval 1.5   # signal: change interval to 1.5h
  python crypto_trigger.py status             # query current state
  python crypto_trigger.py stop               # terminate the workflow
"""

import asyncio
import sys
import os
from temporalio.client import Client, TLSConfig

from workflows.crypto_rebalance_workflow import CryptoRebalanceWorkflow, CryptoRebalanceConfig

TASK_QUEUE = "crypto-rebalancer"
WORKFLOW_ID = "crypto-rebalancer-perpetual"


async def get_client() -> Client:
    temporal_host = os.getenv("TEMPORAL_HOST", "localhost:7233")
    namespace = os.getenv("TEMPORAL_NAMESPACE", "default")
    tls_cert_path = os.getenv("TEMPORAL_TLS_CERT")
    tls_key_path = os.getenv("TEMPORAL_TLS_KEY")

    tls: TLSConfig | bool = False
    if tls_cert_path and tls_key_path:
        with open(tls_cert_path, "rb") as f:
            client_cert = f.read()
        with open(tls_key_path, "rb") as f:
            client_key = f.read()
        tls = TLSConfig(client_cert=client_cert, client_private_key=client_key)

    return await Client.connect(temporal_host, namespace=namespace, tls=tls)


def get_config(dry_run: bool = False) -> CryptoRebalanceConfig:
    return CryptoRebalanceConfig(
        target_btc_pct=0.60,
        target_eth_pct=0.40,
        drift_threshold=0.01,
        interval_hours=float(os.getenv("REBALANCE_INTERVAL_HOURS", ".5")),
        dry_run=dry_run,
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets"),
    )


async def start(dry_run: bool = False):
    client = await get_client()
    handle = await client.start_workflow(
        CryptoRebalanceWorkflow.run,
        get_config(dry_run),
        id=WORKFLOW_ID,
        task_queue=TASK_QUEUE,
    )
    mode = "DRY RUN" if dry_run else "LIVE"
    print(f"✅ Started [{mode}] workflow: {handle.id}")
    print(f"   Run ID: {handle.result_run_id}")
    print(f"   UI: http://localhost:8233/namespaces/default/workflows/{handle.id}")
    print("\nWorkflow is now running perpetually. Use signals to control it.")


async def signal(signal_name: str, *args):
    client = await get_client()
    handle = client.get_workflow_handle(WORKFLOW_ID)
    method = getattr(CryptoRebalanceWorkflow, signal_name)
    await handle.signal(method, *args)
    print(f"✅ Sent signal: {signal_name}" + (f" {args[0]}" if args else ""))


async def query_status():
    client = await get_client()
    handle = client.get_workflow_handle(WORKFLOW_ID)
    status = await handle.query(CryptoRebalanceWorkflow.get_status)
    import json
    print(json.dumps(status, indent=2, default=str))


async def stop():
    client = await get_client()
    handle = client.get_workflow_handle(WORKFLOW_ID)
    await handle.terminate(reason="Manual stop via crypto_trigger.py")
    print(f"✅ Workflow {WORKFLOW_ID} terminated")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "status"

    if cmd == "start":
        asyncio.run(start(dry_run=False))
    elif cmd == "dry-run":
        asyncio.run(start(dry_run=True))
    elif cmd == "force":
        asyncio.run(signal("force_rebalance"))
    elif cmd == "pause":
        asyncio.run(signal("pause"))
    elif cmd == "resume":
        asyncio.run(signal("resume"))
    elif cmd == "set-interval":
        hours = float(sys.argv[2]) if len(sys.argv) > 2 else 2.0
        asyncio.run(signal("set_interval", hours))
    elif cmd == "status":
        asyncio.run(query_status())
    elif cmd == "stop":
        asyncio.run(stop())
    else:
        print(f"Unknown command: {cmd}")
        print(__doc__)
        sys.exit(1)
