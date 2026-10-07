// ======================================================================
// Ямастер Чек — Service Worker (PWA: офлайн-режим веб-клиента)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
//
// Статика — cache-first; API-запросы — только сеть (данные всегда свежие).
// ======================================================================

const CACHE = 'ymaster-check-v1.45.2';
// v1.8.0: полный и корректный список предзагрузки (URL /js/…, /css/… отдаются
// SPA из app/static; ранее в списке был /js/printpack.js и битые пути —
// addAll падал с 404 и Service Worker вовсе не устанавливался)
const ASSETS = [
  '/', '/index.html',
  '/css/app.css?v=1.45.2', '/js/app.js?v=1.45.2',   // v1.12.2: как в index.html
  '/js/api.js', '/js/ui.js',
  '/js/charts.js', '/js/icons.js', '/js/scanner.js', '/js/printpack.js',
  '/js/vendor/jsQR.js', '/img/logo.svg', '/manifest.webmanifest',
  '/img/manual/hero.jpg', '/img/manual/scan.jpg',
  '/img/manual/report.jpg', '/img/manual/update.jpg',   // v1.21.0: инструкция
  '/img/manual/landing-hero.jpg', '/img/manual/landing-bonus.jpg',   // v1.45.1: главная
];

self.addEventListener('install', (e) => {
  // v1.45.1: установка поштучно и устойчиво — один битый/недоступный ассет
  // больше не валил весь install (addAll падал целиком, SW не обновлялся)
  e.waitUntil(caches.open(CACHE).then(c =>
    Promise.all(ASSETS.map(u => c.add(u).catch(() => {})))
  ).then(() => self.skipWaiting()));
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
    return; // API — только сеть
  }

  // Код приложения (страницы, JS, CSS, манифест) — СНАЧАЛА сеть, чтобы
  // клиент всегда соответствовал серверу; кэш — только если сеть недоступна.
  const isAppCode =
    e.request.mode === 'navigate' ||
    url.pathname === '/' ||
    url.pathname.endsWith('.html') ||
    url.pathname.endsWith('.js') ||
    url.pathname.endsWith('.css') ||
    url.pathname.endsWith('.webmanifest');

  if (isAppCode) {
    e.respondWith(
      fetch(e.request).then(resp => {
        if (resp.ok && url.origin === location.origin) {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return resp;
      }).catch(() =>
        caches.match(e.request).then(hit => hit || caches.match('/index.html'))
      )
    );
    return;
  }

  // Картинки и прочая статика — cache-first
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
