// The client's quote page: accept, pay the deposit, see "Booked" without reloading.
const POLL_MS = 6000;

window.quotePage = () => ({
  token: window.QUOTE.token,
  state: window.QUOTE.initial,
  email: window.QUOTE.initial.email || "",
  busy: false,
  error: "",
  timer: null,
  messages: [],
  draft: "",
  sending: false,
  chatError: "",
  chatTimer: null,
  owner: window.QUOTE.owner,

  init() {
    if (this.depositDue()) this.poll();
    if (["sent", "negotiating", "awaiting_owner"].includes(this.state.status)) this.loadMessages();
  },

  // ── ask for a change ──────────────────────────────────────────────────
  speaker(m) {
    if (m.role === "client") return "You";
    if (m.role === "owner") return this.owner;
    return `${window.QUOTE.business} assistant`;
  },
  openOffer() {
    return this.messages.some((m) => m.offer_status === "open");
  },
  async loadMessages() {
    try {
      const resp = await fetch(`/q/${this.token}/messages`);
      if (resp.ok) this.applyConversation(await resp.json());
    } catch {
      /* the next poll will catch up */
    }
  },
  applyConversation(body) {
    this.messages = body.messages;
    if (body.status && this.state.status !== body.status && body.status !== "accepted") this.state.status = body.status;
    clearInterval(this.chatTimer);
    if (this.state.status === "awaiting_owner") this.chatTimer = setInterval(() => this.loadMessages(), 5000);
  },
  async send() {
    const text = this.draft.trim();
    if (!text || this.sending) return;
    this.sending = true;
    this.chatError = "";
    // Show it straight away; the server's list replaces it.
    this.messages = [...this.messages, { id: `tmp-${Date.now()}`, role: "client", text, offer: null, offer_status: "none" }];
    this.draft = "";
    try {
      const resp = await fetch(`/q/${this.token}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.messages = this.messages.filter((m) => !String(m.id).startsWith("tmp-"));
        this.draft = text;
        this.chatError = typeof body.detail === "string" ? body.detail : "Couldn't send that. Try again.";
        return;
      }
      this.applyConversation(body);
    } catch {
      this.messages = this.messages.filter((m) => !String(m.id).startsWith("tmp-"));
      this.draft = text;
      this.chatError = "No connection. Try again.";
    } finally {
      this.sending = false;
    }
  },
  async acceptOffer(m) {
    if (!this.email && !window.QUOTE.initial.email) {
      this.chatError = "Add your email in the box below first, so PayPal can send the deposit request.";
      document.getElementById("email")?.focus();
      return;
    }
    this.busy = true;
    this.chatError = "";
    try {
      const resp = await fetch(`/q/${this.token}/offers/${m.id}/accept`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ email: this.email || "" }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.chatError = typeof body.detail === "string" ? body.detail : "Couldn't accept it just now.";
        await this.loadMessages();
        return;
      }
      // The quote itself changed (new version): reload so every figure on the page matches.
      window.location.reload();
    } catch {
      this.chatError = "No connection. Try again; you won't be charged twice.";
    } finally {
      this.busy = false;
    }
  },

  depositDue() {
    if (this.state.status !== "accepted" || !this.state.deposit_required) return false;
    const d = this.state.deposit;
    // Accepted but the deposit invoice didn't go out yet: still due (the page offers a retry).
    return !d || !["paid", "cancelled", "refunded"].includes(d.status);
  },

  pillText() {
    const s = this.state.status;
    if (s === "accepted") return this.depositDue() ? "Accepted" : "Booked";
    if (s === "expired") return "Expired";
    if (s === "declined") return "Closed";
    if (s === "awaiting_owner") return `With ${this.owner}`;
    return "Awaiting your reply";
  },
  pillClass() {
    const s = this.state.status;
    return { ok: s === "accepted" && !this.depositDue(), warn: s === "accepted" && this.depositDue(), off: s === "expired" || s === "declined" };
  },

  async accept() {
    this.busy = true;
    this.error = "";
    try {
      const resp = await fetch(`/q/${this.token}/accept`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ email: this.email || "" }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        const d = body.detail;
        this.error = typeof d === "string" ? d : Array.isArray(d) ? "Check your email address." : "Couldn't book it just now. Try again.";
        if (resp.status === 502) this.state.status = "accepted";
        return;
      }
      this.state = body;
      if (this.depositDue()) this.poll();
    } catch {
      this.error = "No connection. Try again; you won't be charged twice.";
    } finally {
      this.busy = false;
    }
  },

  poll() {
    clearInterval(this.timer);
    this.timer = setInterval(async () => {
      try {
        const resp = await fetch(`/q/${this.token}/status`);
        if (resp.ok) this.state = await resp.json();
        if (!this.depositDue()) clearInterval(this.timer);
      } catch {
        /* next tick */
      }
    }, POLL_MS);
  },
});
