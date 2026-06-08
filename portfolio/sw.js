// Service Worker — Portfolio Dashboard PWA
const CACHE = 'portfolio-v94';
const BASE = self.location.pathname.substring(0, self.location.pathname.lastIndexOf('/'));

const SHELL = [
  BASE + '/index.html',
  BASE + '/icons/icon-192.png',
  BASE + '/icons/icon-512.png',
  BASE + '/icons/apple-touch-icon.png',
];

self.addEventListener('install', e => {
  e.waitUntil(
    caches.open(CACHE).then(c =>
      Promise.all(SHELL.map(url =>
        fetch(new Request(url, { cache: 'no-store' })).then(r => c.put(url, r))
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
        // Force all open tabs to reload with fresh HTML after SW activates
        self.clients.matchAll({ type: 'window', includeUncontrolled: true })
          .then(clients => clients.forEach(c => c.navigate(c.url)))
      )
  );
});

self.addEventListener('fetch', e => {
  const url = new URL(e.request.url);
  const path = url.pathname;

  // Network-first for all data files
  if (path.includes('/data/')) {
    e.respondWith(fetch(e.request).catch(() => caches.match(e.request)));
    return;
  }

  // Base URL → serve cached index.html (bypasses CDN directory cache)
  if (path === BASE + '/' || path === BASE) {
    e.respondWith(
      caches.match(BASE + '/index.html').then(cached => cached || fetch(e.request))
    );
    return;
  }

  // Cache-first for icons and other shell assets
  e.respondWith(
    caches.match(e.request).then(cached => cached || fetch(e.request))
  );
});
