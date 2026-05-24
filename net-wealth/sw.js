// Service Worker — ODIN Net Worth PWA
const CACHE = 'odin-v13';
const BASE = self.location.pathname.substring(0, self.location.pathname.lastIndexOf('/'));

const SHELL = [
  BASE + '/index.html',
  BASE + '/icons/icon-192.png',
  BASE + '/icons/icon-512.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c =>
      Promise.all(SHELL.map(url =>
        fetch(new Request(url, { cache: 'no-store' })).then(r => c.put(url, r)).catch(() => {})
      ))
    ).then(() => self.skipWaiting())
  );
});

self.addEventListener('activate', e => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
      .then(() =>
        self.clients.matchAll({ type: 'window', includeUncontrolled: true })
          .then(clients => clients.forEach(c => c.navigate(c.url)))
      )
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const path = url.pathname;
  // Network-first for all data files (portfolio JSON, seed JSON, FX APIs)
  if (path.includes('/data/') || url.host.includes('exchangerate') || url.host.includes('frankfurter')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }
  if (path === BASE + '/' || path === BASE) {
    e.respondWith(
      caches.match(BASE + '/index.html').then(cached => cached || fetch(e.request))
    );
    return;
  }
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
