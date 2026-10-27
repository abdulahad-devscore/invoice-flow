"""The workflow end to end on a real Temporal server, with a fake ERP bridge.

Uses Temporal's time-skipping test server when it can be downloaded (CI);
otherwise a dev server at TEMPORAL_ADDRESS (docker compose / `temporal server
start-dev`). Skipped if neither is available.
"""

import os
import sys
import uuid
from datetime import date
from pathlib import Path

import httpx
import pytest
from temporalio import activity
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import Worker

from invoice_flow.activities import InvoiceActivities
from invoice_flow.config import Settings
from invoice_flow.db import Database
from invoice_flow.erp import BridgeError
from invoice_flow.extract import RulesExtractor
from invoice_flow.ingest import submit
from invoice_flow.models import Decision
from invoice_flow.workflow import InvoiceWorkflow

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from generate_invoices import build_invoice, render  # noqa: E402

PO = {
    "number": "P00009",
    "supplier": "Klang Valley Plastics Sdn Bhd",
    "state": "purchase",
    "lines": [
        {
            "line_id": 1,
            "sku": "RM-PP-RESIN",
            "product": "[RM-PP-RESIN] Polypropylene Resin (food grade)",
            "qty_ordered": 1000,
            "qty_received": 800,
            "qty_billed": 0,
            "unit_price": 6.20,
            "tax_percent": 0,
        }
    ],
}


class FakeBridge:
    def __init__(self):
        self.bills: dict[str, dict] = {}
        self.writes = 0
        self.fail: str | None = None
        self.drop_response_once = False

    async def purchase_order(self, po_number):
        return PO if po_number == "P00009" else None

    async def propose_bill(self, invoice, lines, evidence):
        if self.fail:
            raise BridgeError(self.fail)
        key = invoice["supplier_invoice_number"]
        if key not in self.bills:  # the bridge's idempotency: same key, same action
            self.writes += 1
            self.bills[key] = {
                "id": str(uuid.uuid4()),
                "status": "executed",
                "odoo_ref": f"BILL/2026/{self.writes:04d}",
                "evidence": evidence,
            }
        if self.drop_response_once:  # Odoo wrote it, then the connection dropped
            self.drop_response_once = False
            raise httpx.ConnectError("connection reset by peer")
        return self.bills[key]


class RecordingActivities(InvoiceActivities):
    dead_letters: list = []

    @activity.defn(name="send_to_dlq")
    async def send_to_dlq(self, invoice_id: str, step: str, error: str) -> None:
        self.dead_letters.append((invoice_id, step, error))


@pytest.fixture(scope="session")
async def temporal():
    try:
        env = await WorkflowEnvironment.start_time_skipping(data_converter=pydantic_data_converter)
    except RuntimeError:
        try:
            client = await Client.connect(
                os.environ.get("TEMPORAL_ADDRESS", "localhost:7233"), data_converter=pydantic_data_converter
            )
        except RuntimeError:
            pytest.skip("no Temporal server available")
        yield client, False
        return
    yield env.client, True
    await env.shutdown()


@pytest.fixture
async def stack(temporal, tmp_path):
    client, time_skipping = temporal
    settings = Settings(database_url=f"sqlite+aiosqlite:///{tmp_path}/inv.db", task_queue=f"test-{uuid.uuid4()}")
    db = Database(settings.database_url)
    await db.create_all()
    bridge = FakeBridge()
    acts = RecordingActivities(settings, db, bridge, extractor=RulesExtractor())
    acts.dead_letters = []
    async with Worker(client, task_queue=settings.task_queue, workflows=[InvoiceWorkflow], activities=acts.all()):
        yield {
            "client": client,
            "settings": settings,
            "db": db,
            "bridge": bridge,
            "acts": acts,
            "time_skipping": time_skipping,
            "dir": tmp_path,
        }


def make_pdf(directory: Path, number: str, scenario: str = "clean", po: dict = PO) -> bytes:
    path = directory / f"{number}-{uuid.uuid4().hex[:6]}.pdf"
    render(build_invoice(po, scenario, number, date(2026, 10, 25)), path)
    return path.read_bytes()


async def run(stack, pdf: bytes):
    invoice_id, created = await submit(stack["db"], stack["client"], stack["settings"], pdf, "invoice.pdf")
    assert created
    inv = await stack["db"].get(invoice_id)
    return invoice_id, stack["client"].get_workflow_handle_for(InvoiceWorkflow.run, inv.workflow_id)


async def wait_status(stack, invoice_id: str, status: str):
    import asyncio

    for _ in range(100):
        inv = await stack["db"].get(invoice_id)
        if inv.status == status:
            return inv
        await asyncio.sleep(0.1)
    raise AssertionError(f"invoice stayed {inv.status}, expected {status}")


async def test_matched_invoice_posts_straight_through(stack):
    invoice_id, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0001"))
    outcome = await handle.result()
    assert outcome.status == "posted"
    inv = await stack["db"].get(invoice_id)
    assert inv.bill_ref == "BILL/2026/0001"
    assert inv.processing_seconds is not None
    assert stack["bridge"].writes == 1


async def test_mismatch_waits_for_a_person_then_posts(stack):
    invoice_id, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0002", "qty_over_received"))
    inv = await wait_status(stack, invoice_id, "waiting_for_human")
    assert "only 800 received" in inv.reason
    assert stack["bridge"].writes == 0
    await handle.signal(InvoiceWorkflow.review, Decision(approve=True, reviewer="Mei Ling (AP)", note="Agreed"))
    outcome = await handle.result()
    assert outcome.status == "posted"
    assert stack["bridge"].bills["KVP-INV-0002"]["evidence"]["reviewed_by"] == "Mei Ling (AP)"


async def test_rejected_by_reviewer(stack):
    invoice_id, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0003", "price_up_5pct"))
    await wait_status(stack, invoice_id, "waiting_for_human")
    await handle.signal(InvoiceWorkflow.review, Decision(approve=False, reviewer="Mei Ling (AP)", note="Wrong price"))
    assert (await handle.result()).status == "rejected"
    assert stack["bridge"].writes == 0


async def test_duplicate_invoice_number_is_caught(stack):
    _, first = await run(stack, make_pdf(stack["dir"], "KVP-INV-0004"))
    assert (await first.result()).status == "posted"
    _, second = await run(stack, make_pdf(stack["dir"], "KVP-INV-0004"))  # re-sent: different file, same invoice
    assert (await second.result()).status == "duplicate"
    assert stack["bridge"].writes == 1


async def test_identical_file_is_ingested_once(stack):
    pdf = make_pdf(stack["dir"], "KVP-INV-0005")
    first_id, created = await submit(stack["db"], stack["client"], stack["settings"], pdf, "a.pdf")
    again_id, created_again = await submit(stack["db"], stack["client"], stack["settings"], pdf, "a.pdf")
    assert created and not created_again and first_id == again_id


async def test_dropped_response_after_write_does_not_double_post(stack):
    stack["bridge"].drop_response_once = True
    _, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0006"))
    assert (await handle.result()).status == "posted"  # the activity retried
    assert stack["bridge"].writes == 1  # ...and found the bill it had already written


async def test_bridge_refusal_goes_to_dead_letter_queue(stack):
    stack["bridge"].fail = "Odoo rejected the write: period is locked"
    invoice_id, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0007"))
    outcome = await handle.result()
    assert outcome.status == "failed"
    assert stack["acts"].dead_letters == [(invoice_id, "post_bill", "Odoo rejected the write: period is locked")]


async def test_unknown_po_cannot_be_approved_into_the_erp(stack):
    invoice_id, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0008", "unknown_po"))
    inv = await wait_status(stack, invoice_id, "waiting_for_human")
    assert "P09999 not found" in inv.reason
    await handle.signal(InvoiceWorkflow.review, Decision(approve=True, reviewer="Mei Ling (AP)"))
    outcome = await handle.result()
    assert outcome.status == "rejected"
    assert stack["bridge"].writes == 0


async def test_review_timeout_dead_letters(stack):
    if not stack["time_skipping"]:
        pytest.skip("needs the time-skipping test server to fast-forward 72 hours")
    invoice_id, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0009", "price_up_5pct"))
    outcome = await handle.result()  # time skips past the 72 h review window
    assert outcome.status == "failed"
    assert stack["acts"].dead_letters[-1][1] == "human_review"


async def test_two_copies_at_once_keep_exactly_one(stack):
    import asyncio

    pdfs = [make_pdf(stack["dir"], "KVP-INV-0010"), make_pdf(stack["dir"], "KVP-INV-0010")]
    handles = [h for _, h in await asyncio.gather(*(run(stack, p) for p in pdfs))]
    outcomes = sorted(o.status for o in await asyncio.gather(*(h.result() for h in handles)))
    assert outcomes == ["duplicate", "posted"]
    assert stack["bridge"].writes == 1


async def test_dead_lettered_invoice_can_be_replayed(stack):
    from invoice_flow.ingest import start

    stack["bridge"].fail = "ERP bridge unreachable"
    invoice_id, handle = await run(stack, make_pdf(stack["dir"], "KVP-INV-0011"))
    assert (await handle.result()).status == "failed"
    stack["bridge"].fail = None  # someone fixed the outage
    inv = await stack["db"].get(invoice_id)
    await start(stack["client"], stack["settings"], invoice_id, inv.workflow_id, rerun=True)
    replayed = stack["client"].get_workflow_handle_for(InvoiceWorkflow.run, inv.workflow_id)
    assert (await replayed.result()).status == "posted"
