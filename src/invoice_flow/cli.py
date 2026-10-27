"""invoice-flow worker | poll | api | dlq list | dlq replay"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging

from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.worker import Worker

from invoice_flow.activities import InvoiceActivities
from invoice_flow.config import get_settings
from invoice_flow.db import Database
from invoice_flow.erp import BridgeClient
from invoice_flow.workflow import InvoiceWorkflow


async def connect(settings) -> Client:
    for attempt in range(30):  # Temporal may still be starting
        try:
            return await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace, data_converter=pydantic_data_converter
            )
        except RuntimeError:
            await asyncio.sleep(min(2 + attempt, 10))
    raise SystemExit("could not connect to Temporal")


async def worker() -> None:
    s = get_settings()
    db = Database(s.database_url)
    await db.create_all()
    acts = InvoiceActivities(s, db, BridgeClient(s.bridge_url, s.bridge_client_id, s.bridge_client_secret))
    client = await connect(s)
    await Worker(client, task_queue=s.task_queue, workflows=[InvoiceWorkflow], activities=acts.all()).run()


async def poll() -> None:
    from invoice_flow.ingest import poll_forever

    s = get_settings()
    db = Database(s.database_url)
    await db.create_all()
    await poll_forever(db, await connect(s), s)


async def dlq(action: str) -> None:
    from invoice_flow.dlq import list_dead_letters, replay

    s = get_settings()
    if action == "list":
        print(json.dumps(await list_dead_letters(s), indent=2))
    else:
        print(json.dumps(await replay(s, Database(s.database_url), await connect(s)), indent=2))


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")
    p = argparse.ArgumentParser(prog="invoice-flow")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("worker")
    sub.add_parser("poll")
    api = sub.add_parser("api")
    api.add_argument("--port", type=int, default=8200)
    d = sub.add_parser("dlq")
    d.add_argument("action", choices=["list", "replay"])
    args = p.parse_args()
    if args.cmd == "api":
        import uvicorn

        from invoice_flow.api import create_app

        uvicorn.run(create_app(), host="0.0.0.0", port=args.port)
    elif args.cmd == "dlq":
        asyncio.run(dlq(args.action))
    else:
        asyncio.run({"worker": worker, "poll": poll}[args.cmd]())


if __name__ == "__main__":
    main()
