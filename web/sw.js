/* Service worker for the installable app shell only.
 *
 * Deliberately does not cache anything under /api/ -- this dashboard shows
 * live trading analysis, and a cached price or confidence score is worse
 * than no page at all. Only the static shell (HTML/CSS/JS/icons) is cached,
 * network-first, so a flaky connection degrades to "slightly stale UI" and
 * a lost connection degrades to an explicit offline state, never to silently
 * stale numbers.
 */
const CACHE = "market-analyst-shell-v1";
const SHELL = ["/", "/assets/app.css", "/assets/app.js", "/manifest.json"];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys().then((keys) =>
      Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k)))
    )
  );
  self.clients.claim();
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (url.pathname.startsWith("/api/")) return;
  if (event.request.method !== "GET") return;

  event.respondWith(
    fetch(event.request)
      .then((response) => {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
        return response;
      })
      .catch(() => caches.match(event.request))
  );
});
