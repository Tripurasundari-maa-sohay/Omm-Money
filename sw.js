// Service Worker — Portfolio Dashboard PWA
// Uses relative paths — works on GitHub Pages subpath (/portfolio-dashboard/)

const CACHE = 'portfolio-v39';
const BASE = self.location.pathname.substring(0, self.location.pathname.lastIndexOf('/'));

const SHELL = [
  BASE + '/index.html',          // explicit file — avoids CDN directory-index caching
  BASE + '/icons/icon-192.png',
  BASE + '/icons/icon-512.png',
  BASE + '/icons/apple-touch-icon.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c =>
      // cache:'no-store' bypasses GitHub Pages CDN stale cache on SW install
      Promise.all(SHELL.map(url =>
        fetch(new Request(url, { cache: 'no-store' })).then(r => c.put(url, r))
      ))
    ).then(() => self.skipWaiting())
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

  // Network-first for all data files — never serve stale prices or cost basis
  if (path.includes('/data/')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }

  // Base URL redirect → serve cached index.html directly (bypass CDN directory cache)
  if (path === BASE + '/' || path === BASE) {
    e.respondWith(
      caches.match(BASE + '/index.html').then(cached => cached || fetch(e.request))
    );
    return;
  }

  // Cache-first for other shell assets (icons etc)
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
