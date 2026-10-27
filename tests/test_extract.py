"""Extraction from real PDFs in all three supplier layouts, and the cheap->strong model fallback."""

import sys
from datetime import date
from pathlib import Path
from types import SimpleNamespace

import pytest

from invoice_flow.extract import ExtractionFailed, LLMExtractor, RulesExtractor, pdf_text
from invoice_flow.models import InvoiceLine, SupplierInvoice

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from generate_invoices import build_invoice, render  # noqa: E402

PO = {
    "number": "P00013",
    "supplier": "Brightwave Home Appliances Co., Ltd.",
    "state": "purchase",
    "lines": [
        {
            "line_id": 9,
            "sku": "TH-SA-6104",
            "product": "[TH-SA-6104] Air Fryer 4.5L",
            "qty_ordered": 300,
            "qty_received": 300,
            "qty_billed": 0,
            "unit_price": 171.0,
            "tax_percent": 10,
        },
        {
            "line_id": 10,
            "sku": "TH-SA-6103",
            "product": "[TH-SA-6103] Hand Blender 300W",
            "qty_ordered": 120,
            "qty_received": 120,
            "qty_billed": 0,
            "unit_price": 62.0,
            "tax_percent": 10,
        },
    ],
}


@pytest.mark.parametrize(
    "supplier,layout",
    [
        ("Perak Alloy Supplies Sdn Bhd", "classic"),
        ("Brightwave Home Appliances Co., Ltd.", "modern"),
        ("Johor Glassworks Sdn Bhd", "compact"),
    ],
)
async def test_rules_extractor_reads_every_layout(tmp_path, supplier, layout):
    po = {**PO, "supplier": supplier}
    inv = build_invoice(po, "clean", "XYZ-INV-261037", date(2026, 10, 25))
    path = tmp_path / f"{layout}.pdf"
    render(inv, path)
    pdf = path.read_bytes()
    text = pdf_text(pdf)
    assert text.method == "pdfplumber"
    result = await RulesExtractor().extract(text, pdf)
    got = result.invoice
    assert got.supplier_invoice_number == "XYZ-INV-261037"
    assert got.po_number == "P00013"
    assert got.invoice_date == date(2026, 10, 25)
    assert [(line.quantity, line.unit_price) for line in got.lines] == [(300, 171.0), (120, 62.0)]
    assert got.subtotal == 58740.0
    assert got.tax_amount == 5874.0
    assert got.total == 64614.0
    assert got.arithmetic_issues() == []
    if layout == "classic":
        assert got.lines[0].our_sku == "TH-SA-6104"


def _invoice(total: float) -> SupplierInvoice:
    return SupplierInvoice(
        supplier_name="Brightwave",
        supplier_invoice_number="BHA-1",
        invoice_date=date(2026, 10, 25),
        po_number="P00013",
        lines=[InvoiceLine(description="Air Fryer", quantity=2, unit_price=10, amount=20)],
        subtotal=20,
        tax_amount=2,
        total=total,
    )


class FakeMessages:
    def __init__(self, outputs):
        self.outputs = list(outputs)
        self.models = []

    async def parse(self, model, **kwargs):
        self.models.append(model)
        out = self.outputs.pop(0)
        return SimpleNamespace(
            parsed_output=out, stop_reason="end_turn", usage=SimpleNamespace(input_tokens=1000, output_tokens=200)
        )


def extractor(outputs) -> LLMExtractor:
    ex = LLMExtractor.__new__(LLMExtractor)
    ex.model, ex.fallback = "claude-haiku-5-5", "claude-opus-5-5"
    ex.client = SimpleNamespace(messages=FakeMessages(outputs))
    return ex


TEXT = SimpleNamespace(text="invoice text", method="pdfplumber", pages=1)


async def test_cheap_model_is_enough_when_output_adds_up():
    ex = extractor([_invoice(22)])
    result = await ex.extract(TEXT, b"")
    assert result.model == "claude-haiku-5-5"
    assert ex.client.messages.models == ["claude-haiku-5-5"]


async def test_strong_model_retries_when_numbers_dont_add_up():
    ex = extractor([_invoice(99), _invoice(22)])
    result = await ex.extract(TEXT, b"")
    assert result.model == "claude-opus-5-5"
    assert result.attempts == 2
    assert result.cost_usd > 0


async def test_invoice_that_really_doesnt_add_up_is_kept_for_review():
    ex = extractor([_invoice(99), _invoice(99)])
    result = await ex.extract(TEXT, b"")
    assert result.invoice.arithmetic_issues()  # goes to matching, which sends it to a person


async def test_no_structured_output_from_either_model_fails():
    with pytest.raises(ExtractionFailed):
        await extractor([None, None]).extract(TEXT, b"")
