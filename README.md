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

Open http://localhost:8000 on your phone or laptop. Tap the mic and describe a job, or type it. Check the card, then tap **Send**. Sending needs the PayPal sandbox values below; reading a job only needs the Gemini key.

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

## Parser accuracy

The parser turns a voice note into a draft invoice or quote in one Gemini call. Code then prices every line, resolves the due date and decides what's missing: the model never does sums. We measure the whole thing end to end, scoring the card the tradesperson would see, with [`scripts/eval_parser.py`](scripts/eval_parser.py) on [25 cases](evals/parser_cases.json):

- **Trades:** plumbers, cleaners, dog walkers, builders, tutors, market traders.
- **Money:** £, $, € and PLN, plus slang ("a ton", "quid", "bucks").
- **Languages:** Spanish, French and Polish.
- **How people talk:** self-corrections and spelled-out emails.
- **Dates:** "due Friday", "net thirty", "end of next week".
- **Incomplete jobs:** a missing price or a missing client.
- **Edge cases:** a currency PayPal can't invoice in.

| `gemini-3.5-flash`, minimal thinking | Cards fully right | Items, prices, totals | Due dates | Deposits | Typical time |
|---|---|---|---|---|---|
| Typed text (25 cases × 2 runs) | **100%** (50/50) | 100% | 100% | 100% | 1.9 s |
| Voice notes (25 × 2) | **92%** (46/50) | 100% | 100% | 100% | 2.7 s |

All four voice misses are names that sound alike in an email address (Neill/Neil, Shah/Shaw), which no listener could resolve. That's why the app always highlights an email it heard, so the tradesperson can check it before sending.

The voice notes in `evals/audio/` are synthetic: Gemini TTS reads each case in a regional accent, with background noise mixed in ([`scripts/make_eval_audio.py`](scripts/make_eval_audio.py)). Drop a real phone recording in with the same file name and the eval uses it instead.

```bash
uv run python -m scripts.eval_parser --repeat 2            # typed text
uv run python -m scripts.eval_parser --repeat 2 --audio    # voice notes
uv run python -m scripts.eval_parser --model gemini-3.5-flash --model gemini-3.5-flash-lite
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
