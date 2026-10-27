"""Dead-letter queue tooling. Failed invoices land on RabbitMQ `invoices.dlq`;
an operator lists them, fixes the cause, and replays them.

    invoice-flow dlq list
    invoice-flow dlq replay            # replay everything on the queue
"""

from __future__ import annotations

import json

import aio_pika
from temporalio.client import Client

from invoice_flow.config import Settings
from invoice_flow.db import Database
from invoice_flow.ingest import start


async def list_dead_letters(settings: Settings, limit: int = 100) -> list[dict]:
    """Peek without consuming: messages are rejected back onto the queue."""
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    out = []
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(settings.dlq_name, durable=True)
        seen = []
        for _ in range(limit):
            msg = await queue.get(no_ack=False, fail=False)
            if msg is None:
                break
            out.append(json.loads(msg.body))
            seen.append(msg)
        for msg in seen:
            await msg.nack(requeue=True)
    return out


async def replay(settings: Settings, db: Database, temporal: Client) -> list[str]:
    """Start a fresh workflow run for every dead-lettered invoice and remove it from the queue."""
    connection = await aio_pika.connect_robust(settings.rabbitmq_url)
    replayed = []
    async with connection:
        channel = await connection.channel()
        queue = await channel.declare_queue(settings.dlq_name, durable=True)
        while (msg := await queue.get(no_ack=False, fail=False)) is not None:
            body = json.loads(msg.body)
            inv = await db.get(body["invoice_id"])
            if inv is None or inv.status not in ("failed",):
                await msg.ack()  # already handled some other way
                continue
            await db.update(inv.id, status="received", reason=f"replayed after: {body['error']}")
            await start(temporal, settings, inv.id, inv.workflow_id, rerun=True)
            await msg.ack()
            replayed.append(inv.id)
    return replayed
