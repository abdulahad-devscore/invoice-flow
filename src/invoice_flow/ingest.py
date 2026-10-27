"""Getting PDFs in: IMAP mailbox polling and direct upload share one path.

A PDF is stored once (keyed by its SHA-256) and gets exactly one workflow
(workflow id derived from the hash), so a re-delivered email or a double
upload never processes the same file twice.
"""

from __future__ import annotations

import asyncio
import email
import hashlib
import imaplib
import logging
from email.message import Message

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from temporalio.client import Client
from temporalio.common import WorkflowIDReusePolicy
from temporalio.exceptions import WorkflowAlreadyStartedError

from invoice_flow.config import Settings
from invoice_flow.db import Database, Invoice
from invoice_flow.models import WorkflowInput
from invoice_flow.workflow import InvoiceWorkflow

log = logging.getLogger(__name__)


def workflow_id(sha256: str) -> str:
    return f"invoice-{sha256[:24]}"


async def submit(
    db: Database,
    temporal: Client,
    settings: Settings,
    pdf: bytes,
    filename: str,
    *,
    source: str = "email",
    message_id: str | None = None,
    sender: str | None = None,
) -> tuple[str, bool]:
    """Store the PDF and start its workflow. Returns (invoice id, created?)."""
    sha = hashlib.sha256(pdf).hexdigest()
    async with db.session() as s:
        existing = await s.scalar(select(Invoice).where(Invoice.sha256 == sha))
        if existing:
            return existing.id, False
        inv = Invoice(
            sha256=sha,
            filename=filename,
            pdf=pdf,
            source=source,
            message_id=message_id,
            sender=sender,
            workflow_id=workflow_id(sha),
        )
        s.add(inv)
        try:
            await s.commit()
        except IntegrityError:  # raced with another poller
            await s.rollback()
            return (await s.scalar(select(Invoice.id).where(Invoice.sha256 == sha))), False
    await start(temporal, settings, inv.id, inv.workflow_id)
    return inv.id, True


async def start(temporal: Client, settings: Settings, invoice_id: str, wf_id: str, *, rerun: bool = False) -> None:
    """Start the invoice's workflow. New invoices get one run per id; `rerun` (DLQ replay)
    starts a fresh run on the same id once the previous run has closed, whatever its outcome."""
    policy = WorkflowIDReusePolicy.ALLOW_DUPLICATE if rerun else WorkflowIDReusePolicy.REJECT_DUPLICATE
    try:
        await temporal.start_workflow(
            InvoiceWorkflow.run,
            WorkflowInput(invoice_id=invoice_id, review_timeout_hours=settings.human_review_timeout_hours),
            id=wf_id,
            task_queue=settings.task_queue,
            id_reuse_policy=policy,
        )
    except WorkflowAlreadyStartedError:
        if rerun:
            raise
        log.info("workflow %s already started", wf_id)


def _pdf_parts(msg: Message) -> list[tuple[str, bytes]]:
    out = []
    for part in msg.walk():
        filename = part.get_filename() or ""
        if part.get_content_type() == "application/pdf" or filename.lower().endswith(".pdf"):
            payload = part.get_payload(decode=True)
            if payload:
                out.append((filename or "invoice.pdf", payload))
    return out


def _connect(settings: Settings) -> imaplib.IMAP4:
    cls = imaplib.IMAP4_SSL if settings.imap_ssl else imaplib.IMAP4
    imap = cls(settings.imap_host, settings.imap_port)
    imap.login(settings.imap_user, settings.imap_password)
    imap.select(settings.imap_folder)
    return imap


def _close(imap: imaplib.IMAP4) -> None:
    try:
        imap.logout()
    except (OSError, imaplib.IMAP4.error):
        pass  # some servers drop the socket on LOGOUT; nothing is lost at this point


def fetch_unseen(settings: Settings) -> list[tuple[bytes, str, str, list[tuple[str, bytes]]]]:
    """Blocking IMAP fetch of unseen messages *without* marking them seen (BODY.PEEK).

    Returns (uid, message-id, sender, [(filename, pdf)]). Messages are only flagged seen by
    `mark_seen` after their PDFs are stored and queued, so a crash never loses an email.
    """
    imap = _connect(settings)
    try:
        _, data = imap.uid("search", None, "UNSEEN")
        messages = []
        for uid in data[0].split():
            _, parts = imap.uid("fetch", uid, "(BODY.PEEK[])")
            msg = email.message_from_bytes(parts[0][1])
            messages.append((uid, msg.get("Message-ID", ""), msg.get("From", ""), _pdf_parts(msg)))
        return messages
    finally:
        _close(imap)


def mark_seen(settings: Settings, uids: list[bytes]) -> None:
    if not uids:
        return
    imap = _connect(settings)
    try:
        imap.uid("store", b",".join(uids), "+FLAGS", "(\\Seen)")
    finally:
        _close(imap)


async def poll_forever(db: Database, temporal: Client, settings: Settings) -> None:
    while True:
        try:
            for uid, message_id, sender, pdfs in await asyncio.to_thread(fetch_unseen, settings):
                if not pdfs:
                    log.info("email %s from %s has no PDF attachment; skipped", message_id, sender)
                for filename, pdf in pdfs:
                    invoice_id, created = await submit(
                        db, temporal, settings, pdf, filename, message_id=message_id, sender=sender
                    )
                    log.info("%s %s -> invoice %s", "queued" if created else "already have", filename, invoice_id)
                # Only now, with every PDF stored and its workflow started, is the email done.
                await asyncio.to_thread(mark_seen, settings, [uid])
        except (OSError, imaplib.IMAP4.error) as exc:
            log.warning("mailbox poll failed: %s", exc)
        await asyncio.sleep(settings.poll_seconds)
