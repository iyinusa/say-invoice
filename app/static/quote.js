// The client's quote page: accept, pay the deposit, see "Booked" without reloading.
const POLL_MS = 6000;

window.quotePage = () => ({
  token: window.QUOTE.token,
  state: window.QUOTE.initial,
  email: window.QUOTE.initial.email || "",
  busy: false,
  error: "",
  timer: null,

  init() {
    if (this.depositDue()) this.poll();
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
