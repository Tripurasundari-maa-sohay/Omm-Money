// Service Worker — Portfolio Dashboard PWA
// Uses relative paths — works on GitHub Pages subpath (/portfolio-dashboard/)

const CACHE = 'portfolio-v21';
const BASE = self.location.pathname.substring(0, self.location.pathname.lastIndexOf('/'));

const SHELL = [
  BASE + '/',
  BASE + '/index.html',
  BASE + '/icons/icon-192.png',
  BASE + '/icons/icon-512.png',
  BASE + '/icons/apple-touch-icon.png',
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
  const path = url.pathname;

  // Always network-first for data files (never serve stale prices)
  if (path.includes('/data/processed/') || path.includes('/data/history/')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }

  // Cache-first for shell assets
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
