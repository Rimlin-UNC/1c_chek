/* v1.70.98: заставка загрузки «chek.ymaster.ru» — таймлайн около 3 с (100 % — к 2,5 с, уход — к 3,3 с),
   синхронизированный с настоящей загрузкой: «Инициализация → Подключение модулей → Финальная настройка →
   Готово к запуску». Пока приложение не готово, прогресс ждёт на 90 %; когда app.js вызовет ymSplash.ready(),
   он доходит до 100 %, карточка становится мятной и уходит. Повторная загрузка в той же вкладке и автотесты —
   быстрее (≈1,3 с). Версия — из адреса стилей (?v=), затем с сервера (ymSplash.setVersion из app.js).
   Обычный скрипт с defer в <head>: выполняется после разбора страницы и до app.js; встроенные скрипты
   запрещены политикой CSP. Разметка — в index.html, стили — css/splash.css. */
(function () {
  'use strict';
  var root = document.getElementById('ym-splash');
  if (!root || window.ymSplash) return;
  // скрипт пришёл позже страховки CSS (15 с): заставка уже скрыта — не показываем её заново
  if (performance.now() > 14000) { root.classList.add('is-gone'); return; }
  root.classList.add('is-js');
  var byId = function (id) { return document.getElementById(id); };
  var status = byId('ym-splash-status'), pct = byId('ym-splash-pct'), bar = byId('ym-splash-progress');
  var card = root.querySelector('.ym-splash__card');
  var steps = root.querySelectorAll('.ym-splash__steps li');
  var reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var quick = false;
  try {
    quick = window.sessionStorage.getItem('ym-splash-seen') === '1';
    window.sessionStorage.setItem('ym-splash-seen', '1');
  } catch (e) { /* без хранилища — полная заставка */ }
  var K = quick || navigator.webdriver ? 0.4 : 1;
  // [момент, процент, подпись, этап]: этап 1 — идёт инициализация, 2 — модули, 3 — финал, 4 — готово
  var STAGES = [[0, 0, 'Подготовка…', 0], [500, 25, 'Инициализация…', 1], [1300, 68, 'Подключение модулей…', 2],
    [2000, 90, 'Финальная настройка…', 3], [2600, 100, 'Готово к запуску', 4]];
  var HOLD = 2000, FINISH = 450, LEAVE_AFTER = 320, SLOW_AFTER = 10000;
  var t0 = performance.now(), isReady = false, finalFrom = -1, finalAt = 0, shown = 0, stage = -1, done = false;

  var linkV = document.querySelector('link[href*="app.css?v="]');
  var versionEl = byId('ym-splash-version');
  if (linkV && versionEl) {
    var m = /[?&]v=([\d.]+)/.exec(linkV.getAttribute('href') || '');
    if (m) versionEl.textContent = m[1];
  }

  function ease(x) { return x < 0.5 ? 4 * x * x * x : 1 - Math.pow(-2 * x + 2, 3) / 2; }

  function natural(t) {            // процент по естественному таймлайну
    for (var i = 1; i < STAGES.length; i++) {
      if (t <= STAGES[i][0]) {
        var a = STAGES[i - 1], b = STAGES[i];
        return a[1] + (b[1] - a[1]) * ease((t - a[0]) / (b[0] - a[0]));
      }
    }
    return 100;
  }

  function setStage(next) {
    if (next === stage) return;
    stage = next;
    root.setAttribute('data-stage', String(next));
    var label = STAGES[Math.min(next, 4)][2];
    if (status) {
      status.textContent = label;
      status.classList.remove('is-swap');
      void status.offsetWidth;         // перезапуск анимации смены подписи
      status.classList.add('is-swap');
    }
    for (var i = 0; i < steps.length; i++) {
      var n = i + 1;
      steps[i].setAttribute('data-state', next > n ? 'done' : next === n ? 'active' : 'pending');
    }
    if (next >= 4) {
      for (var j = 0; j < steps.length; j++) steps[j].setAttribute('data-state', 'done');
      root.classList.add('is-ready');
    }
  }

  function paint(p) {
    shown = p;
    var v = Math.round(p);
    root.style.setProperty('--p', p.toFixed(2));
    if (pct) pct.textContent = v + ' %';
    if (bar) bar.value = v;
    var s = 0;
    for (var i = STAGES.length - 1; i >= 0; i--) { if (p >= STAGES[i][1] - 0.01) { s = STAGES[i][3]; break; } }
    setStage(p >= 99.99 ? 4 : Math.min(s, 3));
  }

  function leave() {
    if (done) return;
    done = true;
    root.classList.add('is-leaving');
    window.setTimeout(function () {
      root.classList.add('is-gone');
      try { document.dispatchEvent(new CustomEvent('ym-splash-done')); } catch (e) { /* старый браузер */ }
    }, reduce ? 50 : 540);
  }

  function frame(now) {
    if (done) return;
    var t = (now - t0) / K;
    var p;
    if (finalFrom >= 0) {              // финиш: от текущего значения до 100 %
      var k = Math.min(1, (now - finalAt) / (FINISH * K));
      p = finalFrom + (100 - finalFrom) * ease(k);
      if (k >= 1) { paint(100); window.setTimeout(leave, LEAVE_AFTER * K); return; }
    } else if (t < HOLD || !isReady) {
      p = natural(Math.min(t, HOLD));
      if (!isReady && t > HOLD) {      // ждём приложение: медленно подбираемся к 94 %
        p = 90 + 4 * (1 - Math.exp(-(t - HOLD) / 4000));
        if (t > SLOW_AFTER) { var slow = byId('ym-splash-slow'); if (slow) slow.hidden = false; }
      }
    } else {
      finalFrom = shown; finalAt = now;
      p = shown;
    }
    paint(p);
    window.requestAnimationFrame(frame);
  }

  // объём: карточка слегка наклоняется вслед за указателем
  if (!reduce && card && window.matchMedia && window.matchMedia('(pointer: fine)').matches) {
    var pending = false, px = 0.5, py = 0.5;
    root.addEventListener('pointermove', function (e) {
      px = e.clientX / window.innerWidth; py = e.clientY / window.innerHeight;
      if (pending) return;
      pending = true;
      window.requestAnimationFrame(function () {
        pending = false;
        card.style.setProperty('--ry', ((px - 0.5) * 7).toFixed(2) + 'deg');
        card.style.setProperty('--rx', ((0.5 - py) * 5).toFixed(2) + 'deg');
      });
    });
    root.addEventListener('pointerleave', function () { card.style.setProperty('--rx', '0deg'); card.style.setProperty('--ry', '0deg'); });
  }
  var reload = byId('ym-splash-reload');
  if (reload) reload.addEventListener('click', function () { window.location.reload(); });

  window.ymSplash = {
    ready: function () { isReady = true; },
    setVersion: function (v) { if (versionEl && v) versionEl.textContent = String(v).replace(/^v/, ''); },
    show: function () { /* заставка показывается только при загрузке */ },
    isDone: function () { return done; },
  };
  paint(0);
  window.requestAnimationFrame(frame);
})();
