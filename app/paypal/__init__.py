from __future__ import annotations

from app.paypal.client import PayPalClient, PayPalError
from app.settings import get_settings

_client: PayPalClient | None = None


class PayPalNotConfigured(RuntimeError):
    """No sandbox credentials yet. Answered as 503 by the app's exception handler."""


def get_paypal() -> PayPalClient:
    """Process-wide client (one token cache). FastAPI dependency; overridden in tests."""
    global _client
    if _client is None:
        settings = get_settings()
        if not settings.paypal_configured:
            raise PayPalNotConfigured("set SI_PAYPAL_CLIENT_ID and SI_PAYPAL_CLIENT_SECRET")
        _client = PayPalClient(
            settings.paypal_client_id, settings.paypal_client_secret, settings.paypal_base_url
        )
    return _client


async def close_paypal() -> None:
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None


__all__ = ["PayPalClient", "PayPalError", "PayPalNotConfigured", "close_paypal", "get_paypal"]
