// The client's payment-plan page: propose a plan, agree it, watch instalments get paid.
function tr(key, values = {}) {
  const text = (window.PLAN.T || {})[key] || key;
  return text.replace(/\{(\w+)\}/g, (_, k) => (values[k] ?? `{${k}}`));
}

window.planPage = () => ({
  tr,
  token: window.PLAN.token,
  state: window.PLAN.initial,
  messages: window.PLAN.initial.messages,
  draft: "",
  sending: false,
  busy: false,
  error: "",
  timer: null,

  init() {
    this.schedulePoll();
  },
  speaker(m) {
    if (m.role === "client") return tr("you");
    if (m.role === "owner") return window.PLAN.owner;
    return tr("assistant", { business: window.PLAN.business });
  },
  pillText() {
    const s = this.state.status;
    if (s === "paid") return tr("pill_paid");
    if (this.state.agreed.length) return tr("pill_on_plan");
    if (s === "awaiting_owner") return tr("pill_with", { owner: window.PLAN.owner });
    if (s === "overdue") return tr("pill_overdue");
    return tr("pill_unpaid");
  },
  pillClass() {
    const s = this.state.status;
    return { ok: s === "paid" || this.state.agreed.length > 0, warn: s === "overdue" || s === "awaiting_owner" };
  },
  nextDue() {
    const next = this.state.agreed.find((r) => r.status !== "paid");
    return next ? next.due : "";
  },
  apply(body) {
    this.state = body;
    this.messages = body.messages;
    this.schedulePoll();
  },
  schedulePoll() {
    clearInterval(this.timer);
    // Waiting on the owner, or on instalments: keep the page current by itself.
    if (this.state.status === "awaiting_owner" || (this.state.agreed.length && this.state.status !== "paid")) {
      this.timer = setInterval(async () => {
        try {
          const resp = await fetch(`/p/${this.token}/status`);
          if (resp.ok) this.apply(await resp.json());
        } catch {
          /* next tick */
        }
      }, 6000);
    }
  },
  async send() {
    const text = this.draft.trim();
    if (!text || this.sending) return;
    this.sending = true;
    this.error = "";
    this.messages = [...this.messages, { id: `tmp-${Date.now()}`, role: "client", text, plan: [], offer_status: "none" }];
    this.draft = "";
    try {
      const resp = await fetch(`/p/${this.token}/messages`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ text }),
      });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.messages = this.messages.filter((m) => !String(m.id).startsWith("tmp-"));
        this.draft = text;
        this.error = typeof body.detail === "string" ? body.detail : tr("err_send");
        return;
      }
      this.apply(body);
    } catch {
      this.messages = this.messages.filter((m) => !String(m.id).startsWith("tmp-"));
      this.draft = text;
      this.error = tr("err_no_connection");
    } finally {
      this.sending = false;
    }
  },
  async agree(m) {
    this.busy = true;
    this.error = "";
    try {
      const resp = await fetch(`/p/${this.token}/plans/${m.id}/accept`, { method: "POST" });
      const body = await resp.json().catch(() => ({}));
      if (!resp.ok) {
        this.error = typeof body.detail === "string" ? body.detail : tr("err_plan");
        return;
      }
      this.apply(body);
      window.scrollTo({ top: 0, behavior: "smooth" });
    } catch {
      this.error = tr("err_no_connection");
    } finally {
      this.busy = false;
    }
  },
});
