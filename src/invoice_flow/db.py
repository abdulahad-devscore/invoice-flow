"""Invoice records: one row per received PDF. The run dashboard reads this table."""

from __future__ import annotations

import asyncio
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import JSON, DateTime, Float, Integer, LargeBinary, String, Text, select
from sqlalchemy.exc import IntegrityError, ProgrammingError
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def utcnow() -> datetime:
    return datetime.now(UTC)


class Base(DeclarativeBase):
    type_annotation_map = {dict[str, Any]: JSON, list[Any]: JSON}


class Invoice(Base):
    __tablename__ = "invoices"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    sha256: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    source: Mapped[str] = mapped_column(String(20), default="email")  # email | upload
    message_id: Mapped[str | None] = mapped_column(String(300))
    sender: Mapped[str | None] = mapped_column(String(300))
    filename: Mapped[str] = mapped_column(String(300))
    pdf: Mapped[bytes] = mapped_column(LargeBinary)
    status: Mapped[str] = mapped_column(String(30), index=True, default="received")
    reason: Mapped[str | None] = mapped_column(Text)
    supplier_name: Mapped[str | None] = mapped_column(String(200))
    supplier_invoice_number: Mapped[str | None] = mapped_column(String(100), index=True)
    po_number: Mapped[str | None] = mapped_column(String(50))
    amount_total: Mapped[float | None] = mapped_column(Float)
    extraction: Mapped[dict[str, Any] | None] = mapped_column()
    extraction_model: Mapped[str | None] = mapped_column(String(60))
    extraction_cost_usd: Mapped[float | None] = mapped_column(Float)
    match: Mapped[dict[str, Any] | None] = mapped_column()
    reviewer: Mapped[str | None] = mapped_column(String(200))
    review_note: Mapped[str | None] = mapped_column(Text)
    bill_ref: Mapped[str | None] = mapped_column(String(64))
    bridge_action_id: Mapped[str | None] = mapped_column(String(36))
    workflow_id: Mapped[str | None] = mapped_column(String(100))
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    waiting_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_seconds: Mapped[float | None] = mapped_column(Float)  # machine time, excludes waiting for people


class Database:
    def __init__(self, url: str):
        self.engine = create_async_engine(url, pool_pre_ping=True)
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)

    async def create_all(self) -> None:
        # worker, poller and API all start together; if another process wins the race to
        # create the tables, the retry sees them and does nothing.
        for attempt in range(3):
            try:
                async with self.engine.begin() as conn:
                    await conn.run_sync(Base.metadata.create_all)
                return
            except (ProgrammingError, IntegrityError):
                if attempt == 2:
                    raise
                await asyncio.sleep(0.5)

    def session(self) -> AsyncSession:
        return self.sessions()

    async def get(self, invoice_id: str) -> Invoice | None:
        async with self.session() as s:
            return await s.get(Invoice, invoice_id)

    async def update(self, invoice_id: str, **values) -> None:
        async with self.session() as s:
            inv = await s.get(Invoice, invoice_id)
            for k, v in values.items():
                setattr(inv, k, v)
            await s.commit()

    async def find_duplicate(self, invoice_id: str, supplier: str, number: str) -> Invoice | None:
        """The earlier record of the same supplier invoice, if any.

        Two copies processed at the same moment both see each other, so only a copy that
        arrived *first* (received_at, then id as tie-break) counts: exactly one survives.
        """
        async with self.session() as s:
            me = await s.get(Invoice, invoice_id)
            rows = (
                await s.scalars(
                    select(Invoice).where(
                        Invoice.supplier_invoice_number == number,
                        Invoice.id != invoice_id,
                        Invoice.status.notin_(["rejected", "failed", "duplicate"]),
                    )
                )
            ).all()
        rows = [r for r in rows if (r.received_at, r.id) < (me.received_at, me.id)]
        first_word = supplier.split()[0].lower()
        return next((r for r in rows if r.supplier_name and r.supplier_name.split()[0].lower() == first_word), None)
