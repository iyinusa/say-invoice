// SayInvoice tradesperson app: Record -> Preview -> Send -> Sent.
// Amounts here are for display only; the server recomputes every total.

const PAY_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_RECORD_SECONDS = 90;
const CHECK_EVERY_MS = 8000; // backup for slow webhooks while the Sent screen waits
const SETTLED = ["paid", "cancelled", "refunded"];
// Currencies PayPal Invoicing accepts (mirrors app/agents/parser_rules.py).
const PAYPAL_CURRENCIES = "AUD BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF ILS JPY MXN MYR NOK NZD PHP PLN RUB SEK SGD THB TWD USD".split(" ");

// Spoken replies: "send" sends (after a visible countdown), "change" hands control back.
const SAY_SEND = /\b(send( it)?|yes|yeah|yep|go( ahead)?|do it|confirm|ok(ay)?)\b/i;
const SAY_STOP = /\b(no|stop|cancel|change|wait|edit|hold on|not yet)\b/i;
const SEND_COUNTDOWN = 3;

function readStore(key, fallback) {
  try {
    const v = localStorage.getItem(key);
    return v === null ? fallback : JSON.parse(v);
  } catch {
    return fallback;
  }
}
function writeStore(key, value) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
  } catch {
    /* private mode: the toggle just isn't remembered */
  }
}

// ── first-run setup ───────────────────────────────────────────────────────
// A sample job per trade: it fills the live invoice preview and the "Try saying" examples.
const TRADES = [
  { key: "plumber", label: "Plumber", said: "Fixed Mr Kennedy's boiler", labour: "Boiler repair", qty: 2, unit: "hours", rate: 60,
    part: "Pressure valve", partSaid: "a valve", parts: 25, quoteSaid: "Quote for Mrs Patel, replace the kitchen tap", quoteLabour: 90, quoteParts: 40 },
  { key: "electrician", label: "Electrician", said: "Fitted four new sockets for Mr Kennedy", labour: "Socket installation", qty: 3, unit: "hours", rate: 55,
    part: "Sockets and cable", partSaid: "sockets and cable", parts: 48, quoteSaid: "Quote for Mrs Patel, rewire the garage", quoteLabour: 380, quoteParts: 120 },
  { key: "builder", label: "Builder", said: "Rebuilt Mr Kennedy's garden wall", labour: "Garden wall repair", qty: 6, unit: "hours", rate: 40,
    part: "Bricks and mortar", partSaid: "bricks and mortar", parts: 85, quoteSaid: "Quote for Mrs Patel, a new patio", quoteLabour: 900, quoteParts: 650 },
  { key: "cleaner", label: "Cleaner", said: "End of tenancy clean for Mr Kennedy", labour: "End of tenancy clean", qty: 4, unit: "hours", rate: 22,
    part: "Cleaning supplies", partSaid: "supplies", parts: 15, quoteSaid: "Quote for Mrs Patel, a deep clean of the office", quoteLabour: 120, quoteParts: 20 },
  { key: "gardener", label: "Gardener", said: "Cut back Mr Kennedy's hedges", labour: "Hedge trimming", qty: 3, unit: "hours", rate: 30,
    part: "Green waste removal", partSaid: "taking the waste away", parts: 25, quoteSaid: "Quote for Mrs Patel, lay a new lawn", quoteLabour: 300, quoteParts: 180 },
  { key: "decorator", label: "Decorator", said: "Painted Mr Kennedy's living room", labour: "Painting and prep", qty: 8, unit: "hours", rate: 28,
    part: "Paint and materials", partSaid: "paint", parts: 60, quoteSaid: "Quote for Mrs Patel, paint the hallway and stairs", quoteLabour: 450, quoteParts: 90 },
  { key: "mechanic", label: "Mechanic", said: "Serviced Mr Kennedy's van", labour: "Full service", qty: 2, unit: "hours", rate: 50,
    part: "Oil and filters", partSaid: "oil and filters", parts: 45, quoteSaid: "Quote for Mrs Patel, new brake pads and discs", quoteLabour: 120, quoteParts: 140 },
  { key: "other", label: "Something else", said: "Did a job for Mr Kennedy", labour: "Labour", qty: 2, unit: "hours", rate: 50,
    part: "Materials", partSaid: "materials", parts: 30, quoteSaid: "Quote for Mrs Patel", quoteLabour: 200, quoteParts: 60 },
];
const TOP_CURRENCIES = ["GBP", "USD", "EUR", "CAD", "AUD"];
const ONB_STEPS = ["You", "Invoices", "Deals", "Getting paid"];
// Rough size of one pound in each currency, so a ¥ or Ft sample job doesn't look like pocket change.
const CURRENCY_SCALE = { BRL: 6, CNY: 9, CZK: 29, DKK: 9, HKD: 10, HUF: 480, ILS: 5, JPY: 190, MXN: 24,
                         MYR: 6, NOK: 14, PHP: 70, PLN: 5, RUB: 110, SEK: 14, THB: 44, TWD: 40 };
const DEFAULT_POLICY = { max_discount_pct: 10, floor_pct: 90, min_deposit_pct: 25, max_instalments: 3, max_plan_days: 60,
                         price_negotiable: true, scope_negotiable: true, date_negotiable: true, auto_agree_within_policy: true };
const ONB_DRAFT = "si.onb";
const ONB_FIELDS = ["step", "reached", "business_name", "owner_name", "trade", "trade_other", "currency", "default_due_days",
                    "invoice_note", "note_seeded", "max_discount_pct", "floor_pct", "min_deposit_pct", "ask_first",
                    "max_instalments", "auto_chase"];
// The saved trade ("Plumber") -> its sample job; anything else is the generic one.
const tradeKey = (label) => (label ? TRADES.find((t) => t.label === label)?.key || "other" : "");
const shortDate = (d) => d.toLocaleDateString(undefined, { day: "numeric", month: "short" });
const daysFromNow = (n) => {
  const d = new Date();
  d.setDate(d.getDate() + n);
  return d;
};

// Buzz the phone, but only once the user has touched the page (browsers block it before).
function buzz(pattern) {
  if (navigator.userActivation?.hasBeenActive) navigator.vibrate?.(pattern);
}

function newKey() {
  if (window.crypto?.randomUUID) return crypto.randomUUID();
  return Array.from(crypto.getRandomValues(new Uint8Array(16)), (b) => b.toString(16).padStart(2, "0")).join("");
}

// "2,5" (a comma decimal on many phone keyboards) and "£60" both mean what they look like.
function num(value) {
  const n = Number(String(value ?? "").replace(/[^\d.,-]/g, "").replace(",", "."));
  return Number.isFinite(n) ? n : NaN;
}

// 50 ms of 16-bit mono silence as a WAV blob URL: primes the audio element (see primeSpeech).
function silentWav() {
  const rate = 8000, samples = rate / 20, buf = new ArrayBuffer(44 + samples * 2), v = new DataView(buf);
  const text = (at, s) => [...s].forEach((c, i) => v.setUint8(at + i, c.charCodeAt(0)));
  text(0, "RIFF"); v.setUint32(4, 36 + samples * 2, true); text(8, "WAVEfmt ");
  v.setUint32(16, 16, true); v.setUint16(20, 1, true); v.setUint16(22, 1, true);
  v.setUint32(24, rate, true); v.setUint32(28, rate * 2, true); v.setUint16(32, 2, true); v.setUint16(34, 16, true);
  text(36, "data"); v.setUint32(40, samples * 2, true);
  return URL.createObjectURL(new Blob([buf], { type: "audio/wav" }));
}

function errorText(body, fallback) {
  const d = body?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d[0]?.msg) return d[0].msg.replace(/^Value error, /, "");
  return fallback;
}

// Signed out (the 30-day cookie ran out, or the passcode changed): back to the sign-in page.
{
  const ownFetch = window.fetch.bind(window);
  window.fetch = async (...args) => {
    const resp = await ownFetch(...args);
    if (resp.status === 401 && new URL(resp.url || location.href).origin === location.origin) location.assign("/login");
    return resp;
  };
}

window.sayInvoice = () => ({
  screen: "record",
  error: "",
  busy: false,

  // recording
  recording: false,
  seconds: 0,
  recorder: null,
  timer: null,
  typing: false,
  typed: "",

  // preview
  card: { client_name: "", client_email: "", currency: "", due_date: "", notes: "", items: [], deposit_pct: "", job_date: "" },
  flags: [],
  missing: [],
  question: "",
  answer: "",
  warnings: [],
  transcript: "",
  draft: null,
  key: "",
  noteOpen: false,
  clientSource: "",
  suggestions: [],
  clientOptions: [],
  mode: "invoice",
  quote: null,
  jobs: [],
  allJobs: false,
  job: null,
  matchedJob: null,

  // sent
  invoice: null,
  qrOpen: false,
  qrError: false,
  copied: false,
  wakeLock: null,
  canShare: typeof navigator.share === "function",
  checkTimer: null,

  // deal rules
  policy: null,
  saved: false,

  // money view
  home: null,
  offline: !navigator.onLine,
  loadFailed: { home: false, detail: false },
  tab: "owed",
  detail: null,
  detailRef: null,
  acting: false,
  actionMsg: "",
  actionError: "",
  cancelArmed: false,
  payOpen: false,
  pay: { amount: "", method: "CASH" },

  // voice read-back
  readBack: readStore("si.readback", true),
  voiceState: "idle", // idle | speaking | listening | countdown
  heard: "",
  countdown: 0,
  countdownTimer: null,
  recognizer: null,
  audioEl: null,
  language: "en",
  canListen: !!(window.SpeechRecognition || window.webkitSpeechRecognition),
  canSpeak: "speechSynthesis" in window || window.SAYINVOICE.ttsEngine === "kokoro",

  // onboarding + business settings
  TRADES, TOP_CURRENCIES, ONB_STEPS,
  onb: { step: 0, reached: 1, dir: "fwd", business_name: "", owner_name: "", trade: tradeKey(window.SAYINVOICE.trade), trade_other: "",
         currency: window.SAYINVOICE.currency || "GBP", default_due_days: 7, invoice_note: "", note_seeded: false,
         max_discount_pct: 10, floor_pct: 90, min_deposit_pct: 25, ask_first: false, max_instalments: 3, auto_chase: false,
         paypal_account: "", paypal_env: "sandbox", currencies: PAYPAL_CURRENCIES, _policy: null },
  invoiceNote: window.SAYINVOICE.invoiceNote || "",
  ownerName: window.SAYINVOICE.ownerName || "",
  biz: null,
  businessName: window.SAYINVOICE.businessName,

  // demo clock
  clockInfo: null,
  clockBusy: false,

  // chase agent
  reminders: [],
  chaseRunning: false,
  chaseSummary: null,
  chaseBusy: null,
  chaseError: "",
  editingReminder: null,
  reminderText: "",
  autoChase: false,

  // approvals (Deal Agent escalations)
  approvals: [],
  countering: false,
  replying: false,
  counter: { discount: "", deposit: "", note: "" },
  deciding: false,
  approvalError: "",
  later: [], // message ids put off with "Later" this session; they stay pending
  get waiting() {
    return this.approvals.filter((a) => !this.later.includes(a.message_id));
  },
  get approval() {
    return this.waiting[0] || null;
  },
  answerLater() {
    if (!this.approval) return;
    this.later = [...this.later, this.approval.message_id];
    Object.assign(this, { countering: false, replying: false, approvalError: "" });
  },
  // Opening a job that's waiting for you brings its request back.
  unsnooze(kind, id) {
    const key = kind === "quote" ? "quote_id" : "invoice_id";
    const mine = this.approvals.filter((a) => a[key] === id).map((a) => a.message_id);
    if (mine.length) this.later = this.later.filter((m) => !mine.includes(m));
  },

  init() {
    this.listen();
    this.loadApprovals();
    this.loadJobs();
    this.loadHome();
    this.loadReminders();
    this.loadClock();
    if (!window.SAYINVOICE.onboarded) this.startOnboarding();
    window.addEventListener("online", () => {
      this.offline = false;
      this.loadHome();
      this.loadJobs();
    });
    window.addEventListener("offline", () => (this.offline = true));
    // Installable app + opens with no signal. Needs https (localhost counts).
    if ("serviceWorker" in navigator && window.isSecureContext) {
      navigator.serviceWorker.register("/sw.js").catch(() => {});
    }
  },

  // ── onboarding ────────────────────────────────────────────────────────
  async startOnboarding() {
    this.screen = "onboarding";
    try {
      const [ws, pol] = await Promise.all([fetch("/api/workspace"), fetch("/api/policy")]);
      const w = ws.ok ? await ws.json() : {};
      const p = pol.ok ? await pol.json() : {};
      const trade = TRADES.find((t) => t.key !== "other" && t.label === w.trade);
      Object.assign(this.onb, {
        trade: trade ? trade.key : w.trade ? "other" : "", trade_other: trade ? "" : w.trade || "",
        currency: w.currency || "GBP", default_due_days: w.default_due_days ?? 7, invoice_note: w.invoice_note || "",
        auto_chase: !!w.auto_chase, paypal_account: w.paypal_account || "", paypal_env: w.paypal_env || "sandbox",
        currencies: w.currencies || PAYPAL_CURRENCIES,
        max_discount_pct: Number(p.max_discount_pct ?? 10), floor_pct: Number(p.floor_pct ?? 90),
        min_deposit_pct: Number(p.min_deposit_pct ?? 25), max_instalments: Number(p.max_instalments ?? 3),
        ask_first: p.auto_agree_within_policy === false, _policy: pol.ok ? p : null,
      });
    } catch {
      /* the defaults still work */
    }
    // A setup left halfway (the phone locked, the tab closed) picks up where it was.
    const draft = readStore(ONB_DRAFT, null);
    if (draft && typeof draft === "object") {
      for (const k of ONB_FIELDS) if (k in draft && typeof draft[k] === typeof this.onb[k]) this.onb[k] = draft[k];
    }
    // Every change is kept as it's made (a slider, a chip), not only on Continue.
    this.$watch("onb", () => {
      if (!window.SAYINVOICE.onboarded && this.onb.step <= 4) this.onbSaveDraft();
    });
    this.onbFocus();
  },
  onbSaveDraft() {
    writeStore(ONB_DRAFT, Object.fromEntries(ONB_FIELDS.map((k) => [k, this.onb[k]])));
  },
  onbGo(step) {
    step = Math.max(0, Math.min(step, 5));
    if (step === this.onb.step) return;
    this.error = "";
    // Step 5 is only reached by saving; the rail and Back only go to steps already seen.
    if (step === 5 && !window.SAYINVOICE.onboarded) return;
    this.onb.dir = step < this.onb.step ? "back" : "fwd";
    this.onb.step = step;
    this.onb.reached = Math.max(this.onb.reached, Math.min(step, 4));
    if (step === 2 && !this.onb.note_seeded) {
      this.onb.note_seeded = true;
      if (!this.onb.invoice_note) this.onb.invoice_note = this.onbNoteIdeas()[0];
    }
    if (step <= 4) this.onbSaveDraft();
    this.onbFocus();
  },
  onbFocus() {
    this.$nextTick(() => {
      window.scrollTo(0, 0);
      const pane = [...this.$root.querySelectorAll(".onboarding .onb-pane")].find((el) => el.style.display !== "none");
      const target = this.onb.step === 1 && !this.onb.business_name.trim() ? pane?.querySelector("#onb-biz") : pane?.querySelector("h1");
      target?.focus({ preventScroll: true });
    });
  },
  onbCanContinue() {
    if (this.onb.step === 1) return !!this.onb.business_name.trim();
    return true;
  },
  async onbNext() {
    if (!this.onbCanContinue() || this.busy) return;
    if (this.onb.step < 4) return this.onbGo(this.onb.step + 1);
    if (await this.onbSave()) this.onbGo(5);
  },
  // Enter moves on, as on any form (but not from a note, a button or a menu).
  onbEnter(e) {
    if (["TEXTAREA", "BUTTON", "SELECT"].includes(e.target.tagName)) return;
    if (this.onb.step >= 1 && this.onb.step <= 4) {
      e.preventDefault();
      this.onbNext();
    }
  },
  onbPickTrade(key) {
    this.onb.trade = this.onb.trade === key ? "" : key;
    if (key === "other" && this.onb.trade) this.$nextTick(() => this.$root.querySelector(".onb-other")?.focus());
  },
  async onbSave() {
    this.error = "";
    this.busy = true;
    try {
      const o = this.onb;
      const n = Number(o.max_instalments) || 1;
      const base = { ...DEFAULT_POLICY, ...(o._policy || {}) };
      const ws = await fetch("/api/workspace", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          business_name: o.business_name.trim(), owner_name: o.owner_name.trim(), trade: this.onbTradeLabel(),
          currency: o.currency, default_due_days: o.default_due_days, invoice_note: o.invoice_note.trim(),
          auto_chase: !!o.auto_chase, onboarded: true,
        }),
      });
      if (!ws.ok) {
        this.error = errorText(await ws.json().catch(() => ({})), "Couldn't save your business. Check the details.");
        return false;
      }
      const pol = await fetch("/api/policy", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          ...base, max_discount_pct: Number(o.max_discount_pct) || 0, floor_pct: Number(o.floor_pct) || 90,
          min_deposit_pct: Number(o.min_deposit_pct) || 0, max_instalments: n, auto_agree_within_policy: !o.ask_first,
          // A plan is fortnightly, so leave room for the last payment.
          max_plan_days: Math.max(Number(base.max_plan_days) || 60, (n - 1) * 14),
        }),
      });
      if (!pol.ok) {
        this.error = errorText(await pol.json().catch(() => ({})), "Couldn't save your deal rules. Check the numbers.");
        return false;
      }
      const w = await ws.json();
      this.businessName = w.business_name;
      this.invoiceNote = w.invoice_note || "";
      this.ownerName = w.owner_name || "";
      this.autoChase = !!w.auto_chase;
      Object.assign(window.SAYINVOICE, { onboarded: true, currency: w.currency, defaultDueDays: w.default_due_days,
                                        minDeposit: Number(o.min_deposit_pct) || 0, businessName: w.business_name,
                                        invoiceNote: this.invoiceNote });
      writeStore(ONB_DRAFT, null);
      return true;
    } catch {
      this.error = "No connection. Try again.";
      return false;
    } finally {
      this.busy = false;
    }
  },

  // The live preview: a sample job for their trade, in their currency, on their terms.
  onbTrade() {
    return TRADES.find((t) => t.key === this.onb.trade) || TRADES[0];
  },
  onbTradeLabel() {
    if (this.onb.trade === "other") return this.onb.trade_other.trim();
    return TRADES.find((t) => t.key === this.onb.trade)?.label || "";
  },
  onbFirstName() {
    return this.onb.owner_name.trim().split(/\s+/)[0] || "";
  },
  onbInitials() {
    const words = (this.onb.business_name.trim() || "Your business").split(/\s+/).filter((w) => /\p{L}|\d/u.test(w));
    return words.slice(0, 2).map((w) => [...w.replace(/^[^\p{L}\d]+/u, "")][0] || "").join("").toUpperCase() || "SI";
  },
  onbSym(code) {
    return this.currencySymbol(code || this.onb.currency);
  },
  onbMoney(cents) {
    return this.money(cents, this.onb.currency);
  },
  // Whole pounds -> cents in their currency, rounded to a price someone would really charge.
  onbAmount(units) {
    const scale = CURRENCY_SCALE[this.onb.currency] || 1;
    let v = units * scale;
    if (scale !== 1) {
      const step = 10 ** Math.max(0, Math.floor(Math.log10(v)) - 1);
      v = Math.round(v / step) * step;
    }
    return Math.round(v * 100);
  },
  onbHero() {
    const t = TRADES[0];
    const rate = this.onbAmount(t.rate), parts = this.onbAmount(t.parts), sym = this.onbSym();
    return { said: `“Fixed Mr Kennedy's boiler, ${t.qty} hours at ${sym}${rate / 100}, plus ${sym}${parts / 100} for a valve”`,
             labour: rate * t.qty, parts, total: rate * t.qty + parts };
  },
  onbJob() {
    const t = this.onbTrade();
    const rate = this.onbAmount(t.rate), parts = this.onbAmount(t.parts), labour = rate * t.qty;
    return {
      labour, parts, total: labour + parts,
      lines: [
        { name: t.labour, detail: `${t.qty} ${t.unit} × ${this.onbMoney(rate)}`, cents: labour },
        { name: t.part, detail: "Materials", cents: parts },
      ],
    };
  },
  onbDueLabel() {
    const d = Number(this.onb.default_due_days) || 0;
    return d ? `Due ${shortDate(daysFromNow(d))} · ${d} days` : "Due on receipt";
  },
  onbNoteIdeas() {
    const biz = this.onb.business_name.trim();
    return [biz ? `Thanks for choosing ${biz}!` : "Thanks for your business!", "Thanks for your business!",
            "It was a pleasure working with you."].filter((s, i, all) => all.indexOf(s) === i);
  },
  // What the Deal Agent would say to "any cheaper?", worked out the way PolicyGuard prices it:
  // the discount comes off labour only, and never takes the total under the secret floor.
  onbDeal() {
    const j = this.onbJob(), o = this.onb;
    const want = Math.max(0, Number(o.max_discount_pct) || 0);
    const room = j.labour ? ((j.total * (1 - (Number(o.floor_pct) || 0) / 100)) / j.labour) * 100 : 0;
    const pct = Math.max(0, Math.floor(Math.min(want, room)));
    const price = j.total - Math.round((j.labour * pct) / 100);
    const deposit = Math.round((price * (Number(o.min_deposit_pct) || 0)) / 100);
    const who = this.onbFirstName() || "the owner";
    if (o.ask_first) {
      return { pct, price, tone: "ask", reply: `Let me check with ${who} and come straight back to you.`,
               verdict: pct ? `You get a tap to approve up to ${pct}% off (${this.onbMoney(price)}).` : "You get a tap to decide." };
    }
    if (!pct) {
      return { pct, price, tone: "ask", reply: `That's already my best price, but I'll ask ${who} for you.`,
               verdict: want ? "Your lowest price leaves no room, so it comes to you." : "No discounts allowed, so it comes to you." };
    }
    const dep = deposit ? ` A ${this.onbMoney(deposit)} deposit books your date.` : "";
    return {
      pct, price, tone: "ok",
      reply: `I can take ${pct}% off the labour, so ${this.onbMoney(price)} in all. Materials stay as they are.${dep}`,
      verdict: pct < want ? `Held at ${pct}% by your lowest price. The client can accept it right away.`
                          : "Inside your rules, so the client can accept it right away.",
    };
  },
  onbPlan() {
    const n = Math.max(1, Number(this.onb.max_instalments) || 1), total = this.onbJob().total;
    const each = Math.floor(total / n);
    return Array.from({ length: n }, (_, i) => (i === n - 1 ? total - each * (n - 1) : each));
  },
  onbLadder() {
    const due = Number(this.onb.default_due_days) || 0;
    const at = (late) => shortDate(daysFromNow(due + late));
    return [
      { name: "Due", when: at(0), cls: "due" },
      { name: "Friendly", when: `1 day late · ${at(1)}`, cls: "r1" },
      { name: "Firm", when: `7 days late · ${at(7)}`, cls: "r2" },
      { name: "Final", when: `14 days late · ${at(14)}`, cls: "r3" },
    ];
  },
  greeting() {
    const h = new Date().getHours();
    const part = h < 5 ? "Working late" : h < 12 ? "Good morning" : h < 18 ? "Good afternoon" : "Good evening";
    const name = this.ownerName.trim().split(/\s+/)[0];
    return name ? `${part}, ${name}` : part;
  },
  examples() {
    const t = this.onbTrade(), sym = this.onbSym(), n = (units) => this.onbAmount(units) / 100;
    const dep = Number(this.onb.min_deposit_pct) || 0;
    return [
      { label: "An invoice", text: `${t.said}, ${t.qty} ${t.unit} at ${sym}${n(t.rate)}, plus ${sym}${n(t.parts)} for ${t.partSaid}, email kennedy@example.com` },
      { label: "A quote", text: `${t.quoteSaid}, ${sym}${n(t.quoteLabour)} labour plus about ${sym}${n(t.quoteParts)} for materials${dep ? `, ${dep}% deposit` : ""}` },
      { label: "In another language", text: "Factura para Carmen López, limpieza de oficina, cuatro horas a 15 euros, carmen@example.es" },
    ];
  },
  tryExample(text) {
    this.reset();
    this.typing = true;
    this.typed = text;
    this.$nextTick(() => this.$refs.jobText?.focus());
  },
  finishOnboarding() {
    this.reset();
  },

  // ── voice read-back ───────────────────────────────────────────────────
  toggleReadBack() {
    this.readBack = !this.readBack;
    writeStore("si.readback", this.readBack);
    if (!this.readBack) this.stopVoice();
  },
  // iOS only lets a page talk after a tap: say nothing, once, inside a tap.
  primeSpeech() {
    try {
      if ("speechSynthesis" in window && !this._primed) {
        window.speechSynthesis.speak(new SpeechSynthesisUtterance(""));
        this._primed = true;
      }
      if (!this.audioEl) this.audioEl = new Audio();
      // The server voice plays through this element. Playing a moment of silence inside the
      // tap unlocks it on iOS and starts the audio output now, not on the first read-back.
      if (window.SAYINVOICE.ttsEngine === "kokoro" && !this._audioPrimed) {
        this._audioPrimed = true;
        this.audioEl.src = silentWav();
        this.audioEl.play().catch(() => { this._audioPrimed = false; });
      }
    } catch {
      /* no speech on this device */
    }
  },
  speechLang() {
    const lang = (this.language || "en").toLowerCase();
    return lang === "en" ? (navigator.language?.startsWith("en") ? navigator.language : "en-GB") : lang;
  },
  readBackText() {
    if (this.question) return this.question;
    if (!this.canSend()) return this.blocker();
    const who = this.card.client_name.trim() || this.card.client_email.trim() || "the client";
    if (this.mode === "quote") {
      const dep = this.depositCents() > 0 ? `, with a ${this.depositPct()} percent deposit` : "";
      return `Quote for ${this.money(this.totalCents())} to ${who}${dep}. Say send to create the link, or change to edit it.`;
    }
    if (this.mode === "final") {
      const paid = this.depositPaidCents() > 0 ? ` after the ${this.money(this.depositPaidCents())} deposit` : "";
      return `Final invoice for ${who}: ${this.money(this.finalBalanceCents())} to pay${paid}. Say send to send it, or change to edit it.`;
    }
    const due = this.card.due_date
      ? `due ${new Date(this.card.due_date).toLocaleDateString("en-GB", { weekday: "long", day: "numeric", month: "long" })}`
      : this.dueHint().replace("Empty means ", "");
    return `Invoice for ${this.money(this.totalCents())} to ${who}, ${due}. Say send to send it, or change to edit it.`;
  },
  async speak(text) {
    this.voiceState = "speaking";
    // Kokoro on the server when it's turned on; the phone's own voice otherwise, or for
    // whatever the server couldn't say.
    if (window.SAYINVOICE.ttsEngine === "kokoro") {
      text = await this.speakOnServer(text);
      if (!text || this.voiceState !== "speaking") return;
    }
    if (!("speechSynthesis" in window)) return;
    // Whatever the device's voice does (errors, never says "end"), this always finishes.
    await new Promise((resolve) => {
      try {
        const u = new SpeechSynthesisUtterance(text);
        u.lang = this.speechLang();
        const voice = window.speechSynthesis.getVoices().find((v) => v.lang?.toLowerCase().startsWith(u.lang.slice(0, 2).toLowerCase()));
        if (voice) u.voice = voice;
        u.rate = 1.02;
        u.onend = u.onerror = resolve;
        window.speechSynthesis.cancel();
        window.speechSynthesis.speak(u);
        setTimeout(resolve, 15000);
      } catch {
        resolve();
      }
    });
  },
  // Kokoro, a sentence at a time: the next sentence is made while this one plays, so the
  // first words come after one short sentence, not the whole read-back. Returns what's
  // left for the phone's voice ("" when it all got said): 503 while the server voice warms
  // up, 404 when it's off, or a slow answer.
  async speakOnServer(text) {
    const parts = text.split(/(?<=[.!?])\s+/).filter((p) => p.trim());
    // Words sooner: the first sentence plays from its first comma while the rest is made.
    const comma = parts[0]?.indexOf(", ") ?? -1;
    if (comma > 15 && parts[0].length - comma > 12) parts.splice(0, 1, parts[0].slice(0, comma + 1), parts[0].slice(comma + 2));
    const lang = this.speechLang();
    const fetchPart = (part) => {
      const ctrl = new AbortController();
      const timer = setTimeout(() => ctrl.abort(), 8000);
      return fetch(`/api/tts?${new URLSearchParams({ text: part, lang })}`, { signal: ctrl.signal })
        .then((r) => (r.ok ? r.blob() : null))
        .catch(() => null)
        .finally(() => clearTimeout(timer));
    };
    const el = this.audioEl || (this.audioEl = new Audio());
    let next = fetchPart(parts[0]);
    for (let i = 0; i < parts.length; i++) {
      const blob = await next;
      if (this.voiceState !== "speaking") return "";
      if (!blob) return parts.slice(i).join(" ");
      if (i + 1 < parts.length) next = fetchPart(parts[i + 1]);
      const url = URL.createObjectURL(blob);
      el.src = url;
      // pause counts as done too: Stop pauses it, and a paused clip never "ends".
      await new Promise((resolve) => {
        el.onended = el.onerror = el.onpause = resolve;
        el.play().catch(resolve);
      });
      URL.revokeObjectURL(url);
    }
    return "";
  },
  // Listen for one short spoken reply. (Not `listen()`: that name is the live-updates stream.)
  hear() {
    const Rec = window.SpeechRecognition || window.webkitSpeechRecognition;
    if (!Rec) return Promise.resolve("");
    return new Promise((resolve) => {
      const rec = new Rec();
      this.recognizer = rec;
      rec.lang = this.speechLang();
      rec.interimResults = false;
      rec.maxAlternatives = 3;
      let said = "";
      rec.onresult = (e) => { said = Array.from(e.results[0]).map((a) => a.transcript).join(" | "); };
      rec.onend = rec.onerror = () => resolve(said);
      try { rec.start(); } catch { resolve(""); }
      setTimeout(() => { try { rec.stop(); } catch { /* already stopped */ } }, 6000);
    });
  },
  async readItBack() {
    if (this.voiceState !== "idle" || !this.canSpeak) return;
    this.primeSpeech();
    try {
      await this.speak(this.readBackText());
    } catch {
      this.voiceState = "idle"; // a broken voice must never leave the card stuck
      return;
    }
    if (this.voiceState !== "speaking") return; // stopped meanwhile
    if (!this.canSend() || !this.canListen) {
      this.voiceState = "idle";
      return;
    }
    this.voiceState = "listening";
    let said = "";
    try {
      said = await this.hear();
    } catch {
      said = "";
    }
    if (this.voiceState !== "listening") return;
    this.heard = said.split(" | ")[0] || "";
    // "Stop" beats "send": a muddled "send, no wait" never sends.
    if (said && !SAY_STOP.test(said) && SAY_SEND.test(said)) return this.startCountdown();
    this.voiceState = "idle";
  },
  autoReadBack() {
    if (this.readBack && this.screen === "preview") setTimeout(() => this.readItBack(), 250);
  },
  startCountdown() {
    this.voiceState = "countdown";
    this.countdown = SEND_COUNTDOWN;
    clearInterval(this.countdownTimer);
    this.countdownTimer = setInterval(() => {
      this.countdown -= 1;
      if (this.countdown <= 0) {
        clearInterval(this.countdownTimer);
        this.voiceState = "idle";
        if (this.canSend() && !this.busy) this.send();
      }
    }, 1000);
  },
  stopVoice() {
    clearInterval(this.countdownTimer);
    try { this.recognizer?.abort(); } catch { /* not listening */ }
    try { window.speechSynthesis?.cancel(); } catch { /* not speaking */ }
    try { this.audioEl?.pause(); } catch { /* no audio */ }
    this.voiceState = "idle";
    this.countdown = 0;
  },

  // ── demo clock ────────────────────────────────────────────────────────
  async loadClock() {
    try {
      const resp = await fetch("/api/clock");
      if (resp.ok) this.clockInfo = await resp.json();
    } catch {
      /* no clock: real time */
    }
  },
  async setClock(days) {
    this.clockBusy = true;
    try {
      const resp = await fetch("/api/clock", {
        method: "PUT", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ offset_days: Math.max(0, Math.min(120, days)) }),
      });
      if (resp.ok) this.clockInfo = await resp.json();
      // Overdue, the Chase Agent and job pages all follow the clock.
      await Promise.all([this.loadHome(), this.loadReminders(), this.loadJobs()]);
      if (this.screen === "detail") await this.refreshDetail();
    } finally {
      this.clockBusy = false;
    }
  },

  // ── chase agent ───────────────────────────────────────────────────────
  async loadReminders() {
    try {
      const resp = await fetch("/api/reminders");
      if (resp.ok) this.reminders = await resp.json();
    } catch {
      /* next refresh */
    }
  },
  chaseLine() {
    const s = this.chaseSummary;
    const n = this.reminders.length;
    if (n) return `${n} reminder${n === 1 ? "" : "s"} waiting for your OK`;
    if (s) {
      if (!s.due) return "Checked just now: nobody needs a reminder yet.";
      return s.auto ? `Sent ${s.sent} reminder${s.sent === 1 ? "" : "s"} just now.` : "Drafts ready below.";
    }
    return "Checks overdue invoices every hour: friendly, then firm, then final.";
  },
  async runChase() {
    this.chaseRunning = true;
    this.chaseError = "";
    try {
      const resp = await fetch("/api/chase/run", { method: "POST" });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.chaseError = errorText(body, "The Chase Agent couldn't run just now.");
        return;
      }
      this.chaseSummary = body;
      await Promise.all([this.loadReminders(), this.loadHome()]);
    } catch {
      this.chaseError = "No connection. Try again.";
    } finally {
      this.chaseRunning = false;
    }
  },
  async sendReminder(r) {
    this.chaseBusy = r.id;
    this.chaseError = "";
    const edited = this.editingReminder === r.id ? this.reminderText : null;
    try {
      const resp = await fetch(`/api/reminders/${r.id}/send`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify(edited !== null ? { note: edited } : {}),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) this.chaseError = errorText(body, "Couldn't send it. Try again.");
      this.editingReminder = null;
      await Promise.all([this.loadReminders(), this.loadHome()]);
    } catch {
      this.chaseError = "No connection. Try again.";
    } finally {
      this.chaseBusy = null;
    }
  },
  async skipReminder(r) {
    this.chaseBusy = r.id;
    try {
      await fetch(`/api/reminders/${r.id}/skip`, { method: "POST" });
      await this.loadReminders();
    } finally {
      this.chaseBusy = null;
    }
  },
  async saveChase() {
    try {
      const resp = await fetch("/api/chase/settings", {
        method: "PUT", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ auto_chase: this.autoChase }),
      });
      if (!resp.ok) throw new Error();
    } catch {
      this.autoChase = !this.autoChase;
    }
  },

  // ── who owes me ───────────────────────────────────────────────────────
  async loadHome() {
    try {
      const resp = await fetch("/api/home");
      if (resp.ok) this.home = await resp.json();
      this.loadFailed.home = !resp.ok;
    } catch {
      this.loadFailed.home = true; // keep the last view, if there is one
    }
  },
  async openMoney() {
    this.screen = "money";
    this.actionMsg = "";
    this.loadReminders();
    await this.loadHome();
    if (this.home && this.tab === "owed" && !this.home.tabs.owed.length && this.home.tabs.overdue.length) this.tab = "overdue";
  },
  async openDetail(kind, id) {
    this.detailRef = { kind, id };
    this.unsnooze(kind, id);
    this.actionMsg = "";
    this.actionError = "";
    this.cancelArmed = false;
    if (this.detail && !(this.detail.kind === kind && this.detail.id === id)) this.detail = null;
    this.screen = "detail";
    window.scrollTo(0, 0);
    await this.refreshDetail();
  },
  async refreshDetail() {
    if (!this.detailRef) return;
    try {
      const resp = await fetch(`/api/jobs/${this.detailRef.kind}/${this.detailRef.id}`);
      if (resp.ok) this.detail = await resp.json();
      this.loadFailed.detail = !resp.ok;
    } catch {
      this.loadFailed.detail = true; // keep what's on screen
    }
  },
  stateLabel(d) {
    if (d.kind === "quote") {
      return { sent: "Waiting for the client", negotiating: "Negotiating", awaiting_owner: "Needs your answer",
               accepted: "Booked", declined: "Declined", expired: "Expired" }[d.state] || d.state;
    }
    if (d.state === "overdue") return `${d.days_overdue} day${d.days_overdue === 1 ? "" : "s"} overdue`;
    return { owed: "Waiting for payment", draft: "Not sent", paid: "Paid", cancelled: "Cancelled", refunded: "Refunded" }[d.state] || d.state;
  },
  when(iso) {
    return new Date(iso).toLocaleString(undefined, { day: "numeric", month: "short", hour: "numeric", minute: "2-digit" });
  },
  async act(kind) {
    if (!this.detail || this.acting) return;
    this.acting = true;
    this.actionError = "";
    this.actionMsg = "";
    try {
      const resp = await fetch(`/api/invoices/${this.detail.id}/${kind}`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: kind === "retry" ? undefined : "{}",
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.actionError = errorText(body, "That didn't work. Try again.");
        return;
      }
      this.actionMsg = { remind: "Reminder sent ✓", retry: "Sent ✓", cancel: "Invoice cancelled" }[kind] || "Done ✓";
      await this.refreshDetail();
      this.loadHome();
    } catch {
      this.actionError = "No connection. Try again.";
    } finally {
      this.acting = false;
    }
  },
  cancelInvoice() {
    if (!this.cancelArmed) {
      this.cancelArmed = true;
      setTimeout(() => (this.cancelArmed = false), 4000);
      return;
    }
    this.cancelArmed = false;
    this.act("cancel");
  },
  openPay() {
    this.pay = { amount: String(Number(this.detail.balance)), method: "CASH" };
    this.actionError = "";
    this.payOpen = true;
  },
  async confirmPay() {
    const amount = num(this.pay.amount);
    if (!(amount > 0)) {
      this.actionError = "Enter the amount received.";
      return;
    }
    this.acting = true;
    this.actionError = "";
    try {
      const resp = await fetch(`/api/invoices/${this.detail.id}/record-payment`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ amount: amount.toFixed(2), method: this.pay.method }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.actionError = errorText(body, "Couldn't record it. Try again.");
        return;
      }
      this.payOpen = false;
      this.actionMsg = body.status === "paid" ? "Marked as paid ✓" : "Part payment recorded ✓";
      await this.refreshDetail();
      this.loadHome();
    } catch {
      this.actionError = "No connection. Try again.";
    } finally {
      this.acting = false;
    }
  },
  detailQr() {
    const d = this.detail;
    this.invoice = { id: d.id, total: d.balance, currency: d.currency, status: d.state === "overdue" ? "sent" : "sent", payer_view_url: d.pay_url };
    this.openQr();
  },
  async copyText(text) {
    try {
      await navigator.clipboard.writeText(text);
      this.copied = true;
      setTimeout(() => (this.copied = false), 2500);
    } catch {
      window.prompt("Copy this link:", text);
    }
  },
  finishFromDetail() {
    const job = this.jobs.find((j) => j.quote_id === this.detail.id);
    if (job) this.finishJob(job);
  },

  // ── finishing booked jobs ─────────────────────────────────────────────
  async loadJobs() {
    try {
      const resp = await fetch("/api/jobs/to-finish");
      if (resp.ok) this.jobs = await resp.json();
    } catch {
      /* the list is a convenience; the mic still works */
    }
  },
  jobLine(j) {
    const bits = [j.number, this.money(this.toCents(j.total), j.currency)];
    const d = j.deposit;
    if (!d) bits.push("no deposit");
    else if (d.status === "paid") bits.push("deposit paid");
    else if (d.status === "cancelled") bits.push("deposit cancelled");
    else bits.push("deposit due");
    if (j.job_date) bits.push(new Date(j.job_date).toLocaleDateString(undefined, { weekday: "short", day: "numeric", month: "short" }));
    return bits.join(" · ");
  },
  finishJob(j) {
    this.job = j;
    this.mode = "final";
    this.card = {
      client_name: j.client_name, client_email: j.client_email, currency: j.currency,
      due_date: "", notes: "", deposit_pct: "", job_date: "",
      items: j.lines.map((ln) => ({
        key: newKey(), name: ln.name, description: ln.description,
        quantity: String(Number(ln.quantity)), unit_price: String(Number(ln.unit_price)),
        unit: ln.unit, is_material: ln.is_material, approximate: false,
      })),
    };
    Object.assign(this, {
      flags: [], missing: [], question: "", warnings: [], transcript: "", answer: "", error: "",
      draft: { transcript: "", language: "en" }, key: newKey(), matchedJob: null, screen: "preview",
    });
  },
  async useJob(j) {
    const said = this.transcript;
    this.finishJob(j);
    // Re-read what was said against the agreed quote: "used a £45 cartridge" becomes a swap.
    if (said) await this.submitAnswer({ text: said });
  },
  matchJob(result) {
    const name = (result.client_name || "").toLowerCase().replace(/^(mr|mrs|ms|miss)\.?\s+/, "").trim();
    const email = (result.client_email || "").toLowerCase();
    // The most recently booked first: a repeat client's latest job is the likely one.
    return [...this.jobs].reverse().find((j) => {
      const jn = (j.client_name || "").toLowerCase();
      return (email && j.client_email === email) || (name.length > 2 && jn.includes(name));
    }) || null;
  },
  agreedDiscountCents() {
    const pct = Number(this.job?.labour_discount_pct || 0);
    return Math.round((this.labourCents() * pct) / 100);
  },
  depositPaidCents() {
    return this.toCents(this.job?.deposit?.paid || 0);
  },
  depositUnpaid() {
    const d = this.job?.deposit;
    return !!d && ["sent", "overdue"].includes(d.status) && this.toCents(d.paid) === 0;
  },
  finalBalanceCents() {
    return this.totalCents() - this.agreedDiscountCents() - this.depositPaidCents();
  },
  async sendFinal() {
    if (!this.canSend() || this.busy) return;
    this.busy = true;
    this.error = "";
    const dec = (v) => (Math.round(num(v) * 100) / 100).toFixed(2);
    const payload = {
      client_name: this.card.client_name.trim(),
      client_email: this.card.client_email.trim(),
      items: this.card.items.map((i) => ({
        name: i.name.trim(), description: (i.description || "").trim(),
        quantity: dec(i.quantity), unit_price: dec(i.unit_price), unit: i.unit, is_material: i.is_material,
      })),
      due_date: this.card.due_date || null,
      notes: this.card.notes,
      transcript: this.transcript,
      draft: this.draft,
    };
    try {
      const resp = await fetch(`/api/quotes/${this.job.quote_id}/final-invoice`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.error = errorText(body, "Couldn't send it. Try again.");
        return;
      }
      this.invoice = body;
      this.screen = "sent";
      this.startChecking();
      this.loadJobs();
    } catch {
      this.error = "No connection. Tap Send again when you have signal; it won't send twice.";
    } finally {
      this.busy = false;
    }
  },

  async loadApprovals() {
    try {
      const resp = await fetch("/api/approvals");
      if (resp.ok) this.approvals = await resp.json();
    } catch {
      /* live ones still arrive over SSE */
    }
  },
  onApproval(a) {
    if (this.approvals.some((x) => x.message_id === a.message_id)) return;
    this.approvals = [...this.approvals, a];
    buzz([120, 80, 120]);
  },
  newWork() {
    return (this.approval?.priced?.add_requests || []).length > 0 || (this.approval?.violations || []).includes("needs_pricing");
  },
  startCounter() {
    const a = this.approval;
    const pct = a?.priced ? Number(a.priced.discount_pct) : 0;
    this.counter = { discount: String(Math.min(pct || 5, 50)), deposit: a?.priced ? String(Number(a.priced.deposit_pct)) : "", note: "" };
    this.countering = true;
  },
  async decide(action) {
    const a = this.approval;
    if (!a) return;
    this.deciding = true;
    this.approvalError = "";
    const body = { action, note: ["counter", "reply"].includes(action) ? this.counter.note : "" };
    if (action === "counter") {
      const d = num(this.counter.discount), dep = num(this.counter.deposit);
      if (!(d >= 0 && d <= 100)) { this.approvalError = "Discount should be 0 to 100%."; this.deciding = false; return; }
      body.labour_discount_pct = d.toFixed(2);
      if (String(this.counter.deposit).trim() !== "") body.deposit_pct = dep.toFixed(2);
    }
    try {
      const url = a.kind === "plan"
        ? `/api/invoices/${a.invoice_id}/plan-messages/${a.message_id}/decision`
        : `/api/quotes/${a.quote_id}/messages/${a.message_id}/decision`;
      const resp = await fetch(url, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const out = await resp.json().catch(() => ({}));
      if (!resp.ok && resp.status !== 409) {
        this.approvalError = errorText(out, "Couldn't send that. Try again.");
        return;
      }
      // Done (or already answered elsewhere): next card. By id, since "Later" can
      // put the one on screen anywhere in the list.
      this.approvals = this.approvals.filter((x) => x.message_id !== a.message_id);
      this.countering = false;
      this.replying = false;
    } catch {
      this.approvalError = "No connection. Try again.";
    } finally {
      this.deciding = false;
    }
  },

  // ── live updates ──────────────────────────────────────────────────────
  listen() {
    if (!window.EventSource) return;
    // EventSource reconnects on its own after a dropped signal.
    const source = new EventSource("/api/events");
    source.addEventListener("invoice", (e) => this.onInvoice(JSON.parse(e.data)));
    source.addEventListener("quote", (e) => this.onQuote(JSON.parse(e.data)));
    source.addEventListener("approval", (e) => this.onApproval(JSON.parse(e.data)));
    source.addEventListener("reminder", () => this.loadReminders());
  },

  onInvoice(update) {
    this.liveRefresh();
    if (!this.invoice || update.id !== this.invoice.id) return;
    const wasPaid = this.invoice.status === "paid";
    Object.assign(this.invoice, {
      status: update.status,
      amount_paid: update.amount_paid,
      paid_at: update.paid_at,
    });
    if (SETTLED.includes(update.status)) this.stopChecking();
    if (update.status === "paid" && !wasPaid) buzz([80, 60, 160]);
  },

  startChecking() {
    this.stopChecking();
    this.checkTimer = setInterval(() => this.checkNow(), CHECK_EVERY_MS);
  },
  stopChecking() {
    clearInterval(this.checkTimer);
    this.checkTimer = null;
  },
  async checkNow() {
    if (!this.invoice) return;
    try {
      const resp = await fetch(`/api/invoices/${this.invoice.id}/refresh`, { method: "POST" });
      if (resp.ok) this.onInvoice(await resp.json());
    } catch {
      /* offline for a moment; the next tick or the live stream will catch up */
    }
  },

  paidLabel() {
    const inv = this.invoice;
    if (!inv) return "";
    if (inv.status === "paid") return "Paid";
    if (inv.status === "partially_paid") {
      return `${this.money(this.toCents(inv.amount_paid), inv.currency)} of ${this.money(this.toCents(inv.total), inv.currency)} paid`;
    }
    if (inv.status === "cancelled") return "Cancelled";
    return "Waiting for payment…";
  },

  // ── recording ─────────────────────────────────────────────────────────
  async toggleRecording(target) {
    this.primeSpeech();
    if (this.recording) return this.stopRecording();
    this.error = "";
    if (!navigator.mediaDevices?.getUserMedia || !window.MediaRecorder) {
      this.typing = true;
      this.error = "This browser can't record audio. Type it instead.";
      return;
    }
    let stream;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true },
      });
    } catch {
      this.typing = true;
      this.error = "Allow the microphone to talk, or type it instead.";
      return;
    }
    // Chrome/Android record webm; iOS Safari records mp4. Gemini reads both.
    const mime = ["audio/webm;codecs=opus", "audio/mp4", "audio/webm", "audio/ogg;codecs=opus"].find(
      (t) => MediaRecorder.isTypeSupported(t),
    );
    const recorder = new MediaRecorder(stream, mime ? { mimeType: mime } : undefined);
    const chunks = [];
    recorder.ondataavailable = (e) => e.data.size && chunks.push(e.data);
    recorder.onstop = () => {
      stream.getTracks().forEach((t) => t.stop());
      const type = recorder.mimeType || mime || "audio/webm";
      const blob = new Blob(chunks, { type });
      if (this.seconds < 1 || blob.size < 1000) {
        this.error = "That was too short. Tap, speak, then tap again.";
        return;
      }
      this.submitAudio(blob, target);
    };
    this.recorder = recorder;
    this.seconds = 0;
    this.recording = true;
    recorder.start();
    this.timer = setInterval(() => {
      this.seconds += 1;
      if (this.seconds >= MAX_RECORD_SECONDS) this.stopRecording();
    }, 1000);
  },

  stopRecording() {
    clearInterval(this.timer);
    this.recording = false;
    if (this.recorder?.state === "recording") this.recorder.stop();
  },

  clock(s) {
    return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
  },

  // ── parsing ───────────────────────────────────────────────────────────
  async callParse({ text = "", blob = null, previous = null }) {
    const form = new FormData();
    if (blob) form.append("audio", blob, blob.type.includes("mp4") ? "note.m4a" : "note.webm");
    if (text) form.append("text", text);
    // "auto": say "quote" and the card opens as a quote; the switch on the card can change it.
    form.append("mode", previous ? (this.mode === "quote" ? "quote" : "invoice") : "auto");
    form.append("tz", Intl.DateTimeFormat().resolvedOptions().timeZone || "");
    if (previous) form.append("previous", JSON.stringify(previous));
    let resp;
    try {
      resp = await fetch("/api/parse", { method: "POST", body: form });
    } catch {
      throw new Error("No connection. Check your signal and try again.");
    }
    const body = await resp.json().catch(() => ({}));
    if (!resp.ok) throw new Error(errorText(body, "Couldn't read that. Try again, or type it."));
    return body;
  },

  async submitAudio(blob, target) {
    if (target === "answer") return this.submitAnswer({ blob });
    await this.readJob({ blob });
  },

  async parseTyped() {
    this.primeSpeech();
    await this.readJob({ text: this.typed.trim() });
  },

  async readJob(input) {
    this.screen = "reading";
    this.error = "";
    try {
      this.loadCard(await this.callParse(input));
      this.screen = "preview";
      this.autoReadBack();
    } catch (e) {
      this.screen = "record";
      this.error = e.message;
      // "Type it instead" is the fallback, so open the box (for a failed recording too).
      this.typing = true;
      this.$nextTick(() => this.$refs.jobText?.focus());
    }
  },

  async answerTyped() {
    if (this.answer.trim()) await this.submitAnswer({ text: this.answer.trim() });
  },

  async submitAnswer(input) {
    this.busy = true;
    this.error = "";
    try {
      // Send the card as edited, so hand fixes survive the model's update.
      this.loadCard(await this.callParse({ ...input, previous: this.cardToDraft() }), { keepKey: true });
      this.answer = "";
      this.autoReadBack();
    } catch (e) {
      this.error = e.message;
    } finally {
      this.busy = false;
    }
  },

  loadCard(result, { keepKey = false } = {}) {
    // A follow-up on a final invoice stays a final invoice.
    if (!(keepKey && this.mode === "final")) this.mode = result.mode;
    this.language = result.language || "en";
    this.clientSource = result.client_source || "";
    this.suggestions = result.client_suggestions || [];
    this.matchedJob = this.mode === "invoice" && !keepKey ? this.matchJob(result) : this.matchedJob;
    this.card = {
      deposit_pct: result.deposit_pct != null ? String(Number(result.deposit_pct)) : "",
      language: result.client_language || (result.language || "en").split("-")[0],
      job_date: "",
      client_name: result.client_name,
      client_email: result.client_email,
      currency: result.currency,
      due_date: result.due_date || "",
      notes: result.notes,
      items: result.items.map((i) => ({
        key: newKey(),
        name: i.name,
        description: i.description,
        quantity: String(Number(i.quantity)),
        unit_price: Number(i.unit_price) > 0 ? String(Number(i.unit_price)) : "",
        unit: i.unit,
        is_material: i.is_material,
        approximate: i.approximate,
      })),
    };
    this.flags = [...result.uncertain_fields, ...result.missing_fields];
    this.missing = result.missing_fields;
    this.question = result.follow_up_question;
    this.warnings = result.warnings;
    this.transcript = result.transcript;
    this.draft = result.draft;
    if (!keepKey || !this.key) this.key = newKey();
  },

  // The edited card in the parser's draft shape, for a follow-up answer.
  cardToDraft() {
    return {
      ...this.draft,
      mode: this.mode === "quote" ? "quote" : "invoice",
      deposit_pct: num(this.card.deposit_pct) >= 0 ? num(this.card.deposit_pct) : null,
      deposit_amount: null,
      client_name: this.card.client_name,
      client_email: this.card.client_email,
      currency: this.card.currency,
      due_date: this.card.due_date || null,
      due_in_days: null,
      due_weekday: null,
      due_weeks_ahead: null,
      notes: this.card.notes,
      items: this.card.items.map((i) => ({
        name: i.name,
        description: i.description || "",
        quantity: num(i.quantity) || 0,
        unit_price: num(i.unit_price) || 0,
        unit: i.unit,
        is_material: i.is_material,
        approximate: i.approximate,
      })),
      missing_fields: this.missing,
      uncertain_fields: [],
      follow_up_question: this.question,
    };
  },

  // ── client memory ─────────────────────────────────────────────────────
  pickClient(c) {
    Object.assign(this.card, { client_name: c.name, client_email: c.email, language: c.language || this.card.language });
    this.clientSource = "saved";
    this.suggestions = [];
    this.confirmField("client_email");
    this.missing = this.missing.filter((f) => f !== "client_email");
    if (this.question && !this.missing.length) this.question = "";
  },
  lookupClients() {
    clearTimeout(this._clientTimer);
    const q = this.card.client_name.trim();
    if (q.length < 2) return;
    this._clientTimer = setTimeout(async () => {
      try {
        const resp = await fetch(`/api/clients?q=${encodeURIComponent(q)}`);
        if (resp.ok) this.clientOptions = await resp.json();
      } catch {
        /* suggestions are a nicety */
      }
    }, 200);
  },
  useTypedClient() {
    const typed = this.card.client_name.trim().toLowerCase();
    const c = this.clientOptions.find((o) => o.name.toLowerCase() === typed);
    if (c && !this.card.client_email) this.pickClient(c);
  },

  // ── preview card ──────────────────────────────────────────────────────
  flagged(path) {
    return this.flags.includes(path);
  },
  flagClass(path) {
    return { flag: this.flagged(path) };
  },
  confirmField(path) {
    // Touching a field means the tradesperson has checked it.
    this.flags = this.flags.filter((f) => f !== path);
  },

  addItem() {
    this.card.items.push({
      key: newKey(), name: "", description: "", quantity: "1", unit_price: "",
      unit: "items", is_material: false, approximate: false,
    });
  },
  removeItem(n) {
    this.card.items.splice(n, 1);
    // Flags are positional ("items.2.unit_price"): drop the item-level ones.
    this.flags = this.flags.filter((f) => !f.startsWith("items."));
  },

  toCents(value) {
    return Math.round(Number(value) * 100);
  },
  lineCents(item) {
    const q = num(item.quantity), p = num(item.unit_price);
    if (!(q > 0) || !(p > 0)) return 0;
    return Math.round((Math.round(q * 100) * Math.round(p * 100)) / 100);
  },
  totalCents() {
    return this.card.items.reduce((sum, i) => sum + this.lineCents(i), 0);
  },
  labourCents() {
    return this.card.items.filter((i) => !i.is_material).reduce((s, i) => s + this.lineCents(i), 0);
  },
  materialsCents() {
    return this.card.items.filter((i) => i.is_material).reduce((s, i) => s + this.lineCents(i), 0);
  },
  currencySymbol(currency) {
    const code = (currency || this.card.currency || window.SAYINVOICE.currency || "GBP").toUpperCase();
    try {
      const parts = new Intl.NumberFormat(undefined, { style: "currency", currency: code, currencyDisplay: "narrowSymbol" }).formatToParts(0);
      return parts.find((p) => p.type === "currency")?.value || code;
    } catch {
      return code;
    }
  },
  currencyChoices() {
    // Keep whatever the parser heard (even one PayPal can't take) so the warning makes sense.
    const cur = (this.card.currency || "").toUpperCase();
    return cur && !PAYPAL_CURRENCIES.includes(cur) ? [cur, ...PAYPAL_CURRENCIES] : PAYPAL_CURRENCIES;
  },
  money(cents, currency) {
    const code = (currency || this.card.currency || window.SAYINVOICE.currency || "GBP").toUpperCase();
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency: code }).format(cents / 100);
    } catch {
      return `${code} ${(cents / 100).toFixed(2)}`;
    }
  },
  dueHint() {
    const days = window.SAYINVOICE.defaultDueDays;
    return days > 0 ? `Empty means due in ${days} days` : "Empty means due on receipt";
  },

  depositPct() {
    const v = num(this.card.deposit_pct);
    return Number.isFinite(v) && String(this.card.deposit_pct).trim() !== "" ? v : window.SAYINVOICE.minDeposit;
  },
  depositCents() {
    return Math.round((this.totalCents() * this.depositPct()) / 100);
  },
  depositHint() {
    return String(this.card.deposit_pct).trim() === ""
      ? `Empty means your minimum (${window.SAYINVOICE.minDeposit}%)`
      : "Taken by PayPal when the client accepts";
  },

  blocker() {
    if (!this.card.items.length) return "Add at least one line.";
    const bad = this.card.items.find((i) => !i.name.trim() || !(num(i.quantity) > 0) || !(num(i.unit_price) > 0));
    if (bad) return bad.name.trim() ? `Add a price for “${bad.name.trim()}”.` : "Every line needs a name.";
    if (this.mode === "quote") {
      const email = this.card.client_email.trim();
      if (email && !PAY_EMAIL.test(email)) return "That email doesn't look right.";
      if (!email && !this.card.client_name.trim()) return "Who is the quote for? Add a name or email.";
      const d = num(this.card.deposit_pct);
      if (String(this.card.deposit_pct).trim() !== "" && !(d >= 0 && d <= 100)) return "Deposit should be 0 to 100%.";
    } else if (!PAY_EMAIL.test(this.card.client_email)) {
      return "Add the client's email so PayPal can send it.";
    }
    if (this.mode === "final" && this.finalBalanceCents() <= 0) return "The deposit already covers this job.";
    if (!/^[A-Za-z]{3}$/.test(this.card.currency)) return "Currency should be a 3-letter code, like GBP.";
    return "";
  },
  canSend() {
    return !this.blocker();
  },

  // ── send ──────────────────────────────────────────────────────────────
  async send() {
    this.stopVoice();
    if (this.mode === "quote") return this.createQuote();
    if (this.mode === "final") return this.sendFinal();
    if (!this.canSend() || this.busy) return;
    this.busy = true;
    this.error = "";
    const dec = (v) => (Math.round(num(v) * 100) / 100).toFixed(2);
    const payload = {
      idempotency_key: this.key,
      client_name: this.card.client_name.trim(),
      client_email: this.card.client_email.trim(),
      currency: this.card.currency.trim().toUpperCase(),
      items: this.card.items.map((i) => ({
        name: i.name.trim(),
        description: (i.description || "").trim(),
        quantity: dec(i.quantity),
        unit_price: dec(i.unit_price),
        unit: i.unit,
        is_material: i.is_material,
      })),
      due_date: this.card.due_date || null,
      notes: this.card.notes,
      transcript: this.transcript,
      draft: this.draft,
      language: this.card.language || "en",
    };
    try {
      const resp = await fetch("/api/invoices", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.error = errorText(body, "Couldn't send it. Try again.");
        return;
      }
      this.invoice = body;
      this.screen = "sent";
      this.startChecking();
    } catch {
      // Same key on retry: the server never sends the same invoice twice.
      this.error = "No connection. Tap Send again when you have signal; it won't send twice.";
    } finally {
      this.busy = false;
    }
  },

  // ── quotes ────────────────────────────────────────────────────────────
  async createQuote() {
    if (!this.canSend() || this.busy) return;
    this.busy = true;
    this.error = "";
    const dec = (v) => (Math.round(num(v) * 100) / 100).toFixed(2);
    const payload = {
      idempotency_key: this.key,
      client_name: this.card.client_name.trim(),
      client_email: this.card.client_email.trim() || null,
      currency: this.card.currency.trim().toUpperCase(),
      items: this.card.items.map((i) => ({
        name: i.name.trim(), description: (i.description || "").trim(),
        quantity: dec(i.quantity), unit_price: dec(i.unit_price), unit: i.unit, is_material: i.is_material,
      })),
      deposit_pct: String(this.card.deposit_pct).trim() === "" ? null : dec(this.card.deposit_pct),
      job_date: this.card.job_date || null,
      notes: this.card.notes,
      transcript: this.transcript,
      draft: this.draft,
      language: this.card.language || "en",
    };
    try {
      const resp = await fetch("/api/quotes", {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.error = errorText(body, "Couldn't create the quote. Try again.");
        return;
      }
      this.quote = body;
      this.screen = "quoteSent";
    } catch {
      this.error = "No connection. Tap again when you have signal; it won't make two quotes.";
    } finally {
      this.busy = false;
    }
  },
  liveRefresh() {
    // Whatever changed (paid, part-paid, accepted), the money view and an open job page follow.
    clearTimeout(this._liveTimer);
    this._liveTimer = setTimeout(() => {
      this.loadHome();
      if (this.screen === "detail") this.refreshDetail();
    }, 300);
  },
  onQuote(update) {
    this.loadJobs();
    this.liveRefresh();
    if (!this.quote || update.id !== this.quote.id) return;
    const wasBooked = this.quoteBooked();
    this.quote.status = update.status;
    this.quote.deposit = update.deposit_status ? { ...(this.quote.deposit || {}), status: update.deposit_status } : this.quote.deposit;
    if (this.quoteBooked() && !wasBooked) buzz([80, 60, 160]);
  },
  quoteBooked() {
    const q = this.quote;
    if (!q || q.status !== "accepted") return false;
    return this.toCents(q.deposit_amount) === 0 || q.deposit?.status === "paid";
  },
  quoteHeadline() {
    const q = this.quote;
    if (this.quoteBooked()) return "Booked!";
    if (q?.status === "accepted") return "Accepted, deposit due";
    if (q?.status === "awaiting_owner") return "Client is waiting for you";
    if (q?.status === "negotiating") return "Client is negotiating";
    return "Quote ready to send";
  },
  quoteMessage() {
    const q = this.quote;
    const who = q.client_name ? `Hi ${q.client_name.split(" ")[0]}, here` : "Here";
    return `${who}'s your quote for ${this.money(this.toCents(q.total), q.currency)}. You can accept it and book here: ${q.public_url}`;
  },
  smsLink() {
    return this.quote ? `sms:?&body=${encodeURIComponent(this.quoteMessage())}` : "#";
  },
  mailLink() {
    if (!this.quote) return "#";
    const q = this.quote;
    return `mailto:${encodeURIComponent(q.client_email || "")}?subject=${encodeURIComponent(`Your quote ${q.number}`)}&body=${encodeURIComponent(this.quoteMessage())}`;
  },
  async copyQuoteLink() {
    try {
      await navigator.clipboard.writeText(this.quote.public_url);
      this.copied = true;
      setTimeout(() => (this.copied = false), 2500);
    } catch {
      window.prompt("Copy the quote link:", this.quote.public_url);
    }
  },
  async shareQuote() {
    try {
      await navigator.share({ title: `Quote ${this.quote.number}`, text: this.quoteMessage(), url: this.quote.public_url });
    } catch {
      /* closed */
    }
  },

  // ── sent ──────────────────────────────────────────────────────────────
  async openQr() {
    this.qrError = false;
    this.qrOpen = true;
    try {
      // Keep the screen on while the client finds their camera.
      this.wakeLock = await navigator.wakeLock?.request("screen");
    } catch {
      this.wakeLock = null;
    }
  },
  closeQr() {
    this.qrOpen = false;
    this.wakeLock?.release?.();
    this.wakeLock = null;
  },
  async copyLink() {
    try {
      await navigator.clipboard.writeText(this.invoice.payer_view_url);
      this.copied = true;
      setTimeout(() => (this.copied = false), 2500);
    } catch {
      window.prompt("Copy the pay link:", this.invoice.payer_view_url);
    }
  },
  async shareLink() {
    try {
      await navigator.share({
        title: `Invoice ${this.invoice.number}`,
        text: `Here's your invoice for ${this.money(this.toCents(this.invoice.total), this.invoice.currency)}`,
        url: this.invoice.payer_view_url,
      });
    } catch {
      /* the user closed the share sheet */
    }
  },

  // ── deal rules ────────────────────────────────────────────────────────
  async openSettings() {
    this.error = "";
    this.saved = false;
    this.screen = "settings";
    try {
      const [resp, chaseResp, wsResp] = await Promise.all([fetch("/api/policy"), fetch("/api/chase/settings"), fetch("/api/workspace")]);
      if (!resp.ok) throw new Error();
      this.policy = await resp.json();
      if (wsResp.ok) this.biz = await wsResp.json();
      if (chaseResp.ok) this.autoChase = (await chaseResp.json()).auto_chase;
      for (const k of ["max_discount_pct", "floor_pct", "min_deposit_pct"]) this.policy[k] = Number(this.policy[k]);
    } catch {
      this.error = "Couldn't load your rules. Check your signal and try again.";
    }
  },
  async savePolicy() {
    this.busy = true;
    this.error = "";
    try {
      if (this.biz) {
        const b = this.biz;
        const wsResp = await fetch("/api/workspace", {
          method: "PUT", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ business_name: b.business_name, owner_name: b.owner_name, currency: b.currency,
                                 default_due_days: Number(b.default_due_days) || 0, trade: b.trade ?? "",
                                 invoice_note: b.invoice_note ?? "", onboarded: true }),
        });
        if (!wsResp.ok) {
          this.error = errorText(await wsResp.json().catch(() => ({})), "Check your business details.");
          return;
        }
        const w = await wsResp.json();
        this.businessName = w.business_name;
        this.invoiceNote = w.invoice_note || "";
        this.ownerName = w.owner_name || "";
        this.onb.trade = tradeKey(w.trade);
        this.onb.currency = w.currency;
        Object.assign(window.SAYINVOICE, { currency: w.currency, defaultDueDays: w.default_due_days, invoiceNote: this.invoiceNote });
      }
      const resp = await fetch("/api/policy", {
        method: "PUT",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(this.policy),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.error = errorText(body, "Couldn't save. Check the numbers.");
        return;
      }
      this.saved = true;
      setTimeout(() => (this.saved = false), 2500);
    } catch {
      this.error = "No connection. Try again.";
    } finally {
      this.busy = false;
    }
  },

  reset() {
    this.stopVoice();
    this.stopRecording();
    this.stopChecking();
    this.loadJobs();
    this.loadHome();
    Object.assign(this, {
      screen: "record", error: "", busy: false, typed: "", typing: false, answer: "",
      card: { client_name: "", client_email: "", currency: "", due_date: "", notes: "", items: [], deposit_pct: "", job_date: "", language: "en" },
      clientSource: "", suggestions: [], clientOptions: [],
      flags: [], missing: [], question: "", warnings: [], transcript: "", draft: null, key: "", noteOpen: false,
      mode: "invoice", quote: null, job: null, matchedJob: null,
      invoice: null, qrOpen: false, qrError: false, copied: false,
    });
  },
});
