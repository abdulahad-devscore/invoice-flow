"""The durable invoice workflow.

    extract -> duplicate check -> fetch PO -> 3-way match
        matched ------------------------------------------> post bill -> posted
        mismatch -> wait for a person (signal, up to 72 h) -> approve -> post bill -> posted
                                                           -> reject  -> rejected
                                                           -> timeout -> dead-letter queue
    any step failing after its retries ---------------------> dead-letter queue -> failed

Temporal persists every step, so a worker crash mid-run resumes where it left
off; the ERP write is idempotent, so a crash after the bill was posted but
before Temporal recorded it does not post a second bill.
"""

from __future__ import annotations

from datetime import timedelta

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

with workflow.unsafe.imports_passed_through():
    from invoice_flow.activities import InvoiceActivities
    from invoice_flow.match import three_way_match
    from invoice_flow.models import Decision, Status, SupplierInvoice, WorkflowInput, WorkflowOutcome

RETRY = RetryPolicy(
    initial_interval=timedelta(seconds=2),
    backoff_coefficient=2.0,
    maximum_interval=timedelta(minutes=1),
    maximum_attempts=6,
    non_retryable_error_types=["ExtractionFailed", "Unreadable", "BridgeError", "BillNotPosted"],
)
QUICK = timedelta(seconds=30)
SLOW = timedelta(minutes=3)  # extraction calls a model


@workflow.defn
class InvoiceWorkflow:
    def __init__(self) -> None:
        self.decision: Decision | None = None
        self.status = Status.RECEIVED
        self.issues: list[str] = []

    @workflow.signal
    def review(self, decision: Decision) -> None:
        if self.decision is None:  # first decision wins; later clicks are ignored
            self.decision = decision

    @workflow.query
    def current(self) -> dict:
        return {"status": self.status, "issues": self.issues}

    async def _act(self, fn, *args, limit: timedelta = QUICK):
        return await workflow.execute_activity(fn, args=list(args), start_to_close_timeout=limit, retry_policy=RETRY)

    async def _set(self, invoice_id: str, status: Status, reason: str | None = None) -> None:
        self.status = status
        await self._act(InvoiceActivities.mark, invoice_id, status.value, reason)

    @workflow.run
    async def run(self, inp: WorkflowInput) -> WorkflowOutcome:
        acts = InvoiceActivities
        iid = inp.invoice_id
        started = workflow.now()
        waited = timedelta(0)
        step = "extract"

        async def finish(status: Status, reason: str | None = None, bill_ref: str | None = None) -> WorkflowOutcome:
            self.status = status
            machine_seconds = (workflow.now() - started - waited).total_seconds()
            await self._act(acts.finish, iid, status.value, reason, machine_seconds)
            return WorkflowOutcome(invoice_id=iid, status=status, bill_ref=bill_ref, reason=reason)

        try:
            await self._set(iid, Status.EXTRACTING)
            data = await self._act(acts.extract, iid, limit=SLOW)
            invoice = SupplierInvoice.model_validate(data)

            step = "duplicate_check"
            dup = await self._act(acts.check_duplicate, iid, invoice.supplier_name, invoice.supplier_invoice_number)
            if dup:
                return await finish(Status.DUPLICATE, f"same supplier invoice number as invoice {dup}")

            step = "match"
            await self._set(iid, Status.MATCHING)
            po = await self._act(acts.fetch_po, invoice.po_number) if invoice.po_number else None
            match = three_way_match(invoice, po)
            self.issues = match.issues
            await self._act(acts.save_match, iid, match.model_dump(mode="json"))

            reviewer = None
            if not match.matched:
                step = "human_review"
                await self._set(iid, Status.WAITING_FOR_HUMAN, "; ".join(match.issues))
                wait_started = workflow.now()
                try:
                    await workflow.wait_condition(
                        lambda: self.decision is not None,
                        timeout=timedelta(hours=inp.review_timeout_hours),
                    )
                except TimeoutError:
                    await self._act(
                        acts.send_to_dlq, iid, step, f"no review decision within {inp.review_timeout_hours} hours"
                    )
                    return await finish(Status.FAILED, "human review timed out")
                waited = workflow.now() - wait_started
                await self._act(acts.record_review, iid, self.decision.reviewer, self.decision.note)
                if not self.decision.approve:
                    return await finish(Status.REJECTED, self.decision.note or "rejected by reviewer")
                if po is None:
                    return await finish(Status.REJECTED, "approved, but no purchase order to bill against")
                reviewer = self.decision.reviewer

            step = "post_bill"
            await self._set(iid, Status.POSTING)
            action = await self._act(
                acts.post_bill,
                iid,
                invoice.model_dump(mode="json"),
                match.model_dump(mode="json"),
                reviewer,
                limit=timedelta(minutes=2),
            )
            return await finish(Status.POSTED, None, action["odoo_ref"])

        except ActivityError as exc:
            cause = exc.cause
            message = cause.message if isinstance(cause, ApplicationError) else str(cause or exc)
            await self._act(acts.send_to_dlq, iid, step, message)
            return await finish(Status.FAILED, f"{step}: {message}")
