"""Typed shapes for the invoice pipeline.

`SupplierInvoice` is what extraction must produce. It is strict about types
and shape, so a model that hallucinates a field or returns a string where a
number belongs is rejected before anything reaches matching. Arithmetic
consistency is checked separately (`arithmetic_issues`), because an invoice
whose numbers don't add up is a business exception for a person to look at,
not an extraction bug.
"""

from __future__ import annotations

from datetime import date
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid")


class InvoiceLine(Strict):
    description: str = Field(min_length=1)
    supplier_item_code: str | None = Field(default=None, description="Code printed on the invoice line, if any")
    our_sku: str | None = Field(default=None, description="Buyer SKU like TH-CW-2401 or RM-AL-INGOT, if printed")
    quantity: float = Field(gt=0)
    unit_price: float = Field(ge=0)
    amount: float = Field(ge=0, description="Line total before tax")


class SupplierInvoice(Strict):
    supplier_name: str = Field(min_length=2)
    supplier_invoice_number: str = Field(min_length=1)
    invoice_date: date
    due_date: date | None = None
    po_number: str | None = Field(default=None, description="The buyer's purchase order number, e.g. P00012")
    currency: str = "MYR"
    lines: list[InvoiceLine] = Field(min_length=1, max_length=200)
    subtotal: float = Field(ge=0)
    tax_amount: float = Field(ge=0)
    total: float = Field(ge=0)

    def arithmetic_issues(self, tolerance: float = 0.05) -> list[str]:
        issues = []
        for i, line in enumerate(self.lines, 1):
            if abs(line.quantity * line.unit_price - line.amount) > max(tolerance, line.amount * 0.0005):
                issues.append(f"line {i}: {line.quantity:g} x {line.unit_price:.2f} != {line.amount:.2f}")
        lines_sum = sum(line.amount for line in self.lines)
        if abs(lines_sum - self.subtotal) > tolerance * len(self.lines):
            issues.append(f"lines sum to {lines_sum:.2f} but subtotal says {self.subtotal:.2f}")
        if abs(self.subtotal + self.tax_amount - self.total) > tolerance:
            issues.append(f"subtotal {self.subtotal:.2f} + tax {self.tax_amount:.2f} != total {self.total:.2f}")
        return issues


class Status(StrEnum):
    RECEIVED = "received"
    EXTRACTING = "extracting"
    MATCHING = "matching"
    WAITING_FOR_HUMAN = "waiting_for_human"
    POSTING = "posting"
    POSTED = "posted"
    REJECTED = "rejected"
    DUPLICATE = "duplicate"
    FAILED = "failed"


class LineMatch(BaseModel):
    invoice_line: int
    description: str
    po_line_sku: str | None
    quantity: float
    unit_price: float
    po_price: float | None = None
    billable_qty: float | None = None
    issues: list[str] = []


class MatchResult(BaseModel):
    matched: bool
    po_number: str | None
    issues: list[str]
    lines: list[LineMatch]
    expected_tax: float | None = None
    expected_total: float | None = None


class Decision(BaseModel):
    approve: bool
    reviewer: str
    note: str | None = None


class WorkflowInput(BaseModel):
    invoice_id: str
    review_timeout_hours: int = 72


class WorkflowOutcome(BaseModel):
    invoice_id: str
    status: Status
    bill_ref: str | None = None
    reason: str | None = None
