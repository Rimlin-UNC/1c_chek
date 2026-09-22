// ======================================================================
// Ямастер Чек — Service Worker (PWA: офлайн-режим веб-клиента)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
//
// Статика — cache-first; API-запросы — только сеть (данные всегда свежие).
// ======================================================================

const CACHE = 'ymaster-check-v2';
const ASSETS = [
  '/', '/index.html', '/css/app.css', '/js/app.js', '/js/api.js', '/js/ui.js',
  '/js/charts.js', '/js/icons.js', '/js/scanner.js', '/js/vendor/jsQR.js',
  '/img/logo.svg', '/manifest.webmanifest',
];

self.addEventListener('install', (e) => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(ASSETS)).then(() => self.skipWaiting()));
});

self.addEventListener('activate', (e) => {
  e.waitUntil(
    caches.keys()
      .then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener('fetch', (e) => {
  const url = new URL(e.request.url);
  if (e.request.method !== 'GET' || url.pathname.startsWith('/api/') || url.pathname.startsWith('/onec/') || url.pathname.startsWith('/ws/')) {
    return; // сеть
  }
  e.respondWith(
    caches.match(e.request).then(hit => hit || fetch(e.request).then(resp => {
      if (resp.ok && url.origin === location.origin) {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return resp;
    }).catch(() => caches.match('/index.html')))
  );
});
