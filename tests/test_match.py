"""3-way match rules from SOP-WH-001, exhaustively."""

from datetime import date

from invoice_flow.match import three_way_match
from invoice_flow.models import InvoiceLine, SupplierInvoice

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
        },
        {
            "line_id": 2,
            "sku": "RM-HDL-BAK",
            "product": "[RM-HDL-BAK] Bakelite Handle",
            "qty_ordered": 6000,
            "qty_received": 4800,
            "qty_billed": 0,
            "unit_price": 2.10,
            "tax_percent": 0,
        },
    ],
}
TRADED = {
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
        }
    ],
}


def invoice(lines, tax=0.0, total=None, supplier="Klang Valley Plastics Sdn Bhd", po="P00009") -> SupplierInvoice:
    subtotal = round(sum(q * p for _, _, q, p in lines), 2)
    return SupplierInvoice(
        supplier_name=supplier,
        supplier_invoice_number="KVP-INV-1",
        invoice_date=date(2026, 10, 25),
        po_number=po,
        lines=[
            InvoiceLine(description=d, our_sku=s, quantity=q, unit_price=p, amount=round(q * p, 2))
            for d, s, q, p in lines
        ],
        subtotal=subtotal,
        tax_amount=tax,
        total=total if total is not None else round(subtotal + tax, 2),
    )


def test_clean_partial_bill_matches():
    r = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 800, 6.20), ("Handle", "RM-HDL-BAK", 4800, 2.10)]), PO)
    assert r.matched, r.issues


def test_billing_more_than_received_is_flagged():
    r = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 1000, 6.20)]), PO)
    assert not r.matched
    assert "only 800 received" in r.issues[0]


def test_already_billed_quantity_is_not_billable_again():
    po = {**PO, "lines": [{**PO["lines"][0], "qty_billed": 800}]}
    r = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 100, 6.20)]), po)
    assert "only 0 received and not yet billed" in r.issues[0]


def test_price_within_two_percent_passes_and_above_fails():
    ok = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 100, 6.30)]), PO)  # +1.6%, RM 10 on the line
    assert ok.matched, ok.issues
    capped = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 800, 6.30)]), PO)  # +1.6% but RM 80 > RM 50
    assert not capped.matched
    bad = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 800, 6.51)]), PO)  # +5%
    assert not bad.matched
    assert "+5.0%" in bad.issues[0]


def test_rm50_cap_is_tighter_on_big_lines():
    # +1% is inside 2%, but 0.062 x 5,000 = RM 310 over on the line, above the RM 50 cap.
    po = {**PO, "lines": [{**PO["lines"][0], "qty_ordered": 5000, "qty_received": 5000}]}
    r = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 5000, 6.262)]), po)
    assert not r.matched


def test_description_matching_without_sku():
    r = three_way_match(invoice([("Polypropylene Resin food grade", None, 800, 6.20)]), PO)
    assert r.matched, r.issues
    assert r.lines[0].po_line_sku == "RM-PP-RESIN"


def test_unknown_item():
    r = three_way_match(invoice([("Stainless steel rivets", None, 100, 0.5)]), PO)
    assert "no matching line" in r.issues[0]


def test_missing_po():
    r = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 800, 6.20)], po="P09999"), None)
    assert not r.matched
    assert "P09999 not found" in r.issues[-1]
    assert r.po_number is None  # the review page hides "approve" when there is nothing to bill against


def test_wrong_supplier():
    r = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 800, 6.20)], supplier="Penang Packaging Sdn Bhd"), PO)
    assert any("is with 'Klang Valley Plastics" in i for i in r.issues)


def test_sst_on_traded_goods():
    lines = [("Air Fryer 4.5L", "TH-SA-6104", 300, 171.0)]
    assert three_way_match(
        invoice(lines, tax=5130.0, supplier="Brightwave Home Appliances", po="P00013"), TRADED
    ).matched
    no_tax = three_way_match(invoice(lines, tax=0.0, supplier="Brightwave Home Appliances", po="P00013"), TRADED)
    assert any("PO tax rules give 5130.00" in i for i in no_tax.issues)


def test_arithmetic_error_on_invoice():
    r = three_way_match(invoice([("PP resin", "RM-PP-RESIN", 800, 6.20)], total=5060.00), PO)
    assert not r.matched
    assert any("!= total" in i for i in r.issues)
