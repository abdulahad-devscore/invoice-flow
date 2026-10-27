"""3-way match: supplier invoice vs purchase order vs goods receipt.

Rules (SOP-WH-001 "Goods Receiving and 3-Way Match"):
* quantity: invoiced quantity may not exceed received minus already billed
* price: unit price within 2% or RM 50 per line of the PO price, whichever is lower
* tax: SST must follow the PO line tax
* total: lines + tax within RM 1.00
Pure and deterministic, so it can run inside a Temporal workflow and be unit tested exhaustively.
"""

from __future__ import annotations

import re

from invoice_flow.models import LineMatch, MatchResult, SupplierInvoice

PRICE_PCT = 0.02
PRICE_ABS = 50.0
TOTAL_TOLERANCE = 1.00


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if len(t) > 1}


def _find_po_line(line, po_lines: list[dict]) -> dict | None:
    if line.our_sku:
        for pl in po_lines:
            if pl["sku"].upper() == line.our_sku.upper():
                return pl
    # Fall back to description similarity against the PO product names.
    best, best_score = None, 0.0
    want = _tokens(line.description)
    for pl in po_lines:
        have = _tokens(pl["product"]) | _tokens(pl["sku"])
        score = len(want & have) / max(1, len(want | have))
        if score > best_score:
            best, best_score = pl, score
    return best if best_score >= 0.3 else None


def three_way_match(invoice: SupplierInvoice, po: dict | None) -> MatchResult:
    issues: list[str] = list(invoice.arithmetic_issues())
    if po is None:
        return MatchResult(
            matched=False,
            po_number=invoice.po_number,
            issues=issues + [f"purchase order {invoice.po_number or '(none quoted)'} not found"],
            lines=[],
        )
    if po.get("state") not in ("purchase", "done"):
        issues.append(f"{po['number']} is not a confirmed purchase order (state {po.get('state')})")
    if po["supplier"].split()[0].lower() not in invoice.supplier_name.lower():
        issues.append(f"invoice is from '{invoice.supplier_name}' but {po['number']} is with '{po['supplier']}'")

    matches: list[LineMatch] = []
    used: set[int] = set()
    expected_tax = 0.0
    for i, line in enumerate(invoice.lines, 1):
        pl = _find_po_line(line, [p for p in po["lines"] if p["line_id"] not in used])
        lm = LineMatch(
            invoice_line=i,
            description=line.description,
            po_line_sku=pl["sku"] if pl else None,
            quantity=line.quantity,
            unit_price=line.unit_price,
        )
        if pl is None:
            lm.issues.append("no matching line on the purchase order")
        else:
            used.add(pl["line_id"])
            billable = round(pl["qty_received"] - pl["qty_billed"], 4)
            lm.po_price, lm.billable_qty = pl["unit_price"], billable
            if line.quantity > billable + 1e-6:
                lm.issues.append(
                    f"invoiced {line.quantity:g} but only {billable:g} received and not yet billed "
                    f"(ordered {pl['qty_ordered']:g}, received {pl['qty_received']:g})"
                )
            allowed = min(pl["unit_price"] * PRICE_PCT, PRICE_ABS / max(line.quantity, 1))
            if abs(line.unit_price - pl["unit_price"]) > allowed + 1e-9:
                diff = (line.unit_price - pl["unit_price"]) / pl["unit_price"] * 100 if pl["unit_price"] else 0
                lm.issues.append(f"unit price {line.unit_price:.2f} vs PO {pl['unit_price']:.2f} ({diff:+.1f}%)")
            expected_tax += line.amount * pl["tax_percent"] / 100
        matches.append(lm)
        issues += [f"line {i} ({line.description}): {msg}" for msg in lm.issues]

    expected_tax = round(expected_tax, 2)
    if abs(invoice.tax_amount - expected_tax) > TOTAL_TOLERANCE:
        issues.append(f"tax {invoice.tax_amount:.2f} but PO tax rules give {expected_tax:.2f}")
    expected_total = round(sum(line.amount for line in invoice.lines) + expected_tax, 2)
    if abs(invoice.total - expected_total) > TOTAL_TOLERANCE:
        issues.append(f"total {invoice.total:.2f} but lines + tax = {expected_total:.2f}")
    return MatchResult(
        matched=not issues,
        po_number=po["number"],
        issues=issues,
        lines=matches,
        expected_tax=expected_tax,
        expected_total=expected_total,
    )
