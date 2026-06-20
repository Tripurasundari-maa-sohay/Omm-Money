// Service Worker — Portfolio Dashboard PWA (network-only, no shell cache)
// Why: OAuth-gated origin returns 302→Google when cookie missing. Caching shell
// responses leaks the redirect HTML, which then returns to subsequent requests
// causing ERR_FAILED + breaks. Network-only is safe + always fresh.
const CACHE = 'portfolio-v141';

self.addEventListener('install', e => { self.skipWaiting(); });

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

// Network-only for everything. No shell cache, no /data/ cache.
// Browser handles its own HTTP cache via response headers (Caddy sets them).
self.addEventListener('fetch', e => {
  e.respondWith(fetch(e.request).catch(err => {
    return new Response('Offline', { status: 503, headers: { 'Content-Type': 'text/plain' } });
  }));
});
