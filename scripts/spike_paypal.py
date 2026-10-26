"""Spike 1 + 4: prove the PayPal sandbox loop end to end before building on it.

    uv run python -m scripts.spike_paypal --to <sandbox-personal-email>
    uv run python -m scripts.spike_paypal --to <email> --partial 50 --try-update

1. creates and sends a real sandbox invoice (2h labour + a part)
2. saves its "scan to pay" QR code to spike-qr.png
3. --try-update: adds partial-payment terms to the *sent* invoice (Spike 4b)
4. polls the status while you pay it as the sandbox buyer, so you can watch
   SENT -> PARTIALLY_PAID -> PAID (and check the webhook arrives on Render)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import time
from datetime import date, timedelta
from decimal import Decimal
from pathlib import Path

from app.paypal.client import PayPalClient, PayPalError
from app.paypal.invoices import InvoiceDraft, Item, Party, build_invoice_body
from app.settings import get_settings


async def main(args: argparse.Namespace) -> None:
    s = get_settings()
    if not s.paypal_configured:
        raise SystemExit("Set SI_PAYPAL_CLIENT_ID and SI_PAYPAL_CLIENT_SECRET in .env first.")
    pp = PayPalClient(s.paypal_client_id, s.paypal_client_secret, s.paypal_base_url)
    try:
        number = await pp.next_invoice_number()
        print(f"next invoice number: {number}")

        draft = InvoiceDraft(
            number=number,
            currency=args.currency,
            items=[
                Item("Boiler repair (labour)", Decimal(2), Decimal(60), unit_of_measure="HOURS"),
                Item("Replacement valve", Decimal(1), Decimal(25)),
            ],
            recipient=Party(args.name, args.to),
            invoicer=Party("Dave Mason", s.paypal_merchant_email, "Dave's Plumbing"),
            invoice_date=date.today(),
            due_date=date.today() + timedelta(days=7),
            note="Thanks for having me fix the boiler.",
            minimum_amount_due=(
                Decimal(args.partial) if args.partial and not args.try_update else None
            ),
        )
        body = build_invoice_body(draft)
        invoice = await pp.create_draft_invoice(body)
        invoice_id = invoice["id"]
        print(f"created draft {invoice_id}  status={invoice.get('status')}  total={draft.total}")

        link = await pp.send_invoice(invoice_id, subject=f"Invoice {number} from Dave's Plumbing")
        print(f"sent. pay link: {link or '(none returned)'}")

        try:
            png = await pp.generate_qr_code(invoice_id)
            await asyncio.to_thread(Path("spike-qr.png").write_bytes, png)
            print(f"QR code saved to spike-qr.png ({len(png)} bytes)")
        except (PayPalError, ValueError) as exc:
            print(f"QR code failed: {exc}")

        if args.try_update:
            # Spike 4b: can a sent invoice gain partial-payment terms (payment-plan mode)?
            current = await pp.get_invoice(invoice_id)
            current.setdefault("configuration", {})["partial_payment"] = {
                "allow_partial_payment": True,
                "minimum_amount_due": {"currency_code": args.currency, "value": str(args.partial)},
            }
            try:
                updated = await pp.update_invoice(invoice_id, current)
                print(
                    "UPDATE OK: partial payment now",
                    json.dumps(updated.get("configuration", {}).get("partial_payment")),
                )
            except PayPalError as exc:
                print(f"UPDATE FAILED ({exc.human()}): {json.dumps(exc.payload)[:600]}")

        print(f"\nPay it now as the sandbox buyer ({args.to}). Polling for {args.wait}s...")
        last = ""
        deadline = time.monotonic() + args.wait
        while time.monotonic() < deadline:
            inv = await pp.get_invoice(invoice_id)
            status = inv.get("status", "")
            paid = (inv.get("payments") or {}).get("paid_amount", {}).get("value", "0")
            if status != last:
                print(f"  {time.strftime('%H:%M:%S')}  status={status}  paid={paid}")
                last = status
            if status in {"PAID", "MARKED_AS_PAID", "CANCELLED"}:
                break
            await asyncio.sleep(5)
    except PayPalError as exc:
        print(f"\nPayPal error: {exc}\n  -> {exc.human()}\n  debug_id={exc.debug_id}")
        print(json.dumps(exc.payload, indent=2)[:2000])
    finally:
        await pp.aclose()


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    p.add_argument("--to", required=True, help="recipient email: your sandbox Personal account")
    p.add_argument("--name", default="Tom Kennedy", help="recipient name")
    p.add_argument("--currency", default="GBP")
    p.add_argument("--partial", type=Decimal, help="minimum amount per payment, e.g. 50")
    p.add_argument("--try-update", action="store_true", help="add --partial after sending (4b)")
    p.add_argument("--wait", type=int, default=300, help="seconds to poll for payment")
    asyncio.run(main(p.parse_args()))
