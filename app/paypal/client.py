"""Async client for the parts of the PayPal REST API that move money.

Deliberately thin and typed. The agents reach PayPal through the Agent Toolkit /
MCP tools instead; this client is for the calls a human (or PolicyGuard) has
already approved: create, send, update, record payment.
"""

from __future__ import annotations

import asyncio
import base64
import json
import re
import time
import uuid
from collections.abc import Mapping
from datetime import date
from decimal import Decimal
from typing import Any

import httpx

from app.money import fmt

# Webhook headers PayPal signs a delivery with, in the order verify-webhook-signature names them.
_SIGNATURE_HEADERS = {
    "auth_algo": "paypal-auth-algo",
    "cert_url": "paypal-cert-url",
    "transmission_id": "paypal-transmission-id",
    "transmission_sig": "paypal-transmission-sig",
    "transmission_time": "paypal-transmission-time",
}

_BASE64_LINE = re.compile(r"[A-Za-z0-9+/]+={0,2}")
_PNG_MAGIC = b"\x89PNG\r\n\x1a\n"


class PayPalError(Exception):
    """A non-2xx PayPal response, carrying enough to explain it to a tradesperson."""

    def __init__(self, status: int, payload: Mapping[str, Any] | None, debug_id: str = "") -> None:
        self.status = status
        self.payload = dict(payload or {})
        self.name = str(self.payload.get("name") or self.payload.get("error") or "")
        self.details = list(self.payload.get("details") or [])
        self.debug_id = debug_id or str(self.payload.get("debug_id") or "")
        message = (
            self.payload.get("message") or self.payload.get("error_description") or "PayPal error"
        )
        super().__init__(f"PayPal {status} {self.name}: {message}")

    def human(self) -> str:
        """One plain-English sentence for the UI. The full payload goes to the logs."""
        if self.status == 401:
            return "PayPal didn't accept our credentials. Check the client ID and secret."
        issues = {str(d.get("issue", "")) for d in self.details}
        if "USER_NOT_FOUND" in issues or "INVALID_INVOICER_EMAIL" in issues:
            return "The invoicer email must be the email of the PayPal business account."
        if "DUPLICATE_INVOICE_NUMBER" in issues:
            return "That invoice number has been used already."
        if self.details:
            first = self.details[0]
            text = first.get("description") or first.get("issue") or ""
            if text:
                return f"PayPal said: {text}"
        return "PayPal couldn't complete that just now. Please try again."


class PayPalClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        base_url: str,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
        timeout: float = 20.0,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._http = httpx.AsyncClient(base_url=base_url, transport=transport, timeout=timeout)
        self._token = ""
        self._token_expires_at = 0.0
        self._token_lock = asyncio.Lock()

    async def aclose(self) -> None:
        await self._http.aclose()

    # ── auth + transport ────────────────────────────────────────────────────

    async def _access_token(self, *, force: bool = False) -> str:
        async with self._token_lock:
            if not force and self._token and time.monotonic() < self._token_expires_at:
                return self._token
            resp = await self._http.post(
                "/v1/oauth2/token",
                data={"grant_type": "client_credentials"},
                auth=(self._client_id, self._client_secret),
            )
            if resp.status_code != 200:
                raise PayPalError(resp.status_code, _json_or_none(resp))
            payload = resp.json()
            self._token = payload["access_token"]
            # Refresh a minute early so a token never expires mid-request.
            self._token_expires_at = time.monotonic() + int(payload.get("expires_in", 300)) - 60
            return self._token

    async def _request(
        self,
        method: str,
        path: str,
        *,
        json_body: Any = None,
        content: bytes | None = None,
        params: Mapping[str, Any] | None = None,
        request_id: str | None = None,
        representation: bool = False,
    ) -> httpx.Response:
        headers = {"Content-Type": "application/json"}
        if request_id:
            # Idempotency: a retried POST with the same id is not applied twice.
            headers["PayPal-Request-Id"] = request_id
        if representation:
            headers["Prefer"] = "return=representation"

        for attempt in (1, 2):
            headers["Authorization"] = f"Bearer {await self._access_token(force=attempt == 2)}"
            resp = await self._http.request(
                method, path, json=json_body, content=content, params=params, headers=headers
            )
            # A token revoked server-side before its expiry: refresh once and retry.
            if resp.status_code == 401 and attempt == 1:
                continue
            break
        if resp.status_code >= 400:
            raise PayPalError(
                resp.status_code, _json_or_none(resp), resp.headers.get("paypal-debug-id", "")
            )
        return resp

    async def request_json(
        self, method: str, path: str, *, json_body: Any = None
    ) -> dict[str, Any]:
        """Any other REST call (webhook admin in scripts). Empty bodies come back as {}."""
        resp = await self._request(method, path, json_body=json_body)
        return _json_or_none(resp) or {}

    async def access_token(self) -> str:
        """A live bearer token, for handing to PayPal's MCP server / Agent Toolkit."""
        return await self._access_token()

    # ── invoicing ───────────────────────────────────────────────────────────

    async def next_invoice_number(self) -> str:
        resp = await self._request("POST", "/v2/invoicing/generate-next-invoice-number")
        return str(resp.json()["invoice_number"])

    async def create_draft_invoice(
        self, body: Mapping[str, Any], *, request_id: str | None = None
    ) -> dict[str, Any]:
        resp = await self._request(
            "POST",
            "/v2/invoicing/invoices",
            json_body=body,
            request_id=request_id or str(uuid.uuid4()),
            representation=True,
        )
        return dict(resp.json())

    async def send_invoice(
        self,
        invoice_id: str,
        *,
        subject: str = "",
        note: str = "",
        request_id: str | None = None,
    ) -> str:
        """Email the invoice to the recipient. Returns the payer-view (pay) link."""
        body: dict[str, Any] = {"send_to_invoicer": False, "send_to_recipient": True}
        if subject:
            body["subject"] = subject
        if note:
            body["note"] = note
        resp = await self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            json_body=body,
            request_id=request_id or str(uuid.uuid4()),
        )
        payload = _json_or_none(resp) or {}
        return str(payload.get("href", ""))

    async def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        resp = await self._request("GET", f"/v2/invoicing/invoices/{invoice_id}")
        return dict(resp.json())

    async def update_invoice(
        self, invoice_id: str, body: Mapping[str, Any], *, notify_recipient: bool = True
    ) -> dict[str, Any]:
        """Full replace (PUT). PayPal re-sends the invoice email when `notify_recipient`."""
        resp = await self._request(
            "PUT",
            f"/v2/invoicing/invoices/{invoice_id}",
            json_body=body,
            params={
                "send_to_recipient": str(notify_recipient).lower(),
                "send_to_invoicer": "false",
            },
            representation=True,
        )
        return dict(resp.json())

    async def remind(self, invoice_id: str, *, subject: str = "", note: str = "") -> None:
        body: dict[str, Any] = {"send_to_invoicer": False, "send_to_recipient": True}
        if subject:
            body["subject"] = subject
        if note:
            body["note"] = note
        await self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/remind", json_body=body)

    async def cancel(self, invoice_id: str, *, subject: str = "", note: str = "") -> None:
        body: dict[str, Any] = {"send_to_invoicer": False, "send_to_recipient": True}
        if subject:
            body["subject"] = subject
        if note:
            body["note"] = note
        await self._request("POST", f"/v2/invoicing/invoices/{invoice_id}/cancel", json_body=body)

    async def record_payment(
        self,
        invoice_id: str,
        *,
        amount: Decimal,
        currency: str,
        method: str = "CASH",
        paid_on: date | None = None,
        note: str = "",
    ) -> str:
        """Record money received outside PayPal (cash, bank transfer). Returns the payment id."""
        body: dict[str, Any] = {
            "method": method,
            "payment_date": (paid_on or date.today()).isoformat(),
            "amount": {"currency_code": currency, "value": fmt(amount, currency)},
        }
        if note:
            body["note"] = note
        resp = await self._request(
            "POST", f"/v2/invoicing/invoices/{invoice_id}/payments", json_body=body
        )
        return str((_json_or_none(resp) or {}).get("payment_id", ""))

    async def generate_qr_code(self, invoice_id: str, *, size: int = 500) -> bytes:
        """PNG bytes of the "scan to pay" QR code for a sent invoice."""
        resp = await self._request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/generate-qr-code",
            json_body={"width": size, "height": size, "action": "pay"},
        )
        return decode_qr_png(resp.content)

    # ── webhooks ────────────────────────────────────────────────────────────

    async def verify_webhook(
        self, headers: Mapping[str, str], raw_body: bytes, webhook_id: str
    ) -> bool:
        lowered = {k.lower(): v for k, v in headers.items()}
        fields = {key: lowered.get(header, "") for key, header in _SIGNATURE_HEADERS.items()}
        if not webhook_id or not all(fields.values()):
            return False
        # The event must reach PayPal byte-for-byte as it was signed. Parsing and
        # re-serialising it can reorder keys or change number formatting and make
        # a genuine delivery fail verification, so splice the raw body in.
        prefix = json.dumps({**fields, "webhook_id": webhook_id})[:-1]
        content = f'{prefix}, "webhook_event": '.encode() + raw_body + b"}"
        resp = await self._request(
            "POST", "/v1/notifications/verify-webhook-signature", content=content
        )
        return (_json_or_none(resp) or {}).get("verification_status") == "SUCCESS"


def decode_qr_png(raw: bytes) -> bytes:
    """Pull the PNG out of PayPal's QR response.

    The endpoint returns the image base64-encoded, sometimes wrapped in a
    multipart envelope, so take the longest base64 run rather than trusting the
    framing.
    """
    if raw.startswith(_PNG_MAGIC):
        return raw
    # Group consecutive all-base64 lines (the payload may be line-wrapped) and
    # keep the longest group; header lines like "Content-Type: image/png" break a group.
    runs: list[str] = []
    current = ""
    for line in raw.decode("ascii", errors="ignore").splitlines():
        line = line.strip()
        if line and _BASE64_LINE.fullmatch(line):
            current += line
        else:
            runs.append(current)
            current = ""
    runs.append(current)
    payload = max(runs, key=len)
    if len(payload) < 100:
        raise ValueError("no base64 image in QR code response")
    png = base64.b64decode(payload)
    if not png.startswith(_PNG_MAGIC):
        raise ValueError("QR code response is not a PNG")
    return png


def _json_or_none(resp: httpx.Response) -> dict[str, Any] | None:
    try:
        data = resp.json()
    except ValueError:
        return None
    return data if isinstance(data, dict) else None
