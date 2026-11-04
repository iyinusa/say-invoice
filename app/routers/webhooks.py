"""PayPal webhook receiver.

Every delivery is signature-verified with PayPal before we act on it: the URL is
public, and an unverified "INVOICE.PAID" would let anyone mark a job paid.
"""

from __future__ import annotations

import json

from fastapi import APIRouter, Depends, HTTPException, Request
from sqlalchemy.ext.asyncio import AsyncSession

from app.db import get_session
from app.logging_config import get_logger
from app.paypal import PayPalClient, PayPalError, get_paypal
from app.services import payments
from app.settings import Settings, get_settings

router = APIRouter(prefix="/api/webhooks", tags=["webhooks"])
log = get_logger("webhooks")


@router.post("/paypal")
async def paypal_webhook(
    request: Request,
    settings: Settings = Depends(get_settings),
    paypal: PayPalClient = Depends(get_paypal),
    session: AsyncSession = Depends(get_session),
) -> dict[str, str]:
    if not settings.paypal_webhook_id:
        raise HTTPException(503, "webhook not configured")

    raw = await request.body()
    try:
        verified = await paypal.verify_webhook(request.headers, raw, settings.paypal_webhook_id)
    except PayPalError as exc:
        # PayPal is down or rejected the check: ask it to redeliver later.
        log.warning("webhook_verify_failed", error=str(exc), debug_id=exc.debug_id)
        raise HTTPException(502, "could not verify") from exc
    if not verified:
        log.warning("webhook_signature_invalid")
        raise HTTPException(400, "invalid signature")

    event = json.loads(raw)
    try:
        # Re-read the invoice from PayPal rather than trusting the body: replays
        # and out-of-order deliveries can then never move an invoice backwards.
        outcome = await payments.handle_webhook(session, paypal, event)
    except PayPalError as exc:
        log.warning("webhook_refresh_failed", error=str(exc), debug_id=exc.debug_id)
        raise HTTPException(502, "could not read the invoice") from exc
    log.info(
        "paypal_event",
        event_id=event.get("id"),
        event_type=event.get("event_type"),
        invoice_id=payments.webhook_invoice_id(event),
        outcome=outcome,
    )
    return {"status": "received", "outcome": outcome}
