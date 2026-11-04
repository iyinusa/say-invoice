// SayInvoice tradesperson app: Record -> Preview -> Send -> Sent.
// Amounts here are for display only; the server recomputes every total.

const PAY_EMAIL = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;
const MAX_RECORD_SECONDS = 90;
const CHECK_EVERY_MS = 8000; // backup for slow webhooks while the Sent screen waits
const SETTLED = ["paid", "cancelled", "refunded"];

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
  card: { client_name: "", client_email: "", currency: "", due_date: "", notes: "", items: [] },
  flags: [],
  missing: [],
  question: "",
  answer: "",
  warnings: [],
  transcript: "",
  draft: null,
  key: "",

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

  init() {
    this.listen();
  },

  // ── live updates ──────────────────────────────────────────────────────
  listen() {
    if (!window.EventSource) return;
    // EventSource reconnects on its own after a dropped signal.
    const source = new EventSource("/api/events");
    source.addEventListener("invoice", (e) => this.onInvoice(JSON.parse(e.data)));
  },

  onInvoice(update) {
    if (!this.invoice || update.id !== this.invoice.id) return;
    const wasPaid = this.invoice.status === "paid";
    Object.assign(this.invoice, {
      status: update.status,
      amount_paid: update.amount_paid,
      paid_at: update.paid_at,
    });
    if (SETTLED.includes(update.status)) this.stopChecking();
    if (update.status === "paid" && !wasPaid) navigator.vibrate?.([80, 60, 160]);
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
    form.append("mode", "invoice");
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
    this.card = {
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
      mode: "invoice",
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

  blocker() {
    if (!this.card.items.length) return "Add at least one line.";
    const bad = this.card.items.find((i) => !i.name.trim() || !(num(i.quantity) > 0) || !(num(i.unit_price) > 0));
    if (bad) return bad.name.trim() ? `Add a price for “${bad.name.trim()}”.` : "Every line needs a name.";
    if (!PAY_EMAIL.test(this.card.client_email)) return "Add the client's email so PayPal can send it.";
    if (!/^[A-Za-z]{3}$/.test(this.card.currency)) return "Currency should be a 3-letter code, like GBP.";
    return "";
  },
  canSend() {
    return !this.blocker();
  },

  // ── send ──────────────────────────────────────────────────────────────
  async send() {
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
    Object.assign(this, {
      screen: "record", error: "", busy: false, typed: "", typing: false, answer: "",
      card: { client_name: "", client_email: "", currency: "", due_date: "", notes: "", items: [] },
      flags: [], missing: [], question: "", warnings: [], transcript: "", draft: null, key: "",
      invoice: null, qrOpen: false, qrError: false, copied: false,
    });
  },
});
