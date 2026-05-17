// Service Worker — Portfolio Dashboard PWA
// Caches app shell for offline; always fetches data files fresh from network.

const CACHE = 'portfolio-v1';
const SHELL = [
  '/',
  '/index.html',
  '/icons/icon-192.png',
  '/icons/icon-512.png',
  '/icons/apple-touch-icon.png',
];

// Data files — always network-first (never serve stale prices/signals)
const DATA_PATHS = [
  '/data/processed/',
  '/data/history/',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys().then(keys =>
      Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k)))
    ).then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);

  // Always network for data files
  if (DATA_PATHS.some(p => url.pathname.startsWith(p))) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }

  // Cache-first for shell
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
