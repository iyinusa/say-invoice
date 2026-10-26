from __future__ import annotations

import os

# Before any app import: tests never read the developer's .env or touch PayPal.
os.environ["SI_DATABASE_URL"] = "sqlite+aiosqlite:///:memory:"
os.environ["SI_PAYPAL_CLIENT_ID"] = "test-client"
os.environ["SI_PAYPAL_CLIENT_SECRET"] = "test-secret"
os.environ["SI_PAYPAL_WEBHOOK_ID"] = "WH-TEST"
os.environ["SI_GOOGLE_API_KEY"] = ""
