# SayInvoice

**Speak the job. Get paid.**

A voice-first web app for plumbers, cleaners, tutors, dog walkers and market traders. It covers the whole job, from quote to agreement, deposit, invoice and payment. Each step is spoken, an AI Deal Agent negotiates within the tradesperson's own rules, and PayPal collects every payment.

> Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/). Work in progress; a full README with the demo, screenshots and architecture is coming.

## Run it locally

You need [uv](https://docs.astral.sh/uv/) and, for the agents, Node 18+.

```bash
cp .env.example .env        # then fill in the PayPal sandbox + Gemini values
uv sync
uv run alembic upgrade head
uv run uvicorn app.main:app --reload
```

Open http://localhost:8000/healthz.

### PayPal sandbox setup

1. Sign in at [developer.paypal.com](https://developer.paypal.com/dashboard/) and open **Testing Tools → Sandbox Accounts**. You get a **Business** account (the tradesperson) and a **Personal** account (the client who pays).
2. Go to **Apps & Credentials → Sandbox → Create App**. Copy the Client ID and Secret into `SI_PAYPAL_CLIENT_ID` / `SI_PAYPAL_CLIENT_SECRET`.
3. Put the sandbox Business account email in `SI_PAYPAL_MERCHANT_EMAIL`.
4. Once the app has a public URL, add a webhook on the app for `{SI_PUBLIC_BASE_URL}/api/webhooks/paypal` with the `INVOICING.INVOICE.*` events. Copy its ID into `SI_PAYPAL_WEBHOOK_ID`.

### Spikes (end-to-end checks against the real sandbox)

```bash
# 1 + 4: create, send, QR-code and pay a real sandbox invoice; partial payments
uv run python -m scripts.spike_paypal --to <sandbox-personal-email>
uv run python -m scripts.spike_paypal --to <sandbox-personal-email> --partial 50 --try-update

# 2: an ADK agent using PayPal's own MCP tools (read-only)
uv run python -m scripts.spike_adk_mcp

# 3: voice note -> structured job, one multimodal Gemini call
uv run python -m scripts.spike_gemini_audio path/to/voice-note.m4a
```

## Tests

```bash
uv run pytest
uv run ruff check .
```

## Deploy

[`render.yaml`](render.yaml) is a Render Blueprint. It sets up the web service (Docker: Python plus PayPal's Node MCP server) and a Postgres database.

## License

[MIT](LICENSE)
