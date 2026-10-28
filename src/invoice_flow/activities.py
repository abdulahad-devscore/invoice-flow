"""Temporal activities: everything that touches the outside world.

Each activity is safe to retry: database updates are idempotent per invoice,
the ERP write goes through the bridge with a stable idempotency key, and the
bridge itself finds an existing bill before creating one. Errors that will
never succeed on retry are raised as non-retryable so the workflow can route
them to a person or the dead-letter queue straight away.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime

import aio_pika
from temporalio import activity
from temporalio.exceptions import ApplicationError

from invoice_flow.config import Settings
from invoice_flow.db import Database
from invoice_flow.erp import BridgeClient, BridgeError
from invoice_flow.extract import ExtractionFailed, LLMExtractor, RulesExtractor, Unreadable, pdf_text, textract_text


class InvoiceActivities:
    def __init__(self, settings: Settings, db: Database, bridge: BridgeClient, extractor=None):
        self.settings = settings
        self.db = db
        self.bridge = bridge
        self.extractor = extractor or (
            RulesExtractor()
            if settings.extractor == "rules"
            else LLMExtractor(settings.extract_model, settings.extract_fallback_model)
        )

    @activity.defn
    async def mark(self, invoice_id: str, status: str, reason: str | None = None) -> None:
        values = {"status": status, "reason": reason}
        if status == "waiting_for_human":
            values["waiting_since"] = datetime.now(UTC)
        await self.db.update(invoice_id, **values)

    @activity.defn
    async def extract(self, invoice_id: str) -> dict:
        inv = await self.db.get(invoice_id)
        text = pdf_text(inv.pdf)
        if text.method == "none" and self.settings.textract_enabled:
            text = textract_text(inv.pdf, self.settings.aws_region)
        try:
            result = await self.extractor.extract(text, inv.pdf)
        except (ExtractionFailed, Unreadable) as exc:
            raise ApplicationError(str(exc), type=type(exc).__name__, non_retryable=True) from exc
        data = result.invoice.model_dump(mode="json")
        await self.db.update(
            invoice_id,
            extraction=data,
            extraction_model=result.model,
            extraction_cost_usd=result.cost_usd,
            supplier_name=data["supplier_name"],
            supplier_invoice_number=data["supplier_invoice_number"],
            po_number=data["po_number"],
            amount_total=data["total"],
            attempts=inv.attempts + 1,
        )
        return data

    @activity.defn
    async def check_duplicate(self, invoice_id: str, supplier: str, number: str) -> str | None:
        return await self.db.claim(invoice_id, supplier, number)

    @activity.defn
    async def fetch_po(self, po_number: str) -> dict | None:
        try:
            return await self.bridge.purchase_order(po_number)
        except BridgeError as exc:
            raise ApplicationError(str(exc), type="BridgeError", non_retryable=True) from exc

    @activity.defn
    async def save_match(self, invoice_id: str, match: dict) -> None:
        await self.db.update(invoice_id, match=match)

    @activity.defn
    async def record_review(self, invoice_id: str, reviewer: str, note: str | None) -> None:
        await self.db.update(invoice_id, reviewer=reviewer, review_note=note)

    @activity.defn
    async def post_bill(self, invoice_id: str, invoice: dict, match: dict, reviewer: str | None) -> dict:
        lines = [
            {"sku": m["po_line_sku"], "quantity": m["quantity"], "unit_price": m["unit_price"]} for m in match["lines"]
        ]
        evidence = {
            "matched": match["matched"],
            "issues": match["issues"],
            "reviewed_by": reviewer,
            "invoice_id": invoice_id,
        }
        try:
            action = await self.bridge.propose_bill(invoice, lines, evidence)
        except BridgeError as exc:
            raise ApplicationError(str(exc), type="BridgeError", non_retryable=True) from exc
        if action["status"] != "executed":
            raise ApplicationError(
                f"bridge action {action['id']} is {action['status']}: {action.get('error')}",
                type="BillNotPosted",
                non_retryable=True,
            )
        await self.db.update(invoice_id, bill_ref=action["odoo_ref"], bridge_action_id=action["id"])
        return action

    @activity.defn
    async def finish(self, invoice_id: str, status: str, reason: str | None, processing_seconds: float) -> None:
        await self.db.update(
            invoice_id,
            status=status,
            reason=reason,
            completed_at=datetime.now(UTC),
            processing_seconds=round(processing_seconds, 2),
        )

    @activity.defn
    async def send_to_dlq(self, invoice_id: str, step: str, error: str) -> None:
        connection = await aio_pika.connect_robust(self.settings.rabbitmq_url)
        async with connection:
            channel = await connection.channel()
            await channel.declare_queue(self.settings.dlq_name, durable=True)
            body = {
                "invoice_id": invoice_id,
                "workflow_id": activity.info().workflow_id,
                "step": step,
                "error": error,
                "at": datetime.now(UTC).isoformat(),
            }
            await channel.default_exchange.publish(
                aio_pika.Message(
                    json.dumps(body).encode(),
                    delivery_mode=aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                    message_id=f"{invoice_id}:{step}",
                ),
                routing_key=self.settings.dlq_name,
            )

    def all(self) -> list:
        return [
            self.mark,
            self.extract,
            self.check_duplicate,
            self.fetch_po,
            self.save_match,
            self.record_review,
            self.post_bill,
            self.finish,
            self.send_to_dlq,
        ]
