// SayInvoice tradesperson app: Record -> Preview -> Send -> Sent.
// Amounts here are for display only; the server recomputes every total.

const PAY_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_RECORD_SECONDS = 90;
const CHECK_EVERY_MS = 8000; // backup for slow webhooks while the Sent screen waits
const SETTLED = ["paid", "cancelled", "refunded"];
// Currencies PayPal Invoicing accepts (mirrors app/agents/parser_rules.py).
const PAYPAL_CURRENCIES = "AUD BRL CAD CHF CNY CZK DKK EUR GBP HKD HUF ILS JPY MXN MYR NOK NZD PHP PLN RUB SEK SGD THB TWD USD".split(" ");

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

function errorText(body, fallback) {
  const d = body?.detail;
  if (typeof d === "string") return d;
  if (Array.isArray(d) && d[0]?.msg) return d[0].msg.replace(/^Value error, /, "");
  return fallback;
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
  tab: "owed",
  detail: null,
  detailRef: null,
  acting: false,
  actionMsg: "",
  actionError: "",
  cancelArmed: false,
  payOpen: false,
  pay: { amount: "", method: "CASH" },

  // approvals (Deal Agent escalations)
  approvals: [],
  countering: false,
  replying: false,
  counter: { discount: "", deposit: "", note: "" },
  deciding: false,
  approvalError: "",
  get approval() {
    return this.approvals[0] || null;
  },

  init() {
    this.listen();
    this.loadApprovals();
    this.loadJobs();
    this.loadHome();
  },

  // ── who owes me ───────────────────────────────────────────────────────
  async loadHome() {
    try {
      const resp = await fetch("/api/home");
      if (resp.ok) this.home = await resp.json();
    } catch {
      /* keep the last view */
    }
  },
  async openMoney() {
    this.screen = "money";
    this.actionMsg = "";
    await this.loadHome();
    if (this.home && this.tab === "owed" && !this.home.tabs.owed.length && this.home.tabs.overdue.length) this.tab = "overdue";
  },
  async openDetail(kind, id) {
    this.detailRef = { kind, id };
    this.actionMsg = "";
    this.actionError = "";
    this.cancelArmed = false;
    await this.refreshDetail();
    this.screen = "detail";
    window.scrollTo(0, 0);
  },
  async refreshDetail() {
    if (!this.detailRef) return;
    try {
      const resp = await fetch(`/api/jobs/${this.detailRef.kind}/${this.detailRef.id}`);
      if (resp.ok) this.detail = await resp.json();
    } catch {
      /* keep what's on screen */
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
      const resp = await fetch(`/api/quotes/${a.quote_id}/messages/${a.message_id}/decision`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
      });
      const out = await resp.json().catch(() => ({}));
      if (!resp.ok && resp.status !== 409) {
        this.approvalError = errorText(out, "Couldn't send that. Try again.");
        return;
      }
      // Done (or already answered elsewhere): next card.
      this.approvals = this.approvals.slice(1);
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
    await this.readJob({ text: this.typed.trim() });
  },

  async readJob(input) {
    this.screen = "reading";
    this.error = "";
    try {
      this.loadCard(await this.callParse(input));
      this.screen = "preview";
    } catch (e) {
      this.screen = "record";
      this.error = e.message;
      if (input.text) this.typing = true;
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
    } catch (e) {
      this.error = e.message;
    } finally {
      this.busy = false;
    }
  },

  loadCard(result, { keepKey = false } = {}) {
    // A follow-up on a final invoice stays a final invoice.
    if (!(keepKey && this.mode === "final")) this.mode = result.mode;
    this.matchedJob = this.mode === "invoice" && !keepKey ? this.matchJob(result) : this.matchedJob;
    this.card = {
      deposit_pct: result.deposit_pct != null ? String(Number(result.deposit_pct)) : "",
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
      const resp = await fetch("/api/policy");
      if (!resp.ok) throw new Error();
      this.policy = await resp.json();
      for (const k of ["max_discount_pct", "floor_pct", "min_deposit_pct"]) this.policy[k] = Number(this.policy[k]);
    } catch {
      this.error = "Couldn't load your rules. Check your signal and try again.";
    }
  },
  async savePolicy() {
    this.busy = true;
    this.error = "";
    try {
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
    this.stopRecording();
    this.stopChecking();
    this.loadJobs();
    this.loadHome();
    Object.assign(this, {
      screen: "record", error: "", busy: false, typed: "", typing: false, answer: "",
      card: { client_name: "", client_email: "", currency: "", due_date: "", notes: "", items: [], deposit_pct: "", job_date: "" },
      flags: [], missing: [], question: "", warnings: [], transcript: "", draft: null, key: "", noteOpen: false,
      mode: "invoice", quote: null, job: null, matchedJob: null,
      invoice: null, qrOpen: false, qrError: false, copied: false,
    });
  },
});
