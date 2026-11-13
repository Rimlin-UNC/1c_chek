// ======================================================================
// Ямастер Чек — Service Worker (PWA: офлайн-режим веб-клиента)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
//
// Статика — cache-first; API-запросы — только сеть (данные всегда свежие).
// ======================================================================

const CACHE = 'ymaster-check-v1.72.14';
// v1.8.0: полный и корректный список предзагрузки (URL /js/…, /css/… отдаются
// SPA из app/static; ранее в списке был /js/printpack.js и битые пути —
// addAll падал с 404 и Service Worker вовсе не устанавливался)
const ASSETS = [
  '/', '/index.html',
  '/css/app.css?v=1.72.14', '/css/home.css?v=1.72.14', '/js/app.js?v=1.72.14', '/js/counterparties.js?v=1.72.14',   // T-83: главная и приложение
  '/js/api.js', '/js/ui.js', '/js/offline_outbox.js', '/js/navigation_counts.js', '/js/connection_status.js',
  '/js/approvals.js', '/js/bank.js', '/js/card.js', '/js/clients.js',
  '/js/counterparties.js', '/js/docreq.js', '/js/goods.js', '/js/osm.js',
  '/js/primary.js', '/js/primary_folder.js', '/js/primary_mail.js',
  '/js/supabase_demo.js', '/js/unified_fill.js', '/js/unified_review.js',
  '/js/vendor/chart.umd.js',
  '/js/charts.js', '/js/icons.js', '/js/scanner.js', '/js/printpack.js',
  '/js/vendor/jsQR.js', '/img/logo.svg', '/manifest.webmanifest',
  '/css/splash.css', '/js/splash.js',                 // v1.70.98: заставка загрузки
  '/js/boot.js',                                      // v1.70.103: решения до первой отрисовки
  '/fonts/inter-cyrillic-wght-normal.woff2', '/fonts/inter-latin-wght-normal.woff2',   // v1.70.103: свои шрифты
  '/img/manual/hero.jpg', '/img/manual/scan.jpg',
  '/img/manual/report.jpg', '/img/manual/update.jpg',   // v1.21.0: инструкция
  '/img/manual/settings.jpg',   // v1.70.9: инструкция «Настройки»
  '/img/manual/tggateway.jpg',  // v1.70.12: инструкция «Telegram сквозь блокировки»
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

// Только заранее известная публичная статика не зависит от Origin: заголовок
// Vary: Origin у серверного ответа иначе не даёт офлайн-загрузку модулей.
function cachedResponse(request) {
  const url = new URL(request.url);
  const pinned = url.origin === self.location.origin
    && /^\/(js|css|fonts|img)\//.test(url.pathname)
    && ASSETS.includes(url.pathname + url.search);
  return pinned ? caches.open(CACHE).then(c => c.match(request, { ignoreVary: true })) : caches.match(request);
}

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
    e.respondWith((async () => {
      const fallback = async () => {
        const hit = await cachedResponse(e.request);
        if (hit) return hit;
        // HTML-оболочка пригодна только для навигации, не для JS/CSS.
        if (e.request.mode === 'navigate') return caches.match('/index.html');
        return null;
      };
      const controller = new AbortController();
      const timer = setTimeout(() => controller.abort(), 6000);
      try {
        const resp = await fetch(e.request, { signal: controller.signal });
        if ([502, 503, 504].includes(resp.status)) return (await fallback()) || resp;
        if (resp.ok && url.origin === location.origin) {
          const copy = resp.clone();
          caches.open(CACHE).then(c => c.put(e.request, copy));
        }
        return resp;
      } catch {
        return (await fallback()) || Response.error();
      } finally { clearTimeout(timer); }
    })());
    return;
  }

  // Картинки и прочая статика — cache-first
  e.respondWith(
    cachedResponse(e.request).then(hit => hit || fetch(e.request).then(resp => {
      if (resp.ok && url.origin === location.origin) {
        const copy = resp.clone();
        caches.open(CACHE).then(c => c.put(e.request, copy));
      }
      return resp;
    }).catch(() => Response.error()))
  );
});
