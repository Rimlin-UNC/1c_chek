/* v1.70.98: заставка загрузки «chek.ymaster.ru» — этапы «Инициализация → Подключение модулей → Финальная
   настройка → Готово к запуску» синхронно с настоящей загрузкой: пока приложение не готово, прогресс ждёт на
   90 %; когда app.js вызовет ymSplash.ready(), он доходит до 100 %, карточка становится мятной и уходит.
   v1.70.100: длительность задаёт администратор («Настройки» → «Устройство и служебное» → «Заставка загрузки»):
   0–60 с, по умолчанию 5 с. Таймлайн и анимации растягиваются под заданное время: короткая заставка (до 2 с) —
   без списка этапов, длинная (от 8 с) — с подсказками о возможностях программы, 0 — без заставки. Последнее
   значение с сервера хранится в localStorage (ym-splash-sec): заставка с первого кадра идёт по нужному времени,
   свежее значение приходит из /api/v1/about (ymSplash.setDuration). Автотесты (webdriver) — не дольше 1,3 с.
   Версия — из адреса стилей (?v=), затем с сервера (ymSplash.setVersion). Обычный скрипт с defer в <head>:
   выполняется после разбора страницы и до app.js; встроенные скрипты запрещены политикой CSP. Разметка — в
   index.html, стили — css/splash.css.
   v1.70.103 (T-74): гостю (нет токена входа) заставка не показывается, пока администратор не включит её для
   гостей: класс ym-guest ставит js/boot.js до первой отрисовки, здесь таймлайн не запускается. Флаг с сервера
   (about.splash_guests) запоминается в localStorage (ym-splash-guests) — ymSplash.setGuests. */
(function () {
  'use strict';
  var root = document.getElementById('ym-splash');
  if (!root || window.ymSplash) return;
  // скрипт пришёл позже страховки CSS (15 с): заставка уже скрыта — не показываем её заново
  if (performance.now() > 14000) { root.classList.add('is-gone'); return; }
  root.classList.add('is-js');
  var KEY = 'ym-splash-sec', DEFAULT_SEC = 5, MAX_SEC = 60, QUICK_MS = 1300;
  var GUESTS_KEY = 'ym-splash-guests';               // v1.70.103: заставка для гостей (флаг администратора)
  var byId = function (id) { return document.getElementById(id); };
  var status = byId('ym-splash-status'), pct = byId('ym-splash-pct'), bar = byId('ym-splash-progress');
  var tip = byId('ym-splash-tip'), slow = byId('ym-splash-slow');
  var card = root.querySelector('.ym-splash__card');
  var steps = root.querySelectorAll('.ym-splash__steps li');
  var reduce = !!(window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches);
  var quick = !!navigator.webdriver;
  // подсказки длинной заставки — возможности программы
  var TIPS = [
    'Сканируйте QR-код — чек сам проверится в ФНС',
    'Выписка из банка приходит сама: платежи без документов попадают в «Что нужно от клиента»',
    'УПД из ЭДО, PDF и сканы — в одном реестре и одной кнопкой в 1С',
    'Все клиенты бухгалтера — в одном окне: экран «Клиенты»',
    'Срок сдачи и «час икс» — напоминания клиентам уходят сами',
    'Пакет JSON, CSV и УПД за месяц — для других учётных систем',
    'Перед каждым обновлением программа сама делает резервную копию базы',
  ];

  function clampSec(v) {
    var n = Math.round(Number(v));
    return isFinite(n) ? Math.max(0, Math.min(MAX_SEC, n)) : DEFAULT_SEC;
  }
  function storedSec() {
    try {
      var raw = window.localStorage.getItem(KEY);
      return raw === null || raw === '' ? DEFAULT_SEC : clampSec(raw);
    } catch (e) { void e; return DEFAULT_SEC; }           // хранилище недоступно — по умолчанию
  }

  // [момент, процент, подпись, этап]: этап 1 — инициализация, 2 — модули, 3 — финал, 4 — готово
  var sec = storedSec(), T = 0, STAGES = [], HOLD = 0, FINISH = 0, LEAVE_AFTER = 0, SLOW_AFTER = 0, TIP_FROM = 0, TIP_EVERY = 0;
  var t0 = performance.now(), isReady = false, finalFrom = -1, finalAt = 0, shown = 0, stage = -1, done = false;
  var tipIndex = -1, previewing = false;

  function plan(s) {
    T = quick ? Math.min(s * 1000, QUICK_MS) : s * 1000;
    STAGES = [[0, 0, 'Подготовка…', 0], [0.15 * T, 25, 'Инициализация…', 1], [0.40 * T, 68, 'Подключение модулей…', 2],
      [0.62 * T, 90, 'Финальная настройка…', 3], [0.80 * T, 100, 'Готово к запуску', 4]];
    HOLD = 0.62 * T;
    FINISH = Math.max(160, 0.14 * T);
    LEAVE_AFTER = Math.max(80, 0.08 * T);
    SLOW_AFTER = Math.max(10000, T + 4000);
    TIP_FROM = 0.12 * T;
    TIP_EVERY = Math.max(3000, T / 7);
    root.style.setProperty('--ym-t', Math.max(0.3, Math.min(3, T / 3300)).toFixed(3));
    root.setAttribute('data-length', s <= 2 ? 'short' : (s >= 8 ? 'long' : 'normal'));
    if (tip && s < 8) tip.hidden = true;
  }

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
        return a[1] + (b[1] - a[1]) * ease((t - a[0]) / Math.max(1, b[0] - a[0]));
      }
    }
    return 100;
  }

  function swap(el) {
    el.classList.remove('is-swap');
    void el.offsetWidth;             // перезапуск анимации смены подписи
    el.classList.add('is-swap');
  }

  function setStage(next) {
    if (next === stage) return;
    stage = next;
    root.setAttribute('data-stage', String(next));
    var label = STAGES[Math.min(next, 4)][2];
    if (status) { status.textContent = label; swap(status); }
    for (var i = 0; i < steps.length; i++) {
      var n = i + 1;
      steps[i].setAttribute('data-state', next > n ? 'done' : next === n ? 'active' : 'pending');
    }
    if (next >= 4) {
      for (var j = 0; j < steps.length; j++) steps[j].setAttribute('data-state', 'done');
      root.classList.add('is-ready');
    }
  }

  function paintTip(t) {           // длинная заставка: подсказки сменяются каждые T/7 (не чаще 3 с)
    if (!tip || root.getAttribute('data-length') !== 'long' || t < TIP_FROM) return;
    var next = Math.floor((t - TIP_FROM) / TIP_EVERY) % TIPS.length;
    if (next === tipIndex) return;
    tipIndex = next;
    tip.textContent = TIPS[next];
    tip.hidden = false;
    swap(tip);
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

  function leave(instant) {
    if (done) return;
    done = true;
    var finish = function () {
      root.classList.add('is-gone');
      if (previewing) { previewing = false; return; }        // просмотр из настроек — без события загрузки
      try { document.dispatchEvent(new CustomEvent('ym-splash-done')); } catch (e) { /* старый браузер */ }
    };
    if (instant) { finish(); return; }
    root.classList.add('is-leaving');
    window.setTimeout(finish, reduce ? 50 : 540);
  }

  function frame(now) {
    if (done) return;
    var t = now - t0;
    var p;
    if (finalFrom >= 0) {              // финиш: от текущего значения до 100 %
      var k = Math.min(1, (now - finalAt) / FINISH);
      p = finalFrom + (100 - finalFrom) * ease(k);
      if (k >= 1) { paint(100); window.setTimeout(leave, LEAVE_AFTER); return; }
    } else if (t < HOLD || !isReady) {
      p = natural(Math.min(t, HOLD));
      if (!isReady && t > HOLD) {      // ждём приложение: медленно подбираемся к 94 %
        p = 90 + 4 * (1 - Math.exp(-(t - HOLD) / 4000));
        if (t > SLOW_AFTER && slow) slow.hidden = false;
      }
    } else {
      finalFrom = shown; finalAt = now;
      p = shown;
    }
    paint(p);
    paintTip(t);
    window.requestAnimationFrame(frame);
  }

  function start(s) {
    plan(s);
    t0 = performance.now();
    finalFrom = -1; shown = 0; stage = -1; tipIndex = -1; done = false;
    root.classList.remove('is-gone', 'is-leaving', 'is-ready');
    if (tip) tip.hidden = true;
    paint(0);
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
  // просмотр из настроек можно закрыть щелчком или клавишей Esc
  root.addEventListener('click', function (e) { if (previewing && e.target !== reload) leave(); });
  document.addEventListener('keydown', function (e) { if (previewing && e.key === 'Escape') leave(); });

  window.ymSplash = {
    ready: function () { isReady = true; },
    setVersion: function (v) { if (versionEl && v) versionEl.textContent = String(v).replace(/^v/, ''); },
    // свежая длительность с сервера: запоминаем для следующих загрузок и перестраиваем текущий показ
    setDuration: function (v) {
      var next = clampSec(v);
      try { window.localStorage.setItem(KEY, String(next)); } catch (e) { void e; /* приватный режим */ }
      if (done || next === sec) { sec = next; return; }
      sec = next;
      if (!next) { leave(true); return; }
      plan(next);
    },
    duration: function () { return sec; },
    // v1.70.103: показывать ли заставку гостям — для следующих загрузок (решение принимает boot.js до отрисовки)
    setGuests: function (on) {
      try { window.localStorage.setItem(GUESTS_KEY, on ? '1' : '0'); } catch (e) { void e; /* приватный режим */ }
    },
    // просмотр из настроек: показать заставку с выбранной длительностью (приложение уже готово)
    preview: function (v) {
      var s = clampSec(v === undefined ? sec : v);
      if (!s) return false;
      previewing = true;
      isReady = true;
      start(s);
      return true;
    },
    show: function () { /* заставка показывается только при загрузке */ },
    isDone: function () { return done; },
  };
  if (!sec) { done = true; root.classList.add('is-gone'); return; }   // 0 — без заставки
  // v1.70.103: гостю без заставки (класс ym-guest от boot.js) таймлайн не нужен
  if (document.documentElement.classList.contains('ym-guest')) { done = true; root.classList.add('is-gone'); return; }
  start(sec);
})();
