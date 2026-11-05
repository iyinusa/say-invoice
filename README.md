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

### Demo data

```bash
uv run python -m scripts.seed_demo
```

This adds a realistic fortnight for a plumber, through the app's own services and your PayPal sandbox:
- a paid invoice, one not yet due, and one 12 days overdue
- an overdue invoice now on an agreed three-payment plan
- a quote being negotiated, and a quote waiting for your answer
- a booked job with its deposit paid, and a quote just sent

Every invoice really exists in the sandbox, so Remind, the QR code and paying from PayPal all work. The clients' chat turns are scripted, so no Gemini key is needed. You can run it again: it adds nothing the second time. It refuses a live PayPal account.

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
- **The voice** is the phone's own by default: instant, free, and in the card's language. Set `SI_TTS_ENGINE=kokoro` to use [KokoroTTS](https://github.com/hexgrad/kokoro) on the server instead. It's optional because it needs PyTorch (about 1 GB) and an ~330 MB model:

- **Locally:** run `uv sync --extra tts`. The model downloads on first use.
- **In Docker:** set `SI_INSTALL_TTS=true` and run `docker compose up --build`. The image then includes Kokoro, espeak-ng and the model, so the first read-back is quick.

If Kokoro is switched on but not installed, the app logs a warning at startup and uses the phone's voice. It never errors.

**How Kokoro stays fast:**
- **Loaded at startup.** The server loads it in the background when it starts, never on someone's first tap. It loads one shared model, plus each language in `SI_TTS_PRELOAD` (default `en-GB,en-US`): pronunciation rules, voice pack and one warm-up inference. The fixed "Say send to send it…" sentences are made during that warm-up and cached. Startup takes about 15 to 20 seconds on a laptop CPU; `GET /api/tts/status` shows progress, and until it's ready the phone's voice reads.
- **One sentence at a time.** The phone asks for one sentence at a time, the first split at its comma, and plays each while the next is made. On a laptop CPU the first words come about 2 seconds after the card appears. Kokoro makes speech about 3 times faster than it plays, so there are no gaps after that.
- **Audio ready on the first tap.** The first tap plays a moment of silence. That unlocks audio on iPhone and starts the audio output early, so the first read-back doesn't stall.
- **Nothing downloaded at runtime in Docker.** The image downloads the model and every voice pack at build time and runs with `HF_HUB_OFFLINE=1`.

## First run and installing on a phone

The first time the app opens, a short guided setup (about a minute) shows a live preview of your invoice that builds itself as you answer:

1. **You:** business name, first name and trade. The trade appears on quote and payment pages and sets the sample job in the preview and the examples.
2. **Invoices:** currency, when invoices fall due, and a note for every invoice (PayPal's "Note to recipient" unless a job has its own).
3. **Deal rules:** most off labour, the secret lowest price, and the deposit. A live haggle shows what the Deal Agent would offer, worked out the way PolicyGuard prices it.
4. **Getting paid:** how many payments a plan may have, and whether the Chase Agent's reminders need your approval or go out on their own.

A setup left halfway picks up where it was. Everything can be changed later under ⚙ Settings. Existing databases skip setup after `alembic upgrade head`.

SayInvoice is an installable web app. On the deployed https URL, use **Add to Home Screen** (Safari) or **Install app** (Chrome). A small service worker lets the app open with no signal; nothing about money is ever cached, so every invoice, quote and payment request goes to the server. On plain http, other than localhost, the browser won't install it.

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

## Security

- **Owner sign-in.** The tradesperson's app and its API need `SI_OWNER_PASSCODE`. One passcode, then a signed, HttpOnly cookie that lasts 30 days. Changing the passcode signs every device out.
  - Locally, with no passcode set, the app is open.
  - In production it **refuses to start** without one, so it is never public by accident.
  - Clients never sign in: their quote and plan pages are behind 128-bit links.
- **What stays public:** the client pages (`/q/…`, `/p/…`), PayPal's webhook, health, static files and the sign-in page.
- **Webhooks are verified.** Every webhook is checked with PayPal's signature API. Even then, the app re-reads the invoice from PayPal rather than trusting the message body.
- **Cross-site writes are refused.** An owner request whose `Origin` is another site gets a 403, and the cookie is `SameSite=Lax`.
- **Rate limits, per IP:**

  | What | Limit |
  |---|---|
  | Reading a job (Gemini) | 30 a minute, 300 an hour |
  | Client chat (Deal and Plan agents) | 10 a minute, 60 an hour |
  | Accepting a quote or plan | 20 a minute |
  | Server voice | 60 a minute |
  | Sign-in attempts | 5 a minute, 30 an hour |

- **Prompt injection.** Client chat is untrusted. The agents never see the floor price, and PolicyGuard prices every offer in code. Every amount or percentage the agent writes must match what code computed. Each message is a single line of the transcript, so a client can't type a fake "Owner:" turn. `tests/test_prompt_injection.py` runs the classic attacks against an agent scripted to fall for every one.
- **No personal data in logs.** Logs carry ids and amounts. Emails, phone numbers and client-link tokens are masked, and the access log drops query strings.
- **Secrets only in the environment.** The Docker image never contains them.

## Deploy

[`render.yaml`](render.yaml) is a Render Blueprint. It sets up the web service (Docker: Python plus PayPal's Node MCP server), the hourly Chase Agent job and a Postgres database, all on paid plans, so there are no cold starts. Render asks for the secrets once, including `SI_OWNER_PASSCODE`, and generates `SI_SESSION_SECRET` itself. For judges, put the passcode in the submission notes.

## License

[MIT](LICENSE)
