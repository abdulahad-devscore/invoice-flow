"""Generate realistic supplier invoice PDFs for the seeded purchase orders and,
optionally, email them to the AP mailbox.

    python scripts/generate_invoices.py --out samples            # PDFs only
    python scripts/generate_invoices.py --out samples --send     # and email them (GreenMail in compose)

Each scenario mirrors something AP teams see every week. The PO numbers come
from the live bridge, so the invoices always match the seeded ERP.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import smtplib
import sys
from datetime import date, timedelta
from email.message import EmailMessage
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import A4
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import mm
from reportlab.platypus import Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from invoice_flow.config import get_settings  # noqa: E402
from invoice_flow.erp import BridgeClient  # noqa: E402

TODAY = date(2026, 10, 27)
SUPPLIER_INFO = {
    "Perak Alloy": ("PAS", "Lot 88, Kawasan Perindustrian Menglembu, 31450 Ipoh, Perak", "classic"),
    "Selangor Coatings": ("SCS", "No. 5, Jalan Kapar, 42100 Klang, Selangor", "modern"),
    "Klang Valley Plastics": ("KVP", "Lot 2, Jalan Industri Rawang 3, 48000 Rawang, Selangor", "classic"),
    "Johor Glassworks": ("JGW", "PLO 120, Jalan Tembaga, 81700 Pasir Gudang, Johor", "compact"),
    "Penang Packaging": ("PPK", "Plot 34, Bayan Lepas FIZ Phase 4, 11900 Penang", "modern"),
    "Butterworth Steel": ("BST", "1880 Jalan Perusahaan, 13600 Prai, Penang", "classic"),
    "Hengfa Tableware": ("HTT", "18 Huangpu East Road, Guangzhou 510700, China", "compact"),
    "Brightwave Home": ("BHA", "Bldg 6, Bantian Industrial Park, Longgang, Shenzhen, China", "modern"),
}


def supplier_meta(name: str) -> tuple[str, str, str]:
    for key, meta in SUPPLIER_INFO.items():
        if name.startswith(key):
            return meta
    return ("XXX", "Unknown address", "classic")


def build_invoice(po: dict, scenario: str, number: str, inv_date: date) -> dict:
    lines = []
    for pl in po["lines"]:
        qty = pl["qty_received"] - pl["qty_billed"]
        price = pl["unit_price"]
        if scenario == "qty_over_received":
            qty = pl["qty_ordered"]
        if scenario == "price_up_5pct":
            price = round(price * 1.05, 2)
        if qty <= 0:
            continue
        lines.append(
            {
                "sku": pl["sku"],
                "description": pl["product"].split("] ", 1)[-1],
                "qty": qty,
                "price": price,
                "amount": round(qty * price, 2),
                "tax_pct": pl["tax_percent"],
            }
        )
    subtotal = round(sum(line["amount"] for line in lines), 2)
    tax = round(sum(line["amount"] * line["tax_pct"] / 100 for line in lines), 2)
    total = round(subtotal + tax, 2)
    if scenario == "bad_total":
        total = round(total + 100.00, 2)
    return {
        "po": po["number"] if scenario != "unknown_po" else "P09999",
        "supplier": po["supplier"],
        "number": number,
        "date": inv_date,
        "due": inv_date + timedelta(days=30),
        "lines": lines,
        "subtotal": subtotal,
        "tax": tax,
        "total": total,
    }


def render(inv: dict, path: Path) -> None:
    code, address, layout = supplier_meta(inv["supplier"])
    styles = getSampleStyleSheet()
    doc = SimpleDocTemplate(str(path), pagesize=A4, leftMargin=18 * mm, rightMargin=18 * mm, topMargin=16 * mm)
    money = "{:,.2f}".format
    story = [
        Paragraph(f"<b>{inv['supplier'].upper()}</b>", styles["Title"] if layout != "compact" else styles["h2"]),
        Paragraph(address, styles["Normal"]),
        Spacer(1, 6 * mm),
    ]
    buyer = "Tembusu Housewares Sdn Bhd, Lot 12, Jalan Perindustrian 7, 40000 Shah Alam, Selangor"
    if layout == "classic":
        meta = [
            ["TAX INVOICE", ""],
            ["Invoice No:", inv["number"]],
            ["Invoice Date:", inv["date"].isoformat()],
            ["Due Date:", inv["due"].isoformat()],
            ["Customer PO:", inv["po"]],
            ["Bill To:", buyer],
        ]
        header = ["Item Code", "Description", "Qty", "Unit Price", "Amount"]
        rows = [
            [line["sku"], line["description"], money(line["qty"]), money(line["price"]), money(line["amount"])]
            for line in inv["lines"]
        ]
        totals = [["Subtotal", money(inv["subtotal"])], ["SST", money(inv["tax"])], ["Total MYR", money(inv["total"])]]
    elif layout == "modern":
        meta = [
            ["INVOICE", ""],
            ["Invoice #", inv["number"]],
            ["Date of Issue", inv["date"].isoformat()],
            ["Payment Due", inv["due"].isoformat()],
            ["Your PO Ref", inv["po"]],
            ["Customer", buyer],
        ]
        header = ["Code", "Description", "Quantity", "Rate", "Amount"]
        rows = [
            [f"{code}-{i + 101}", line["description"], money(line["qty"]), money(line["price"]), money(line["amount"])]
            for i, line in enumerate(inv["lines"])
        ]
        totals = [
            ["Sub-total", money(inv["subtotal"])],
            ["Sales Tax (SST)", money(inv["tax"])],
            ["Amount Due", money(inv["total"])],
        ]
    else:
        meta = [
            ["Commercial Invoice", ""],
            ["Invoice Number", inv["number"]],
            ["Date", inv["date"].strftime("%d %b %Y")],
            ["Purchase Order", inv["po"]],
            ["Sold To", buyer],
        ]
        header = ["Description", "Qty", "Unit Price", "Total"]
        rows = [
            [line["description"], money(line["qty"]), money(line["price"]), money(line["amount"])]
            for line in inv["lines"]
        ]
        totals = [
            ["Total before tax", money(inv["subtotal"])],
            ["Tax", money(inv["tax"])],
            ["Grand Total", money(inv["total"])],
        ]
    story.append(Table(meta, colWidths=[38 * mm, 130 * mm], hAlign="LEFT"))
    story.append(Spacer(1, 6 * mm))
    table = Table([header, *rows], repeatRows=1, hAlign="LEFT")
    table.setStyle(
        TableStyle(
            [
                ("GRID", (0, 0), (-1, -1), 0.4, colors.grey),
                ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#e8efe9")),
                ("ALIGN", (-3, 1), (-1, -1), "RIGHT"),
                ("FONTSIZE", (0, 0), (-1, -1), 9),
            ]
        )
    )
    story += [table, Spacer(1, 5 * mm)]
    # Totals as plain lines (not a table) so they read like real invoices to text extraction.
    for label, value in totals:
        story.append(Paragraph(f"{label}: RM {value}" if layout != "classic" else f"{label} {value}", styles["Normal"]))
    story += [
        Spacer(1, 8 * mm),
        Paragraph("Bank: Maybank 5140-1234-5678. Please quote the invoice number on payment.", styles["Italic"]),
    ]
    doc.build(story)


async def scenarios(bridge: BridgeClient) -> list[tuple[str, dict]]:
    """Pick POs from the live ERP and assign each a scenario."""
    pos = {p["number"]: p for p in await bridge.purchase_orders("to_bill")}
    full = [await bridge.purchase_order(n) for n in sorted(pos)]
    by_supplier = {}
    for po in full:
        by_supplier.setdefault(po["supplier"].split()[0], []).append(po)
    plan = []

    def take(prefix: str, scenario: str) -> None:
        if by_supplier.get(prefix):
            plan.append((scenario, by_supplier[prefix].pop(0)))

    take("Perak", "clean")
    take("Penang", "clean")
    take("Brightwave", "clean")
    take("Hengfa", "clean")  # partly received: invoice covers only what arrived
    take("Johor", "clean")
    take("Selangor", "price_up_5pct")
    take("Klang", "qty_over_received")
    take("Johor", "duplicate")
    if plan:
        plan.append(("unknown_po", plan[0][1]))
        plan.append(("bad_total", plan[1][1] if len(plan) > 1 else plan[0][1]))
    return plan


async def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", default="samples")
    parser.add_argument("--send", action="store_true", help="email the PDFs to the AP mailbox")
    parser.add_argument("--smtp-host", default=os.environ.get("SMTP_HOST", "localhost"))
    parser.add_argument("--smtp-port", type=int, default=int(os.environ.get("SMTP_PORT", "3025")))
    args = parser.parse_args()
    s = get_settings()
    bridge = BridgeClient(s.bridge_url, s.bridge_client_id, s.bridge_client_secret)
    out = Path(args.out)
    out.mkdir(parents=True, exist_ok=True)
    manifest = []
    for i, (scenario, po) in enumerate(await scenarios(bridge)):
        code, _, _ = supplier_meta(po["supplier"])
        number = f"{code}-INV-26{10 + i:02d}{i * 37 % 100:02d}"
        inv = build_invoice(po, "clean" if scenario == "duplicate" else scenario, number, TODAY - timedelta(days=2))
        files = [out / f"{i + 1:02d}-{scenario}-{number}.pdf"]
        render(inv, files[0])
        if scenario == "duplicate":  # same invoice re-sent a day later, scanned from the supplier's copy
            files.append(out / f"{i + 1:02d}b-{scenario}-resent-{number}.pdf")
            render(inv, files[1])
        manifest.append(
            {
                "scenario": scenario,
                "po": inv["po"],
                "supplier": po["supplier"],
                "number": number,
                "total": inv["total"],
                "files": [f.name for f in files],
            }
        )
        if args.send:
            for f in files:
                send(args.smtp_host, args.smtp_port, s.imap_user, po["supplier"], number, f)
    (out / "manifest.json").write_text(json.dumps(manifest, indent=2))
    print(json.dumps(manifest, indent=2))


def send(host: str, port: int, to: str, supplier: str, number: str, pdf: Path) -> None:
    msg = EmailMessage()
    code = supplier_meta(supplier)[0].lower()
    msg["From"] = f"Accounts <accounts@{code}.example>"
    msg["To"] = to
    msg["Subject"] = f"Invoice {number} from {supplier}"
    msg.set_content(f"Dear AP team,\n\nPlease find attached invoice {number}.\n\nRegards,\n{supplier}")
    msg.add_attachment(pdf.read_bytes(), maintype="application", subtype="pdf", filename=pdf.name)
    with smtplib.SMTP(host, port) as smtp:
        smtp.send_message(msg)


if __name__ == "__main__":
    asyncio.run(main())
