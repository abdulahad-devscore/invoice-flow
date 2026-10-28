"""The duplicate check is an atomic claim on (supplier, invoice number)."""

import asyncio

from invoice_flow.db import Database, Invoice


async def make_db(tmp_path) -> Database:
    db = Database(f"sqlite+aiosqlite:///{tmp_path}/claims.db")
    await db.create_all()
    async with db.session() as s:
        for i in ("a", "b", "c"):
            s.add(Invoice(id=i, sha256=i * 64, filename=f"{i}.pdf", pdf=b"%PDF"))
        await s.commit()
    return db


async def test_concurrent_copies_have_exactly_one_owner(tmp_path):
    db = await make_db(tmp_path)
    results = await asyncio.gather(*(db.claim(i, "Johor Glassworks Sdn Bhd", "JGW-1") for i in ("a", "b", "c")))
    winners = [i for i, r in zip("abc", results, strict=True) if r is None]
    assert len(winners) == 1
    assert all(r == winners[0] for r in results if r is not None)


async def test_claim_is_idempotent_for_its_owner(tmp_path):
    db = await make_db(tmp_path)
    assert await db.claim("a", "Johor Glassworks", "JGW-2") is None
    assert await db.claim("a", "Johor Glassworks", "JGW-2") is None  # an activity retry
    assert await db.claim("b", "Johor Glassworks Sdn Bhd", "JGW-2") == "a"


async def test_rejected_owner_hands_the_number_over(tmp_path):
    db = await make_db(tmp_path)
    assert await db.claim("a", "Johor Glassworks", "JGW-3") is None
    await db.update("a", status="rejected")
    assert await db.claim("b", "Johor Glassworks", "JGW-3") is None  # corrected re-send goes through
    assert await db.claim("c", "Johor Glassworks", "JGW-3") == "b"


async def test_same_number_from_another_supplier_is_not_a_duplicate(tmp_path):
    db = await make_db(tmp_path)
    assert await db.claim("a", "Johor Glassworks", "INV-100") is None
    assert await db.claim("b", "Penang Packaging", "INV-100") is None
