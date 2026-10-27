"""AP review queue and invoice API.

GET  /review                 the human queue (mismatches wait here)
POST /review/{id}            approve or reject -> Temporal signal
GET  /api/invoices           list, filter by status
GET  /api/invoices/{id}      one invoice with extraction and match details
GET  /api/invoices/{id}/pdf  the original PDF
POST /api/invoices           upload a PDF (same path as email)
GET  /api/stats              numbers behind the Grafana dashboard
"""

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from temporalio.client import Client
from temporalio.contrib.pydantic import pydantic_data_converter

from invoice_flow.config import Settings, get_settings
from invoice_flow.db import Database, Invoice
from invoice_flow.ingest import submit
from invoice_flow.models import Decision
from invoice_flow.workflow import InvoiceWorkflow

TEMPLATES = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))
FIELDS = [
    "id",
    "status",
    "reason",
    "filename",
    "sender",
    "supplier_name",
    "supplier_invoice_number",
    "po_number",
    "amount_total",
    "bill_ref",
    "reviewer",
    "review_note",
    "extraction_model",
    "extraction_cost_usd",
    "processing_seconds",
    "received_at",
    "completed_at",
    "workflow_id",
]


def summary(inv: Invoice, full: bool = False) -> dict:
    out = {f: getattr(inv, f) for f in FIELDS}
    if full:
        out.update(extraction=inv.extraction, match=inv.match)
    return out


def create_app(settings: Settings | None = None) -> FastAPI:
    settings = settings or get_settings()
    db = Database(settings.database_url)
    state: dict = {}

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        await db.create_all()
        state["temporal"] = await Client.connect(
            settings.temporal_address, namespace=settings.temporal_namespace, data_converter=pydantic_data_converter
        )
        yield
        await db.engine.dispose()

    app = FastAPI(title="Invoice Flow", version="1.0.0", lifespan=lifespan)

    @app.get("/healthz")
    async def healthz():
        return {"status": "ok"}

    @app.get("/", include_in_schema=False)
    async def root():
        return RedirectResponse("/review")

    @app.get("/api/invoices")
    async def list_invoices(status: str | None = None, limit: int = 100):
        q = select(Invoice).order_by(Invoice.received_at.desc()).limit(limit)
        if status:
            q = q.where(Invoice.status == status)
        async with db.session() as s:
            return [summary(i) for i in (await s.scalars(q)).all()]

    @app.get("/api/invoices/{invoice_id}")
    async def get_invoice(invoice_id: str):
        inv = await db.get(invoice_id)
        if not inv:
            raise HTTPException(404)
        return summary(inv, full=True)

    @app.get("/api/invoices/{invoice_id}/pdf")
    async def get_pdf(invoice_id: str):
        inv = await db.get(invoice_id)
        if not inv:
            raise HTTPException(404)
        return Response(
            inv.pdf, media_type="application/pdf", headers={"Content-Disposition": f'inline; filename="{inv.filename}"'}
        )

    @app.post("/api/invoices", status_code=202)
    async def upload(file: Annotated[UploadFile, File()]):
        pdf = await file.read()
        if not pdf.startswith(b"%PDF"):
            raise HTTPException(415, "PDF files only")
        invoice_id, created = await submit(
            db, state["temporal"], settings, pdf, file.filename or "upload.pdf", source="upload"
        )
        return {"invoice_id": invoice_id, "created": created}

    @app.get("/api/stats")
    async def stats():
        async with db.session() as s:
            counts = dict((await s.execute(select(Invoice.status, func.count()).group_by(Invoice.status))).all())
            avg_auto = await s.scalar(
                select(func.avg(Invoice.processing_seconds)).where(
                    Invoice.status == "posted", Invoice.reviewer.is_(None)
                )
            )
            cost = await s.scalar(select(func.sum(Invoice.extraction_cost_usd)))
        return {
            "by_status": counts,
            "avg_seconds_straight_through": round(avg_auto or 0, 1),
            "extraction_cost_usd": round(cost or 0, 4),
        }

    @app.get("/review", response_class=HTMLResponse, include_in_schema=False)
    async def review_page(request: Request):
        async with db.session() as s:
            waiting = (
                await s.scalars(
                    select(Invoice).where(Invoice.status == "waiting_for_human").order_by(Invoice.waiting_since)
                )
            ).all()
            recent = (
                await s.scalars(
                    select(Invoice)
                    .where(Invoice.status != "waiting_for_human")
                    .order_by(Invoice.received_at.desc())
                    .limit(30)
                )
            ).all()
        return TEMPLATES.TemplateResponse(request, "review.html", {"waiting": waiting, "recent": recent})

    @app.post("/review/{invoice_id}", include_in_schema=False)
    async def review(
        invoice_id: str,
        decision: Annotated[str, Form()],
        reviewer: Annotated[str, Form()],
        note: Annotated[str | None, Form()] = None,
    ):
        inv = await db.get(invoice_id)
        if not inv or inv.status != "waiting_for_human":
            raise HTTPException(409, "invoice is not waiting for review")
        if not reviewer.strip():
            raise HTTPException(422, "reviewer name required")
        handle = state["temporal"].get_workflow_handle(inv.workflow_id)
        await handle.signal(
            InvoiceWorkflow.review, Decision(approve=decision == "approve", reviewer=reviewer.strip(), note=note)
        )
        return RedirectResponse("/review", status_code=303)

    return app
