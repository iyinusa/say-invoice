from __future__ import annotations

import base64
import json
from collections.abc import Callable
from decimal import Decimal

import httpx
import pytest

from app.paypal.client import PayPalClient, PayPalError, decode_qr_png

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 300

Handler = Callable[[httpx.Request], httpx.Response]


def _client(handler: Handler) -> PayPalClient:
    def with_token(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        return handler(request)

    transport = httpx.MockTransport(with_token)
    return PayPalClient("id", "secret", "https://pp.test", transport=transport)


async def test_token_is_cached_across_calls() -> None:
    token_calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal token_calls
        if request.url.path == "/v1/oauth2/token":
            token_calls += 1
            return httpx.Response(200, json={"access_token": "tok", "expires_in": 3600})
        assert request.headers["Authorization"] == "Bearer tok"
        return httpx.Response(200, json={"id": "INV2-1", "status": "SENT"})

    pp = PayPalClient("id", "secret", "https://pp.test", transport=httpx.MockTransport(handler))
    await pp.get_invoice("INV2-1")
    await pp.get_invoice("INV2-1")
    assert token_calls == 1


async def test_create_sends_idempotency_and_representation_headers() -> None:
    seen: dict[str, str] = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(request.headers)
        return httpx.Response(201, json={"id": "INV2-1", "status": "DRAFT"})

    pp = _client(handler)
    created = await pp.create_draft_invoice({"detail": {}}, request_id="req-1")
    assert created["id"] == "INV2-1"
    assert seen["paypal-request-id"] == "req-1"
    assert seen["prefer"] == "return=representation"


async def test_send_returns_pay_link() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert request.url.path == "/v2/invoicing/invoices/INV2-1/send"
        assert json.loads(request.content)["send_to_recipient"] is True
        return httpx.Response(200, json={"href": "https://pay.test/inv"})

    assert await _client(handler).send_invoice("INV2-1") == "https://pay.test/inv"


async def test_refreshes_token_once_on_401() -> None:
    tokens = iter(["old", "new"])
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v1/oauth2/token":
            return httpx.Response(200, json={"access_token": next(tokens), "expires_in": 3600})
        calls.append(request.headers["Authorization"])
        if request.headers["Authorization"] == "Bearer old":
            return httpx.Response(401, json={"name": "AUTHENTICATION_FAILURE"})
        return httpx.Response(200, json={"id": "INV2-1"})

    pp = PayPalClient("id", "secret", "https://pp.test", transport=httpx.MockTransport(handler))
    await pp.get_invoice("INV2-1")
    assert calls == ["Bearer old", "Bearer new"]


async def test_error_is_explained_in_plain_english() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            422,
            json={
                "name": "UNPROCESSABLE_ENTITY",
                "message": "The requested action could not be performed.",
                "details": [{"issue": "DUPLICATE_INVOICE_NUMBER"}],
            },
            headers={"paypal-debug-id": "abc123"},
        )

    with pytest.raises(PayPalError) as err:
        await _client(handler).create_draft_invoice({})
    assert err.value.status == 422
    assert err.value.debug_id == "abc123"
    assert err.value.human() == "That invoice number has been used already."


async def test_record_payment_formats_amount() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["method"] == "CASH"
        assert body["amount"] == {"currency_code": "GBP", "value": "145.00"}
        return httpx.Response(200, json={"payment_id": "PAY-1"})

    payment_id = await _client(handler).record_payment(
        "INV2-1", amount=Decimal("145"), currency="GBP"
    )
    assert payment_id == "PAY-1"


async def test_qr_code_from_multipart_base64() -> None:
    encoded = base64.encodebytes(PNG).decode()  # line-wrapped at 76 chars
    multipart = f"--b\r\nContent-Type: image/png\r\n\r\n{encoded}\r\n--b--".encode()

    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, content=multipart)

    assert await _client(handler).generate_qr_code("INV2-1") == PNG


def test_decode_qr_accepts_raw_png_and_rejects_garbage() -> None:
    assert decode_qr_png(PNG) == PNG
    with pytest.raises(ValueError):
        decode_qr_png(b"not an image")


async def test_verify_webhook_splices_raw_event_untouched() -> None:
    # Key order and number formatting a re-serialise would change.
    raw_event = b'{"z": 1, "id": "WH-1", "amount": 10.50}'
    captured: list[bytes] = []

    def handler(request: httpx.Request) -> httpx.Response:
        captured.append(request.content)
        return httpx.Response(200, json={"verification_status": "SUCCESS"})

    headers = {
        "PAYPAL-AUTH-ALGO": "SHA256withRSA",
        "PAYPAL-CERT-URL": "https://api.paypal.com/cert",
        "PAYPAL-TRANSMISSION-ID": "t-1",
        "PAYPAL-TRANSMISSION-SIG": "sig",
        "PAYPAL-TRANSMISSION-TIME": "2026-10-25T10:00:00Z",
    }
    assert await _client(handler).verify_webhook(headers, raw_event, "WH-ID")
    sent = captured[0]
    assert sent.endswith(b'"webhook_event": ' + raw_event + b"}")
    parsed = json.loads(sent)
    assert parsed["webhook_id"] == "WH-ID"
    assert parsed["transmission_id"] == "t-1"


async def test_verify_webhook_rejects_missing_headers_without_calling_paypal() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        raise AssertionError("must not call PayPal")

    assert not await _client(handler).verify_webhook({}, b"{}", "WH-ID")
