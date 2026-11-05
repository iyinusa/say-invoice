// SayInvoice service worker: makes the app installable and lets it open with no signal.
// Money is never cached: every /api, /q, /p and /webhooks request goes to the network.
const VERSION = "__VERSION__";
const SHELL = `shell-${VERSION}`;
const ASSETS = [
  "/",
  `/static/app.css?v=${VERSION}`,
  `/static/app.js?v=${VERSION}`,
  "/static/icons/icon.svg",
  "/static/icons/icon-192.png",
  "https://cdn.jsdelivr.net/npm/alpinejs@3.14.9/dist/cdn.min.js",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(SHELL)
      .then((cache) => Promise.all(ASSETS.map((url) => cache.add(url).catch(() => {}))))
      .then(() => self.skipWaiting()),
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== SHELL).map((k) => caches.delete(k))))
      .then(() => self.clients.claim()),
  );
});

self.addEventListener("fetch", (event) => {
  const req = event.request;
  if (req.method !== "GET") return;
  const url = new URL(req.url);
  const sameOrigin = url.origin === self.location.origin;
  if (sameOrigin && !(url.pathname === "/" || url.pathname.startsWith("/static/"))) return;

  if (req.mode === "navigate") {
    // Network first, so the page is always today's; the cached shell only when offline.
    event.respondWith(
      fetch(req)
        .then((resp) => {
          const copy = resp.clone();
          if (resp.ok) caches.open(SHELL).then((c) => c.put("/", copy));
          return resp;
        })
        .catch(() => caches.match("/")),
    );
    return;
  }
  // Static files are versioned by URL, so the cache can answer first.
  event.respondWith(
    caches.match(req).then((hit) => hit || fetch(req).then((resp) => {
      if (resp.ok && (sameOrigin || url.hostname === "cdn.jsdelivr.net")) {
        const copy = resp.clone();
        caches.open(SHELL).then((c) => c.put(req, copy));
      }
      return resp;
    })),
  );
});
