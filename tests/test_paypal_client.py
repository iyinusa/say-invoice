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


# ── plain-English errors ─────────────────────────────────────────────────────
def _bad(issue: str, field: str = "") -> dict:
    return {"details": [{"issue": issue, "field": field}]}


@pytest.mark.parametrize(
    ("status", "payload", "says"),
    [
        (0, {"name": "NETWORK"}, "Couldn't reach PayPal"),
        (401, {"error": "invalid_client"}, "credentials"),
        (429, {"name": "RATE_LIMIT_REACHED"}, "busy"),
        (503, {"name": "SERVICE_UNAVAILABLE"}, "trouble right now"),
        (422, _bad("INVALID_STRING_LENGTH", "/primary_recipients/0/billing_info/email_address"),
         "email"),
        (422, _bad("CURRENCY_NOT_SUPPORTED_FOR_RECEIVER"), "currency"),
        (400, _bad("INVALID_PARAMETER_VALUE", "/detail/payment_term/due_date"), "due date"),
        (400, _bad("INVALID_PARAMETER_VALUE", "/items/0/unit_amount/value"), "amounts"),
        (422, _bad("INVOICE_ALREADY_PAID"), "already paid"),
        (422, _bad("CANCEL_NOT_ALLOWED"), "won't cancel"),
        (403, {"name": "NOT_AUTHORIZED"}, "Invoicing turned on"),
        (404, {"name": "RESOURCE_NOT_FOUND"}, "can't find that invoice"),
        (400, {"name": "INVALID_REQUEST", "message": "Request is not well-formed, syntactically "
               "incorrect, or violates schema."}, "Check the details"),
    ],
)  # fmt: skip
def test_errors_read_as_plain_english(status: int, payload: dict, says: str) -> None:
    text = PayPalError(status, payload).human()
    assert says in text
    for jargon in ("schema", "syntactically", "INVALID_", "/items", "_"):
        assert jargon not in text, text


def test_unpaid_is_not_paid() -> None:
    assert (
        "already paid" not in PayPalError(422, {"details": [{"issue": "UNPAID_REMINDER"}]}).human()
    )


# ── no signal ────────────────────────────────────────────────────────────────
async def test_a_dropped_read_is_retried_once() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise httpx.ConnectError("no route", request=request)
        return httpx.Response(200, json={"id": "INV2-1", "status": "SENT"})

    assert (await _client(handler).get_invoice("INV2-1"))["status"] == "SENT"
    assert calls == 2


async def test_no_connection_is_a_paypal_error_not_a_crash() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ReadTimeout("slow", request=request)

    with pytest.raises(PayPalError) as err:
        await _client(handler).get_invoice("INV2-1")
    assert err.value.status == 0 and "Couldn't reach PayPal" in err.value.human()


async def test_a_write_without_an_idempotency_key_is_not_retried() -> None:
    calls = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal calls
        calls += 1
        raise httpx.ReadTimeout("slow", request=request)

    pp = _client(handler)
    with pytest.raises(PayPalError):
        await pp._request("POST", "/v2/invoicing/invoices/INV2-1/remind", json_body={})
    assert calls == 1  # it may have gone through: never risk doing it twice


async def test_a_write_with_an_idempotency_key_is_retried() -> None:
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request.headers["paypal-request-id"])
        if len(seen) == 1:
            raise httpx.ReadTimeout("slow", request=request)
        return httpx.Response(201, json={"id": "INV2-9"})

    pp = _client(handler)
    await pp._request("POST", "/v2/invoicing/invoices", json_body={}, request_id="tap-123")
    assert seen == ["tap-123", "tap-123"]  # same id, so PayPal applies it once


async def test_token_endpoint_down_is_a_paypal_error() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("down", request=request)

    pp = PayPalClient("id", "secret", "https://pp.test", transport=httpx.MockTransport(handler))
    with pytest.raises(PayPalError) as err:
        await pp.get_invoice("INV2-1")
    assert err.value.status == 0
