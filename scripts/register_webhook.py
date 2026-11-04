"""Register (or find) the PayPal webhook for this deployment and print its ID.

    uv run python -m scripts.register_webhook https://sayinvoice.onrender.com
    uv run python -m scripts.register_webhook --list
    uv run python -m scripts.register_webhook --delete WH-XXXX

Put the printed ID in SI_PAYPAL_WEBHOOK_ID (Render env var or .env). PayPal
only delivers to public HTTPS URLs, so this needs the deployed app or a tunnel.
"""

from __future__ import annotations

import argparse
import asyncio
from typing import Any

from app.paypal.client import PayPalClient, PayPalError
from app.settings import get_settings

# Any INVOICING event triggers a re-read of the invoice, so subscribe to all of them.
EVENTS = [
    "INVOICING.INVOICE.PAID",
    "INVOICING.INVOICE.CANCELLED",
    "INVOICING.INVOICE.REFUNDED",
    "INVOICING.INVOICE.UPDATED",
]


async def main(args: argparse.Namespace) -> None:
    s = get_settings()
    if not s.paypal_configured:
        raise SystemExit("Set SI_PAYPAL_CLIENT_ID and SI_PAYPAL_CLIENT_SECRET in .env first.")
    pp = PayPalClient(s.paypal_client_id, s.paypal_client_secret, s.paypal_base_url)
    try:
        hooks: list[dict[str, Any]] = (
            await pp.request_json("GET", "/v1/notifications/webhooks")
        ).get("webhooks", [])
        if args.list:
            for h in hooks:
                print(h["id"], h["url"], [e["name"] for e in h.get("event_types", [])])
            return
        if args.delete:
            await pp.request_json("DELETE", f"/v1/notifications/webhooks/{args.delete}")
            print(f"deleted {args.delete}")
            return
        if not args.base_url:
            raise SystemExit("give the app's public base URL, e.g. https://sayinvoice.onrender.com")
        url = args.base_url.rstrip("/") + "/api/webhooks/paypal"
        if not url.startswith("https://"):
            raise SystemExit("PayPal only delivers webhooks to https:// URLs")
        existing = next((h for h in hooks if h["url"] == url), None)
        if existing:
            print(f"already registered: {existing['id']}")
            return
        created = await pp.request_json(
            "POST",
            "/v1/notifications/webhooks",
            json_body={"url": url, "event_types": [{"name": e} for e in EVENTS]},
        )
        print(f"registered {created['id']} -> {url}\nSet SI_PAYPAL_WEBHOOK_ID={created['id']}")
    except PayPalError as exc:
        raise SystemExit(f"PayPal error: {exc} {exc.details}") from exc
    finally:
        await pp.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("base_url", nargs="?")
    p.add_argument("--list", action="store_true")
    p.add_argument("--delete", metavar="WEBHOOK_ID")
    asyncio.run(main(p.parse_args()))
