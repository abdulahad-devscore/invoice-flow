"""Talks to the ERP through the ERP MCP Bridge (Project 1), as the `invoice-flow`
service account: it may read purchase orders and propose vendor bills. The
bridge auto-approves vendor bills for this account by policy, because the
human control (the AP review queue) lives here."""

from __future__ import annotations

import json
import time

import httpx
import httpx2
from mcp import Client
from mcp.client.streamable_http import streamable_http_client


class BridgeError(Exception):
    """The bridge answered and refused (validation, permission). Not retryable."""


class BridgeUnavailable(Exception):
    """The bridge could not be reached. Retryable."""


def _leaf(exc: BaseException) -> BaseException:
    while isinstance(exc, BaseExceptionGroup):
        exc = exc.exceptions[0]
    return exc


class BridgeClient:
    def __init__(self, url: str, client_id: str, client_secret: str):
        self.url = url.rstrip("/")
        self.client_id = client_id
        self.client_secret = client_secret
        self._token: str | None = None
        self._expires = 0.0

    async def _bearer(self) -> str:
        if not self._token or time.time() > self._expires - 60:
            async with httpx.AsyncClient(timeout=10, trust_env=False) as http:
                resp = await http.post(
                    f"{self.url}/oauth/token",
                    data={
                        "grant_type": "client_credentials",
                        "client_id": self.client_id,
                        "client_secret": self.client_secret,
                    },
                )
                resp.raise_for_status()
                body = resp.json()
            self._token, self._expires = body["access_token"], time.time() + body["expires_in"]
        return self._token

    async def call(self, tool: str, args: dict):
        http = httpx2.AsyncClient(
            headers={"Authorization": f"Bearer {await self._bearer()}"}, trust_env=False, timeout=httpx2.Timeout(60.0)
        )
        try:
            async with Client(streamable_http_client(f"{self.url}/mcp", http_client=http)) as client:
                result = await client.call_tool(tool, args)
        except* (httpx.HTTPError, httpx2.HTTPError, OSError) as group:
            # The MCP client runs in a task group; surface the real cause, not the group.
            first = _leaf(group)
            raise BridgeUnavailable(f"ERP bridge unreachable: {type(first).__name__}: {first}") from first
        if result.is_error:
            raise BridgeError(" ".join(getattr(c, "text", "") for c in result.content))
        data = result.structured_content
        return data["result"] if isinstance(data, dict) and set(data) == {"result"} else data

    async def purchase_order(self, po_number: str) -> dict | None:
        try:
            return await self.call("get_purchase_order", {"po_number": po_number})
        except BridgeError as exc:
            if "No purchase order" in str(exc):
                return None
            raise

    async def purchase_orders(self, status: str) -> list[dict]:
        return await self.call("list_purchase_orders", {"status": status, "limit": 50})

    async def propose_bill(self, invoice: dict, lines: list[dict], evidence: dict) -> dict:
        return await self.call(
            "propose_vendor_bill",
            {
                "po_number": invoice["po_number"],
                "supplier_invoice_ref": invoice["supplier_invoice_number"],
                "invoice_date": invoice["invoice_date"],
                "lines": lines,
                "amount_untaxed": invoice["subtotal"],
                "amount_tax": invoice["tax_amount"],
                "amount_total": invoice["total"],
                "match_evidence": json.loads(json.dumps(evidence, default=str)),
            },
        )
