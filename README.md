# SayInvoice

**Speak the job. Get paid.**

A voice-first web app for plumbers, cleaners, tutors, dog walkers and market traders. It covers the whole job, from quote to agreement, deposit, invoice and payment. Each step is spoken, an AI Deal Agent negotiates within the tradesperson's own rules, and PayPal collects every payment.

> Built for the [PayPal AI Hackathon](https://paypalaihackathon.devpost.com/). Work in progress; a full README with the demo, screenshots and architecture is coming.

## Run it locally

You need [uv](https://docs.astral.sh/uv/) and, for the agents, Node 18+.

```bash
cp .env.example .env        # then fill in the PayPal sandbox + Gemini values
uv sync
uv run alembic upgrade head     # creates ./data/sayinvoice.db
uv run uvicorn app.main:app --reload
```

Open http://localhost:8000 on your phone or laptop. Tap the mic and describe a job, or type it. Check the card, then tap **Send**. Sending needs the PayPal sandbox values below; reading a job only needs the Gemini key.

### Or with Docker

```bash
cp .env.example .env        # fill it in as above
docker compose up --build   # then open http://localhost:8000
```

- **Settings** come from `.env` at run time; the image never contains your keys.
- **The database** (SQLite by default) lives in the `sayinvoice-data` volume, so invoices and deal rules survive rebuilds. To use Postgres, set `SI_DATABASE_URL` in `.env`. `docker compose down -v` deletes the volume.
- **PayPal's MCP server** is pre-installed in the image and used automatically.
- **Migrations** run on every start.
- **Another port:** `SI_PORT=8080 docker compose up`.

### PayPal sandbox setup

1. Sign in at [developer.paypal.com](https://developer.paypal.com/dashboard/) and open **Testing Tools → Sandbox Accounts**. You get a **Business** account (the tradesperson) and a **Personal** account (the client who pays).
2. Go to **Apps & Credentials → Sandbox → Create App**. Copy the Client ID and Secret into `SI_PAYPAL_CLIENT_ID` / `SI_PAYPAL_CLIENT_SECRET`.
3. Put the sandbox Business account email in `SI_PAYPAL_MERCHANT_EMAIL`.
4. Once the app has a public HTTPS URL, run `uv run python -m scripts.register_webhook https://<your-app>`. Copy the ID it prints into `SI_PAYPAL_WEBHOOK_ID`. Until then, invoices still turn "Paid" within a minute: the app re-checks open invoices with PayPal every 60 seconds, and the Sent screen checks every 8 seconds while it waits.

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

## Voice read-back

When a card is ready, SayInvoice reads it aloud: "Invoice for £145.00 to Mr Kennedy, due in 7 days. Say send to send it". Then it listens for a one-word reply.

- **Saying "send"** (or "yes", "go ahead") starts a visible 3-second countdown with a Stop button before anything is sent.
- **Saying "change"** or "wait" hands control back. If the reply contains both, stop wins.
- **What it reads** is built by code from the card, never by the AI.
- **The voice** is the phone's own by default: instant, free, and in the card's language. Set `SI_TTS_ENGINE=kokoro` to use [KokoroTTS](https://github.com/hexgrad/kokoro) on the server instead. It's optional because it needs PyTorch (`uv sync --extra tts`, about 1 GB) and downloads a model on first use; it isn't in the Docker image.

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
