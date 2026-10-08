// ======================================================================
// Ямастер Чек — веб-клиент (SPA)
// Разработчик и владелец идеи: ООО «Ямастер»
// Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
//
// Роли: Администратор (один, все права) · Бухгалтер (расширенные) ·
//       Пользователь (сканирует и видит свои чеки).
// Регистрация — только по приглашению администратора, роль в приглашении.
// ======================================================================

import { api, getToken, setToken, clearToken } from './api.js';
import { toast, esc, fmtSum, fmtInt, fmtDate, statusLabel, roleLabel, chip, openModal, animateNumber } from './ui.js';
import { injectIcons } from './icons.js';
import { barChart, donutChart } from './charts.js';
import { CameraScanner, decodeImageFile, offlineQueue, parseQrClient } from './scanner.js';
import { packCut, splitBlocks, COL_H, columnX, SCALE, RCPT_W } from './printpack.js'; // v1.24.0: нарезка чеков на 3 колонки

// --------------------------------------------------------------------------
//  Состояние
// --------------------------------------------------------------------------
const state = {
  me: null,
  companies: [],                 // v1.11.0: список компаний (администратор)
  companyFilter: (function () {
    try { return localStorage.getItem('ymaster-company') || 'all'; } catch (e) { return 'all'; }
  })(),
  ws: null,
  wsOk: false,
  updModalOpen: false,           // v1.50.0: открыто окно обновления — баннер не дублируем
  updAutoDismissed: 0,           // v1.52.1: ход этого запуска уже закрыли вручную
  camera: null,
  view: 'dashboard',
  routeParam: '',
  receiptsSelected: new Set(),
  recents: JSON.parse(localStorage.getItem('ymaster_recents') || '[]'),
  // v1.53.0: версия интерфейса этой страницы — по ней решаем,
  // нужна ли перезагрузка после обновления
  bootVersion: (function () {
    try {
      const s = document.querySelector('script[src*="app.js?v="]');
      const m = s && s.src.match(/app\.js\?v=([0-9.]+)/);
      return m ? m[1] : '';
    } catch (e) { return ''; }
  })(),
};

const VIEW_TITLES = {
  dashboard: 'Дашборд', scan: 'Сканирование чеков', receipts: 'База чеков',
  export: 'Выгрузка в 1С', mapping: 'Маппинг реквизитов', users: 'Пользователи и приглашения',
  audit: 'Журнал действий', settings: 'Настройки', companies: 'Компании',
  public: 'Сдать чек в Чек-Пул',       // v1.31.0: доступно и гостям
  partners: 'Партнёры и кэшбэк',       // v1.38.0: партнёрская программа
  my: 'Мой Чек-Пул',                   // v1.32.0: кабинет участника пула
  pooladmin: 'Чек-Пул: модерация',     // v1.33.0: панель админа
  poolpeople: 'Чек-Пул: участники',    // v1.41.0: список + просмотр кабинета
  fraud: 'Чек-Пул: антифрод',          // v1.34.0: сигналы и карантин
  poolpick: 'Чек-Пул: подбор для компании', // v1.37.0: бухгалтер
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

const compName = (c) => (c && (c.display_name || c.short_name)) ? (c.display_name || c.short_name) : (c ? c.name : '');
const isAccountant = () => state.me && (state.me.role === 'admin' || state.me.role === 'accountant');
const isAdmin = () => state.me && state.me.role === 'admin';

// --------------------------------------------------------------------------
//  Точка входа
// --------------------------------------------------------------------------
// v1.3.0: версия приложения — единый источник /api/v1/about (никаких хардкодов)
async function loadAppVersion() {
  try {
    const about = await api.get('/api/v1/about', { retries: 1 });
    state.appVersion = about.version;
    const els = [$('#brand-version'), $('#footer-version')];
    els.forEach(el => { if (el) el.textContent = 'v' + about.version; });
  } catch { /* версия не критична */ }
}

async function boot() {
  injectIcons();
  registerServiceWorker();
  bindShell();
  showSplash(true);
  loadAppVersion();

  // Регистрация по приглашению: #/register/<token>
  const m = location.hash.match(/^#\/register\/(.+)$/);
  if (m) { showSplash(false); showRegister(m[1]); return; }

  // Есть сохранённый токен — проверяем его (сеть могла «просыпаться»)
  if (getToken()) {
    for (let attempt = 0; attempt < 6; attempt++) {
      try {
        state.me = await api.get('/api/v1/auth/me', { retries: 1 });
        enterApp();
        showSplash(false);
        return;
      } catch (e) {
        if (e && e.status === 401) break;         // токен реально недействителен
        // Сетевая проблема — НЕ стираем токен, ждём и пробуем снова
        await new Promise(r => setTimeout(r, 1200 * (attempt + 1)));
      }
    }
    clearToken();
  }
  showSplash(false);
  // v1.31.0: гостевая страница «Сдать чек» — работает без входа в систему
  if (location.hash.startsWith('#/public')) { showPublicScreen(); return; }
  // v1.32.0: кабинет «Чек-Пула» — тоже доступен без входа в программу
  if (location.hash.startsWith('#/my')) { showPoolScreen(); return; }
  const pm = location.hash.match(/^#\/pool-magic\/(.+)$/);
  if (pm) { poolConsume('magic', pm[1]); return; }        // ссылка из письма
  const pv = location.hash.match(/^#\/pool-verify\/(.+)$/);
  if (pv) { poolConsume('verify', pv[1]); return; }       // подтверждение e-mail
  const rr = location.hash.match(/^#\/r\/([A-Za-z0-9-]{3,16})$/);
  if (rr) { poolRefSave(rr[1]); return; }                 // v1.35.0: приглашение
  showLogin();
}

// v1.35.0: код приглашения — храним 30 дней, при регистрации уйдёт на сервер
function poolRefGet() {
  try {
    const v = JSON.parse(localStorage.getItem('ymaster-pool-ref') || 'null');
    if (v && Date.now() - v.at < 30 * 24 * 3600 * 1000) return v.code;
    localStorage.removeItem('ymaster-pool-ref');
  } catch (e) { /* нет доступа к хранилищу */ }
  return '';
}

function poolRefSave(code) {
  try {
    localStorage.setItem('ymaster-pool-ref', JSON.stringify({ code, at: Date.now() }));
  } catch (e) { /* нет доступа к хранилищу */ }
  history.replaceState(null, '', location.pathname + '#/public');
  showPublicScreen();
  toast('Код приглашения применён', 'ok', '🎁');
}

// Экран «Подключение…» — исключает мигание карточки входа при проверке токена
function showSplash(show) {
  let el = document.getElementById('boot-splash');
  if (!el) {
    el = document.createElement('div');
    el.id = 'boot-splash';
    el.style.cssText = 'position:fixed;inset:0;z-index:300;background:#f5f5f5;display:flex;flex-direction:column;gap:14px;align-items:center;justify-content:center;color:#5c5c5c;font-size:14px';   // v1.25.0: светлый
    el.innerHTML = '<img src="/img/logo.svg" width="52" height="52" alt=""><b>Ямастер Чек</b><span class="spinner"></span>';
    document.body.appendChild(el);
  }
  el.style.display = show ? 'flex' : 'none';
}

// v1.54.0: главная страница — статистика пула и заявка на разбор чека.
// Вход (логин/пароль/отпечаток) не затрагивается: работаем только с новыми id.
function bindLanding() {
  const stats = $('#land-stats');
  if (stats) {
    api.get('/api/v1/public/pool/info').then((info) => {
      stats.innerHTML =
        `<span class="land-stat"><b>${fmtInt(info.receipts_total || 0)}</b><small>чеков в базе</small></span>` +
        `<span class="land-stat"><b>${fmtInt(info.verified || 0)}</b><small>проверено</small></span>` +
        `<span class="land-stat"><b>+${info.points_per_receipt ?? 1}</b><small>балл за чек</small></span>`;
    }).catch(() => {});
  }
  const how = $('#land-how-link');
  if (how) how.onclick = (e) => {
    e.preventDefault();
    const el = document.getElementById('land-how');
    if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' });
  };
  const lf = $('#land-lead');
  if (lf) {
    const t0 = Date.now();
    lf.onsubmit = async (e) => {
      e.preventDefault();
      const msg = $('#land-lead-msg'), btn = $('#land-lead-btn');
      btn.disabled = true;
      try {
        const r = await api.post('/api/v1/public/pool/leads', {
          email: $('#land-email').value.trim(),
          consent: !!$('#land-consent').checked,
          hp: $('#land-hp').value,
          form_ms: Date.now() - t0,
        });
        try { localStorage.setItem('ymaster-lead-email', $('#land-email').value.trim()); } catch (err) {}
        msg.textContent = r.message || 'Заявка принята';
        msg.style.color = 'var(--ok)';
        btn.textContent = 'Заявка принята';
      } catch (err) {
        msg.textContent = err.message || 'Не получилось — попробуйте ещё раз';
        msg.style.color = '';
      } finally { btn.disabled = false; }
    };
  }
}

function showLogin() {
  $('#register-screen').classList.add('hidden');
  $('#login-screen').classList.remove('hidden');
  $('#app-shell').classList.add('hidden');
  bindLanding();                            // v1.54.0: лендинг на главной
  waInitLoginScreen();                      // v1.43.0: кнопка входа по ключу
  const form = $('#login-form');
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('#login-submit');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    $('#login-error').classList.add('hidden');
    try {
      const r = await api.post('/api/v1/auth/login', {
        username: $('#login-username').value.trim(),
        password: $('#login-password').value,
      });
      setToken(r.access_token);
      state.me = r.user;
      enterApp();
      maybeOfferPasskey(r.user);            // v1.43.0: предложение включить
    } catch (err) {
      const el = $('#login-error');
      el.textContent = err.message || 'Ошибка входа';
      el.classList.remove('hidden');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Войти в систему';
    }
  };
}

// ==========================================================================
//  v1.43.0: БЫСТРЫЙ ВХОД (WebAuthn / passkey) — отпечаток пальца, лицо
//  или PIN устройства вместо пароля. Биометрия не покидает устройство:
//  сервер хранит только публичный ключ. Ключ привязан к устройству,
//  устройств может быть несколько (Настройки → «Быстрый вход»).
// ==========================================================================
const WA_B64 = {
  enc(buf) {
    const b = new Uint8Array(buf);
    let s = '';
    for (let i = 0; i < b.length; i++) s += String.fromCharCode(b[i]);
    return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '');
  },
  dec(str) {
    const s = str.replace(/-/g, '+').replace(/_/g, '/');
    const pad = '='.repeat((4 - (s.length % 4)) % 4);
    const bin = atob(s + pad);
    const b = new Uint8Array(bin.length);
    for (let i = 0; i < bin.length; i++) b[i] = bin.charCodeAt(i);
    return b.buffer;
  },
};

async function waPlatformAvailable() {
  try {
    if (!window.PublicKeyCredential || !navigator.credentials) return false;
    return await PublicKeyCredential.isUserVerifyingPlatformAuthenticatorAvailable();
  } catch (e) { return false; }
}

// Проверка поддержки: показать кнопку на экране входа
async function waInitLoginScreen() {
  const wrap = $('#login-passkey-wrap');
  if (!wrap) return;
  const ok = await waPlatformAvailable();
  wrap.classList.toggle('hidden', !ok);
  if (ok) $('#login-passkey').onclick = async () => {
    const btn = $('#login-passkey');
    btn.disabled = true;
    btn.textContent = 'Подтвердите себя на устройстве…';
    try {
      const r = await waLoginCore('');
      setToken(r.access_token);
      state.me = r.user;
      enterApp();
    } catch (e) {
      const el = $('#login-error');
      el.textContent = e.waCancelled
        ? 'Вход отменён — подтвердите отпечаток/лицо или введите пароль'
        : (e.message || 'Не удалось войти по ключу устройства');
      el.classList.remove('hidden');
    }
    btn.disabled = false;
    btn.textContent = '🔑 Войти по отпечатку пальца / Face ID';
  };
}

async function waLoginCore(username) {
  const opts = await api.post('/api/v1/auth/passkey/login/options',
    { username: username || null });
  const get = {
    challenge: WA_B64.dec(opts.challenge),
    rpId: opts.rpId,
    timeout: opts.timeout || 60000,
    userVerification: opts.userVerification || 'required',
  };
  if (opts.allowCredentials && opts.allowCredentials.length) {
    get.allowCredentials = opts.allowCredentials.map(c => ({
      type: c.type || 'public-key', id: WA_B64.dec(c.id),
    }));
  }
  let assertion;
  try {
    assertion = await navigator.credentials.get({ publicKey: get });
  } catch (e) {
    const err = new Error('Отменено на устройстве');
    err.waCancelled = true;
    throw err;
  }
  return api.post('/api/v1/auth/passkey/login/verify', {
    id: assertion.id,
    rawId: WA_B64.enc(assertion.rawId),
    type: assertion.type,
    response: {
      challenge_hex: opts.challenge_hex,
      authenticatorData: WA_B64.enc(assertion.response.authenticatorData),
      clientDataJSON: WA_B64.enc(assertion.response.clientDataJSON),
      signature: WA_B64.enc(assertion.response.signature),
      userHandle: assertion.response.userHandle
        ? WA_B64.enc(assertion.response.userHandle) : null,
    },
  });
}

async function waRegisterCore(label) {
  const opts = await api.post('/api/v1/auth/passkey/register/options', {});
  let cred;
  const create = {
    challenge: WA_B64.dec(opts.challenge),
    rp: opts.rp,
    user: {
      id: WA_B64.dec(opts.user.id),
      name: opts.user.name,
      displayName: opts.user.displayName || opts.user.name,
    },
    pubKeyCredParams: opts.pubKeyCredParams,
    timeout: opts.timeout || 60000,
    authenticatorSelection: opts.authenticatorSelection || undefined,
    excludeCredentials: (opts.excludeCredentials || []).map(c => ({
      type: c.type || 'public-key', id: WA_B64.dec(c.id),
    })),
  };
  try {
    cred = await navigator.credentials.create({ publicKey: create });
  } catch (e) {
    const err = new Error('Не удалось создать ключ — устройство отклонило запрос');
    err.waCancelled = true;
    throw err;
  }
  return api.post('/api/v1/auth/passkey/register/verify', {
    id: cred.id,
    rawId: WA_B64.enc(cred.rawId),
    type: cred.type,
    label: label || '',
    response: {
      challenge_hex: opts.challenge_hex,
      attestationObject: WA_B64.enc(cred.response.attestationObject),
      clientDataJSON: WA_B64.enc(cred.response.clientDataJSON),
    },
  });
}

// Предложение после первого входа по паролю (один раз, «Не сейчас» — пауза)
async function maybeOfferPasskey(user) {
  try {
    if (!user || !user.id) return;
    if (user.must_change_password) return;          // сначала смена пароля
    if (!await waPlatformAvailable()) return;
    const made = localStorage.getItem('ymaster-wa-made-' + user.id);
    const skip = localStorage.getItem('ymaster-wa-skip-' + user.id);
    if (made) return;
    if (skip && (Date.now() - Number(skip)) < 30 * 24 * 3600 * 1000) return;
    const { slot, close } = openModal(
      `<div class="modal-title">🔑 Быстрый вход на этом устройстве</div>
       <p class="modal-text">Включить вход по <b>отпечатку пальца</b>, распознаванию
       <b>лица</b> или PIN этого устройства? Пароль больше не понадобится —
       устройство само подтвердит вас. Биометрия не отправляется на сервер.</p>
       <div class="modal-actions">
         <button class="btn" id="wa-skip">Не сейчас</button>
         <button class="btn btn-primary" id="wa-go">Включить</button>
       </div>`);
    $('#wa-skip', slot).onclick = () => {
      try { localStorage.setItem('ymaster-wa-skip-' + user.id, String(Date.now())); }
      catch (e) {}
      close();
    };
    $('#wa-go', slot).onclick = async () => {
      try {
        const r = await waRegisterCore('');
        try { localStorage.setItem('ymaster-wa-made-' + user.id, '1'); }
        catch (e) {}
        close();
        toast(r.message, 'ok', 'Быстрый вход');
      } catch (e) {
        close();
        toast(e.waCancelled ? 'Ключ не создан — устройство отменило запрос'
                            : (e.message || 'Не удалось включить'),
              'err', 'Быстрый вход');
      }
    };
  } catch (e) { /* предложение не критично */ }
}

// --------------------------------------------------------------------------
//  v1.31.0: ПУБЛИЧНАЯ СТРАНИЦА «СДАТЬ ЧЕК В ЧЕК-ПУЛ» (гость — без входа).
//  Одна форма для двух режимов: гостевой экран (#/public без токена) и
//  раздел приложения. Антифрод-минимум: honeypot + тайминг + лимиты сервера.
// --------------------------------------------------------------------------
function poolStatusChip(st) {
  if (st === 'verified') return '<span class="chip exported"><span class="dot"></span>Принят в пул</span>';
  if (st === 'pending') return '<span class="chip new"><span class="dot"></span>На ручной проверке</span>';
  if (st === 'rejected') return '<span class="chip failed"><span class="dot"></span>Не принят</span>';
  return '<span class="chip"><span class="dot"></span>' + esc(st) + '</span>';
}

// ==========================================================================
//  v1.51.0: живая камера там, где сдаётся чек — тот же модуль scanner.js,
//  что в разделе «Сканирование»: распознавание в браузере, антисбливание.
// ==========================================================================
function openPoolCameraScan(onText) {
  let cam = null;
  const { slot, close } = openModal(`
    <div class="modal-title">📸 Сканирование QR-кода чека</div>
    <div class="camera-viewport" style="max-width:440px">
      <video id="poolcam-video" playsinline muted></video>
      <div class="scan-frame" id="poolcam-frame" style="display:none"><span class="corner"></span></div>
      <div class="scan-line" id="poolcam-line" style="display:none"></div>
      <div class="camera-overlay" id="poolcam-overlay">
        <span class="big-ico">📷</span>
        <div>Наведите камеру на QR-код чека.<br>Код распознаётся автоматически.</div>
        <button class="btn btn-primary" id="poolcam-start">▶ Включить камеру</button>
      </div>
    </div>
    <p class="form-hint" style="margin-top:8px">Совет: QR с экрана другого телефона тоже сканируется.
    Чужой QR-код (не чек) игнорируется — ждём реквизиты 54-ФЗ.</p>`,
    { onClose: () => { if (cam) { cam.stop(); cam = null; } } });
  const start = slot.querySelector('#poolcam-start');
  if (start) start.onclick = async () => {
    const video = slot.querySelector('#poolcam-video');
    try {
      cam = new CameraScanner(video, (text) => {
        const p = parseQrClient(text);
        if (!p) return;                       // не чековый QR — ждём дальше
        if (cam) { cam.stop(); cam = null; }
        close();
        onText(text, p);
      });
      await cam.start();
      slot.querySelector('#poolcam-overlay').classList.add('hidden');
      slot.querySelector('#poolcam-frame').style.display = '';
      slot.querySelector('#poolcam-line').style.display = '';
      toast('Камера включена — наведите на QR-код чека', 'info', '📸');
    } catch (e) { toast(e.message, 'err', 'Камера недоступна'); }
  };
}

// v1.52.0: ручной ввод реквизитов там же, где сканируют (резерв —
// QR повреждён). Собирает стандартную строку QR — дальше работает
// обычная проверка и отправка, ничего дублировать не нужно.
function poolManualDialog(onQr) {
  const { slot } = openModal(`
    <div class="modal-title">⌨ Ручной ввод реквизитов чека</div>
    <p class="form-hint" style="margin-bottom:12px">Резервный способ — когда QR повреждён
    или не читается. Реквизиты напечатаны на самом чеке.</p>
    <div class="form-grid">
      <label class="field full"><span>Дата и время чека</span>
        <input id="pm-date" placeholder="22.09.2025 14:30" required></label>
      <label class="field"><span>Сумма, ₽</span>
        <input id="pm-sum" type="number" step="0.01" min="0.01" placeholder="1500.00" required></label>
      <label class="field"><span>Признак расчёта</span>
        <select id="pm-op"><option value="1">Приход</option><option value="2">Возврат прихода</option></select></label>
      <label class="field"><span>ФН (8–20 цифр)</span>
        <input id="pm-fn" placeholder="9999078902001234" required></label>
      <label class="field"><span>ФД</span>
        <input id="pm-fd" placeholder="12345" required></label>
      <label class="field full"><span>ФП / ФПД</span>
        <input id="pm-fp" placeholder="1234567890" required></label>
    </div>
    <div class="modal-actions">
      <button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" id="pm-send">Собрать QR</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
  slot.querySelector('#pm-send').onclick = () => {
    const v = (id) => slot.querySelector(id).value.trim();
    const m = v('#pm-date').match(/^(\d{2})\.(\d{2})\.(\d{4})\s+(\d{2}):(\d{2})$/);
    if (!m) { toast('Дата в формате 22.09.2025 14:30', 'err', 'Проверьте реквизиты'); return; }
    const fn = v('#pm-fn'), fd = v('#pm-fd'), fp = v('#pm-fp');
    if (!/^\d{8,20}$/.test(fn) || !/^\d{1,10}$/.test(fd) || !/^\d{1,10}$/.test(fp)) {
      toast('ФН — 8–20 цифр, ФД и ФП — только цифры', 'err', 'Проверьте реквизиты'); return;
    }
    const sum = parseFloat(v('#pm-sum').value.replace(',', '.'));
    if (!(sum > 0)) { toast('Укажите сумму чека', 'err', 'Проверьте реквизиты'); return; }
    const qr = `t=${m[3]}${m[2]}${m[1]}T${m[4]}${m[5]}&s=${sum.toFixed(2)}`
      + `&fn=${fn}&i=${fd}&fp=${fp}&n=${v('#pm-op')}`;
    $('#modal-root').classList.add('hidden');
    onQr(qr);
  };
}

function publicFormHTML() {
  return `
  <div id="pub-ref" class="info-callout hidden" style="margin-bottom:8px"></div>
  <div id="pub-info" class="form-hint" style="margin-bottom:10px">Загружаем…</div>
  <label class="field"><span>Строка QR или ссылка из приложения «Проверка чеков ФНС»</span>
    <textarea id="pub-qr" rows="3" placeholder="t=20260905T1430&s=1250.00&fn=...&i=...&fp=...&n=1"></textarea></label>
  <label class="field"><span>E-mail сотрудника компании — если чек сдаёте по авансовому отчёту (необязательно)</span>
    <input id="pub-emp" type="email" placeholder="ivanov@company.ru" autocomplete="email" style="max-width:340px"></label>
  <div class="form-hint" style="margin:2px 0 8px">Подтвердите e-mail в кабинете — чеки автоматически будут уходить
    вашей компании, минуя общий пул.</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
    <button class="btn btn-sm btn-primary" id="pub-cam"
            title="Живое сканирование камерой — как в разделе «Сканирование»">📸 Сканировать камерой</button>
    <button class="btn btn-sm" id="pub-manual"
            title="Резервный способ: QR повреждён — реквизиты с чека">⌨ Ввести вручную</button>
    <label class="btn btn-sm" style="cursor:pointer;margin:0">📷 Фото QR
      <input id="pub-photo" type="file" accept="image/*" capture="environment" class="hidden"></label>
    <span class="form-hint" style="align-self:center">фото чека можно просто перетащить на форму</span>
  </div>
  <div id="pub-photo-hint" class="form-hint hidden" style="margin:4px 0 8px"></div>
  <input id="pub-hp" type="text" tabindex="-1" autocomplete="off" aria-hidden="true"
         style="position:absolute;left:-9999px;top:-9999px;height:1px;width:1px">
  <label style="display:flex;gap:10px;align-items:flex-start;cursor:pointer;margin:10px 0">
    <input type="checkbox" id="pub-offerta" style="width:auto;margin-top:3px">
    <span style="font-size:13.5px">Согласен с офертой (<a href="#" id="pub-offerta-link">текст</a>) —
    чек мой, передаю его фискальные данные в открытую базу</span></label>
  <div id="pub-offerta-text" class="info-callout hidden" style="font-size:13px;margin-bottom:10px"></div>
  <button class="btn btn-primary btn-block" id="pub-submit">Отправить чек — получить балл</button>
  <div id="pub-msg" class="form-error hidden" style="margin-top:8px"></div>
  <div id="pub-leaders" style="margin-top:14px"></div>
  <div id="pub-status" class="hidden" style="margin-top:12px"></div>
  <div id="pub-mine" style="margin-top:14px"></div>`;
}

function bindPoolForm(root, opts) {
  const t0 = Date.now();
  const $p = (id) => root.querySelector('#' + id);
  const show = (id, on) => { const el = $p(id); if (el) el.classList.toggle('hidden', !on); };

  const loadMine = async () => {
    try {
      const my = await poolApi('GET', '/api/v1/public/pool/my');
      const mine = $p('pub-mine');
      if (mine) {
        mine.innerHTML = `
        <div class="form-hint" style="margin-bottom:6px">Ваши баллы: <b>${fmtInt(my.points)}</b> ·
          сегодня чеков: ${my.today} из ${my.daily_limit}</div>
        ${my.receipts.length ? `<div class="table-wrap"><table style="width:100%">
          <thead><tr><th>Когда</th><th>Магазин</th><th>Сумма</th><th>Статус</th><th>Баллы</th></tr></thead>
          <tbody>${my.receipts.map(r => `<tr>
            <td class="num">${esc((r.created_at || '').slice(0, 16).replace('T', ' '))}</td>
            <td>${esc(r.merchant_name || '—')}</td>
            <td class="num">${fmtSum(r.total_sum)}</td>
            <td>${poolStatusChip(r.status)}</td>
            <td class="num">${r.points ? '+' + r.points : '—'}</td></tr>`).join('')}</tbody>
        </table></div>` : ''}`;
      }
      return my;
    } catch (e) { return null; }
  };

  // v1.51.0: единый скан-механизм (фото / перетаскивание / камера) во всех
  // местах сдачи чека — распознавание то же, что в разделе «Сканирование»
  const decodeToQr = async (f) => {
    const hint = $p('pub-photo-hint');
    show('pub-photo-hint', true);
    if (hint) hint.textContent = 'Ищу QR на фото…';
    try {
      const { raws, valid } = await decodeImageFile(f);
      if (!raws.length) { if (hint) hint.textContent = 'QR на фото не найден — попробуйте крупнее и чётче'; return; }
      $p('pub-qr').value = raws[0];
      const p = valid[0];
      if (hint) hint.textContent = 'QR распознан: ФН ' + p.fn + (p.sum ? ', сумма ' + p.sum : '') + '. Осталось отметить оферту и отправить.';
    } catch (e) { if (hint) hint.textContent = e.message || 'Не удалось прочитать фото'; }
  };
  const photo = $p('pub-photo');
  if (photo) photo.onchange = async () => {
    const f = photo.files && photo.files[0];
    if (!f) return;
    await decodeToQr(f);
    photo.value = '';
  };
  // фото можно перетащить прямо на форму
  root.addEventListener('dragover', (e) => e.preventDefault());
  root.addEventListener('drop', (e) => {
    e.preventDefault();
    const fs = [...((e.dataTransfer && e.dataTransfer.files) || [])]
      .filter((f) => /^image\//.test(f.type));
    if (fs.length) decodeToQr(fs[0]);
  });
  // живая камера — тот же модуль scanner.js, что в разделе «Сканирование»
  const camPick = () => openPoolCameraScan((text, p) => {
    $p('pub-qr').value = text;
    const hint = $p('pub-photo-hint');
    show('pub-photo-hint', true);
    if (hint) hint.textContent = 'QR отсканирован камерой: ФН ' + p.fn
      + (p.sum ? ', сумма ' + p.sum : '') + '. Осталось отметить оферту и отправить.';
  });
  const camBtn = $p('pub-cam');
  if (camBtn) camBtn.onclick = camPick;
  // v1.54.0: пришли с главной «Сканировать чек» — камера открывается сама
  if (/[?&]cam=1/.test(location.hash || '')) setTimeout(camPick, 350);
  // v1.52.0: резервный ввод реквизитов (QR повреждён) — тот же конвейер
  const manBtn = $p('pub-manual');
  if (manBtn) manBtn.onclick = () => poolManualDialog((qr) => {
    $p('pub-qr').value = qr;
    const hint = $p('pub-photo-hint');
    show('pub-photo-hint', true);
    if (hint) hint.textContent = 'Реквизиты собраны в строку QR — проверьте и отправьте.';
  });

  const pollStatus = (fn) => {
    const box = $p('pub-status');
    show('pub-status', true);
    box.innerHTML = '<div class="chip new"><span class="dot"></span>Чек принят — проверяем по официальным источникам…</div>';
    let tries = 0;
    const timer = setInterval(async () => {
      tries += 1;
      const my = await loadMine();
      const row = my && my.receipts.find(r => r.fn === fn);
      if (row) {
        clearInterval(timer);
        // v1.51.0: кабинет обновляет сводку, когда чек дошёл до проверки
        if (opts && typeof opts.onDone === 'function') {
          try { opts.onDone(row); } catch (e) { /* не мешаем показу статуса */ }
        }
        const extra = row.status === 'verified'
          ? `Балл начислен — всего у вас <b>${fmtInt(my.points)}</b>.`
          : (row.status === 'pending'
              ? 'Чек на ручной проверке — это не ошибка, администратор посмотрит его вручную.'
              : esc(row.message || 'Источник не нашёл чек — проверьте строку QR.'));
        box.innerHTML = `<div class="info-callout">${poolStatusChip(row.status)}
          <div style="margin-top:6px">${extra}</div></div>`;
        // v1.55.0: электронный чек — человек сразу видит, что сервис работает
        try {
          const full = await poolApi('GET', '/api/v1/public/pool/receipt/' + row.id);
          renderEcheck(box, full);
        } catch (e) {
          const mb = document.createElement('button');
          mb.className = 'btn btn-sm'; mb.style.marginTop = '8px';
          mb.textContent = '✉ Прислать данные чека на e-mail';
          mb.onclick = () => mailReceiptDialog(row);
          box.firstElementChild.appendChild(mb);
        }
      } else if (tries > 30) {
        clearInterval(timer);
        box.innerHTML = '<div class="info-callout">Проверка занимает больше минуты — статус появится в списке ниже.</div>';
      }
    }, 2000);
  };

  // v1.54.0: отправка разобранных данных чека на почту (без регистрации)
  const mailReceiptDialog = (row) => {
    let saved = '';
    try { saved = localStorage.getItem('ymaster-lead-email') || ''; } catch (e) {}
    if (!saved) { const emp = $p('pub-emp'); if (emp) saved = emp.value.trim(); }
    const { slot } = openModal(`
      <div class="modal-title">✉ Данные чека на почту</div>
      <p class="form-hint" style="margin-bottom:10px">Пришлём письмо с реквизитами чека
      (магазин, сумма, дата, статус проверки). Регистрация не нужна.</p>
      <label class="field"><span>E-mail</span>
        <input type="email" id="rcpt-email" required value="${esc(saved)}" autocomplete="email"></label>
      <div class="modal-actions">
        <button class="btn" data-close>Отмена</button>
        <button class="btn btn-primary" id="rcpt-send">Отправить</button>
      </div>`);
    slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
    slot.querySelector('#rcpt-send').onclick = async (e) => {
      const btn = e.target;
      btn.disabled = true;
      try {
        const email = slot.querySelector('#rcpt-email').value.trim();
        const r = await poolApi('POST', '/api/v1/public/pool/receipt-email',
          { fn: row.fn || '', email, hp: '' });
        try { localStorage.setItem('ymaster-lead-email', email); } catch (err) {}
        $('#modal-root').classList.add('hidden');
        toast(r.message, 'ok', '✉ Чек-Пул');
      } catch (err) {
        toast(err.message, 'err', '✉ Чек-Пул');
        btn.disabled = false;
      }
    };
  };

  const submit = $p('pub-submit');
  if (submit) submit.onclick = async () => {
    const msg = $p('pub-msg');
    msg.classList.add('hidden');
    submit.disabled = true;
    try {
      const res = await poolApi('POST', '/api/v1/public/pool/check', {
        qr_text: $p('pub-qr').value,
        offerta: !!$p('pub-offerta').checked,
        hp: $p('pub-hp').value,
        form_ms: Date.now() - t0,
        employee_email: ($p('pub-emp') ? $p('pub-emp').value.trim() : ''),
      });
      if (!res.ok || !res.accepted) {
        msg.textContent = res.message || 'Не получилось отправить чек';
        msg.classList.remove('hidden');
        return;
      }
      pollStatus(res.fn);
    } catch (e) {
      msg.textContent = e.message || 'Ошибка сети';
      msg.classList.remove('hidden');
    } finally { submit.disabled = false; }
  };

  const olink = $p('pub-offerta-link');
  if (olink) olink.onclick = (e) => {
    e.preventDefault();
    show('pub-offerta-text', $p('pub-offerta-text').classList.contains('hidden'));
  };

  // v1.35.0: баннер «вас пригласили» — код из ссылки #/r/КОД
  const refCode = poolRefGet();
  if (refCode) {
    api.get('/api/v1/public/pool/ref/' + encodeURIComponent(refCode)).then((ri) => {
      if (!ri.valid) return;
      const el = $p('pub-ref');
      if (el) {
        el.classList.remove('hidden');
        el.innerHTML = `🎁 Вас пригласил(а) <b>${esc(ri.label)}</b>. Сдавайте чеки,
          регистрируйтесь в кабинете — пригласивший получит баллы за ваши первые шаги.`;
      }
    }).catch(() => {});
  }

  (async () => {
    try {
      const info = await api.get('/api/v1/public/pool/info');
      if (!info.enabled) {
        root.innerHTML = '<div class="info-callout">Приём чеков сейчас выключен — загляните позже.</div>';
        return;
      }
      const box = $p('pub-info');
      if (box) box.innerHTML = `В пуле уже <b>${fmtInt(info.receipts_total)}</b> чеков, из них проверено
        <b>${fmtInt(info.verified)}</b>. 1 чек = ${info.points_per_receipt} балл ·
        лимит ${info.daily_limit} чеков/сутки.`;
      const ot = $p('pub-offerta-text');
      if (ot) ot.textContent = info.offerta;
      poolLoadLeadersPub(root);          // v1.36.0: лидерборд месяца
    } catch (e) { /* сеть могла мигнуть — форма остаётся */ }
    await loadMine();
  })();
}

function showPublicScreen() {
  $('#register-screen').classList.add('hidden');
  $('#login-screen').classList.add('hidden');
  $('#app-shell').classList.add('hidden');
  const root = $('#public-root');
  root.innerHTML = publicFormHTML();
  const back = document.getElementById('public-to-login');
  if (back) back.onclick = (e) => {
    e.preventDefault();
    history.replaceState(null, '', location.pathname);
    showLogin();
  };
  bindPoolForm(root);
}

// v1.31.0: тот же приём — разделом приложения (для вошедших сотрудников)
async function viewPublic(container) {
  container.innerHTML = `
  <div class="glass card" style="max-width:760px">
    <div class="card-title">🧾 Сдать чек в Чек-Пул <span class="form-hint">(Этап 2 · v1.31.0)</span></div>
    <p class="form-hint" style="margin-bottom:10px">Чек попадает в открытую базу «Ямастер Чек-Пул»:
    проверяем по официальным источникам (ФНС → Честный Знак, без квот) и начисляем балл.
    Это не влияет на корпоративные чеки компании.</p>
    ${publicFormHTML()}
  </div>`;
  bindPoolForm(container);
}

// --------------------------------------------------------------------------
//  v1.32.0: КАБИНЕТ УЧАСТНИКА «ЧЕК-ПУЛА» (Этап 3).
//  Отдельный контур от рабочей программы: свой токен (typ=pool), свой вход.
//  Гостевая история (cookie vid) при регистрации/входе присоединяется
//  к аккаунту. Доступен как публичный экран (#/my), так и раздел приложения.
// --------------------------------------------------------------------------
const POOL_TK = 'ymaster-pool-token';
const pGet = () => { try { return localStorage.getItem(POOL_TK) || ''; } catch (e) { return ''; } };
const pSet = (t) => { try { localStorage.setItem(POOL_TK, t); } catch (e) {} };
const pClear = () => { try { localStorage.removeItem(POOL_TK); } catch (e) {} };

// v1.34.0: обезличенный отпечаток устройства (антифрод, слой Device).
// Считается локально из технических признаков браузера, персональные
// данные не собираются; на сервере хранится только хэш ≤ 12 месяцев.
function poolFp() {
  try {
    let fp = localStorage.getItem('ymaster-pool-fp');
    if (fp) return fp;
    const parts = [
      navigator.userAgent, navigator.language,
      (navigator.languages || []).join(','), screen.width + 'x' + screen.height,
      screen.colorDepth, Intl.DateTimeFormat().resolvedOptions().timeZone,
      String(navigator.hardwareConcurrency || 0),
    ];
    let h = 0;
    const str = parts.join('|');
    for (let i = 0; i < str.length; i++) { h = (h * 31 + str.charCodeAt(i)) >>> 0; }
    fp = 'fp' + h.toString(36) + str.length.toString(36);
    localStorage.setItem('ymaster-pool-fp', fp);
    return fp;
  } catch (e) { return ''; }
}

async function poolApi(method, path, body) {
  const headers = { 'Content-Type': 'application/json' };
  const fp = poolFp();
  if (fp) headers['X-Visitor-Id'] = fp;      // v1.34.0: антифрод (хэш на сервере)
  if (pGet()) headers.Authorization = 'Bearer ' + pGet();
  const r = await fetch(path, { method, headers, body: body ? JSON.stringify(body) : undefined });
  let data = null;
  try { data = await r.json(); } catch (e) { /* пустой ответ */ }
  if (!r.ok) {
    const err = new Error((data && (data.detail || data.message)) || 'Ошибка');
    err.status = r.status;
    throw err;
  }
  return data;
}

// magic-ссылка / подтверждение e-mail из письма
async function poolConsume(kind, raw) {
  history.replaceState(null, '', location.pathname);
  try {
    const r = await poolApi('POST', kind === 'magic'
      ? '/api/v1/pool-auth/magic/consume' : '/api/v1/pool-auth/verify', { token: raw });
    if (kind === 'magic' && r.token) { pSet(r.token); toast('Вход выполнен', 'ok', 'Чек-Пул'); }
    else toast(r.message || 'Готово', 'ok', 'Чек-Пул');
  } catch (e) { toast(e.message || 'Ссылка недействительна', 'err', 'Чек-Пул'); }
  showPoolScreen();
}

function poolAuthHTML() {
  return `
  <div style="display:flex;gap:8px;margin-bottom:12px">
    <button class="btn btn-sm btn-primary" id="ptab-login">Вход</button>
    <button class="btn btn-sm" id="ptab-reg">Регистрация</button>
    <button class="btn btn-sm" id="ptab-magic">Вход по ссылке</button>
  </div>
  <form id="pf-login">
    <label class="field"><span>E-mail</span><input id="pl-email" type="email" required autocomplete="email"></label>
    <label class="field"><span>Пароль</span><input id="pl-pass" type="password" required autocomplete="current-password"></label>
    <button class="btn btn-primary btn-block" type="submit">Войти в кабинет</button>
  </form>
  <form id="pf-reg" class="hidden">
    <div id="p-ref-hint" class="info-callout hidden" style="margin-bottom:8px"></div>
    <label class="field"><span>E-mail</span><input id="pr-email" type="email" required autocomplete="email"></label>
    <label class="field"><span>Придумайте пароль (мин. 8 символов)</span><input id="pr-pass" type="password" required minlength="8" autocomplete="new-password"></label>
    <p class="form-hint" style="margin:4px 0 8px">Чеки, сданные в этом браузере без регистрации, присоединятся к кабинету.</p>
    <button class="btn btn-primary btn-block" type="submit">Создать кабинет</button>
  </form>
  <form id="pf-magic" class="hidden">
    <label class="field"><span>E-mail</span><input id="pm-email" type="email" required></label>
    <p class="form-hint" style="margin:4px 0 8px">Пришлём одноразовую ссылку для входа (если администратор настроил почту).</p>
    <button class="btn btn-primary btn-block" type="submit">Прислать ссылку</button>
  </form>
  <div id="pool-auth-msg" class="form-error hidden" style="margin-top:8px"></div>`;
}

function poolDashHTML() {
  return `
  <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin-bottom:12px">
    <button class="btn btn-primary btn-sm" id="pool-submit-open"
            title="Камера, фото или строка QR — как на странице «Сдать чек»">📸 Сдать чек сканером</button>
    <span class="form-hint">камера, фото или вставка строки — баллы зачисляются автоматически</span>
  </div>
  <div id="pool-summary" class="form-hint">Загружаем…</div>
  <div class="form-grid" style="margin:10px 0">
    <label class="field"><span>Баллы</span><input id="pool-pts" value="—" disabled></label>
    <label class="field"><span>Мои чеки</span><input id="pool-cnt" value="—" disabled></label>
    <label class="field"><span>Сегодня сдано</span><input id="pool-today" value="—" disabled></label>
  </div>
  <div id="pool-receipts"></div>
  <div class="card-title" style="margin-top:14px">🎁 Приглашайте — баллы вам и друзьям
    <span class="form-hint">(v1.35.0)</span></div>
  <div id="pool-invite" class="form-hint" style="margin:6px 0">Загружаем…</div>
  <div class="card-title" style="margin-top:14px">🏆 Ачивки, лидерборд и вывод баллов
    <span class="form-hint">(Этап 7 · v1.36.0)</span></div>
  <div id="pool-engage" class="form-hint" style="margin:6px 0">Загружаем…</div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">
    <button class="btn btn-sm" id="pool-csv">⬇️ CSV (Excel)</button>
    <button class="btn btn-sm" id="pool-print">🖨 Печать / PDF</button>
  </div>
  <div class="card-title" style="margin-top:14px">Профиль</div>
  <div class="form-grid" style="margin:8px 0">
    <label class="field"><span>Старый пароль</span><input id="pool-oldp" type="password" autocomplete="current-password"></label>
    <label class="field"><span>Новый пароль (мин. 8 символов)</span><input id="pool-newp" type="password" autocomplete="new-password"></label>
  </div>
  <button class="btn btn-sm" id="pool-pass-save">Сменить пароль</button>
  <div class="form-grid" style="margin:8px 0">
    <label class="field"><span>Новый e-mail</span><input id="pool-newemail" type="email"></label>
  </div>
  <button class="btn btn-sm" id="pool-email-save">Сменить e-mail</button>
  <div style="display:flex;gap:8px;margin-top:16px">
    <button class="btn btn-sm" id="pool-logout">Выйти из кабинета</button>
    <button class="btn btn-sm" id="pool-delete" style="color:#b3261e">Удалить аккаунт</button>
  </div>
  <div id="pool-msg" class="form-error hidden" style="margin-top:8px"></div>`;
}

function poolShowErr(root, id, msg) {
  const el = root.querySelector(id);
  if (el) { el.textContent = msg; el.classList.remove('hidden'); }
}

async function poolLoadSummary(root) {
  try {
    const d = await poolApi('GET', '/api/v1/pool-my/summary');
    const box = root.querySelector('#pool-summary');
    if (box) box.innerHTML = `Кабинет: <b>${esc(d.email)}</b>
      ${d.email_verified ? '<span class="chip exported"><span class="dot"></span>e-mail подтверждён</span>'
      : `<span class="chip new"><span class="dot"></span>e-mail не подтверждён</span>
         <a href="#" id="pool-resend-verify">Подтвердить</a>`}`;
    const rv = root.querySelector('#pool-resend-verify');
    if (rv) rv.onclick = async (e) => {
      e.preventDefault();
      try { toast((await poolApi('POST', '/api/v1/pool-my/resend-verification')).message, 'ok', 'Чек-Пул'); }
      catch (err) { toast(err.message, 'err', 'Чек-Пул'); }
    };
    const pts = root.querySelector('#pool-pts');
    if (pts) pts.value = fmtInt(d.points);
    const cnt = root.querySelector('#pool-cnt');
    if (cnt) cnt.value = fmtInt(d.receipts_total);
    const today = root.querySelector('#pool-today');
    if (today) today.value = d.today + ' из ' + d.daily_limit;
    return d;
  } catch (e) {
    if (e.status === 401) { pClear(); root.innerHTML = poolAuthHTML(); bindPoolAuth(root); }
    return null;
  }
}

async function poolLoadReceipts(root, page) {
  try {
    const d = await poolApi('GET', `/api/v1/pool-my/receipts?page=${page}&page_size=10`);
    const box = root.querySelector('#pool-receipts');
    if (!box) return;
    const pages = Math.max(1, Math.ceil(d.total / d.page_size));
    box.innerHTML = `<div class="card-title" style="font-size:15px">Мои чеки
        <span class="form-hint">(всего ${fmtInt(d.total)})</span></div>
      ${d.items.length ? `<div class="table-wrap"><table style="width:100%">
      <thead><tr><th>Сдан</th><th>Магазин</th><th>Сумма</th><th>Статус</th><th>Баллы</th></tr></thead>
      <tbody>${d.items.map(r => `<tr data-rid="${r.id}" style="cursor:pointer">
        <td class="num">${esc((r.created_at || '').slice(0, 16).replace('T', ' '))}</td>
        <td>${esc(r.merchant_name || '—')}</td>
        <td class="num">${fmtSum(r.total_sum)}</td>
        <td>${poolStatusChip(r.status)}</td>
        <td class="num">${r.points ? '+' + r.points : '—'}</td></tr>`).join('')}</tbody></table></div>`
      : '<p class="form-hint">Пока пусто — сдайте первый чек на странице «Сдать чек».</p>'}
      ${pages > 1 ? `<div style="display:flex;gap:8px;align-items:center;margin-top:6px">
        <button class="btn btn-sm" id="pool-prev" ${page <= 1 ? 'disabled' : ''}>← Назад</button>
        <span class="form-hint">стр. ${page} из ${pages}</span>
        <button class="btn btn-sm" id="pool-next" ${page >= pages ? 'disabled' : ''}>Вперёд →</button></div>` : ''}`;
    box.querySelectorAll('tr[data-rid]').forEach(tr => {
      tr.onclick = () => poolShowReceipt(tr.dataset.rid);
    });
    const pv = box.querySelector('#pool-prev');
    if (pv) pv.onclick = () => poolLoadReceipts(root, page - 1);
    const nx = box.querySelector('#pool-next');
    if (nx) nx.onclick = () => poolLoadReceipts(root, page + 1);
  } catch (e) { /* 401 обработан в сводке */ }
}

async function poolShowReceipt(id) {
  try {
    const r = await poolApi('GET', '/api/v1/pool-my/receipt/' + id);
    const rows = (r.items || []).map(i => `<tr>
      <td>${esc(i.name)}</td><td class="num">${i.quantity}</td>
      <td class="num">${fmtSum(i.price)}</td><td class="num">${fmtSum(i.total)}</td></tr>`).join('');
    const { slot } = openModal(`
      <div class="modal-title">${esc(r.merchant_name || 'Чек')} · ${fmtSum(r.total_sum)}</div>
      <div class="form-hint" style="margin-bottom:8px">${poolStatusChip(r.status)}
        ${r.receipt_date ? ' · ' + esc(r.receipt_date.slice(0, 16).replace('T', ' ')) : ''}
        ${r.fn ? ' · ФН ' + esc(r.fn) : ''}
        ${r.merchant_address ? '<br>' + esc(r.merchant_address) : ''}</div>
      ${rows ? `<div class="table-wrap"><table style="width:100%">
        <thead><tr><th>Позиция</th><th>Кол-во</th><th>Цена</th><th>Сумма</th></tr></thead>
        <tbody>${rows}</tbody></table></div>`
        : '<p class="form-hint">Позиции появятся после проверки официальными источниками.</p>'}`);
    return slot;
  } catch (e) { toast(e.message, 'err', 'Чек-Пул'); }
}

function poolDownloadCsv() {
  fetch('/api/v1/pool-my/export.csv', { headers: { Authorization: 'Bearer ' + pGet() } })
    .then(r => { if (!r.ok) throw new Error('Не удалось выгрузить'); return r.blob(); })
    .then(blob => {
      const a = document.createElement('a');
      a.href = URL.createObjectURL(blob);
      a.download = 'chek-pool-moi-cheki.csv';
      a.click();
      setTimeout(() => URL.revokeObjectURL(a.href), 4000);
    })
    .catch(e => toast(e.message, 'err', 'Чек-Пул'));
}

function poolPrintList(root) {
  poolApi('GET', '/api/v1/pool-my/receipts?page=1&page_size=100').then(d => {
    const rows = d.items.map(r => `<tr><td>${esc((r.receipt_date || r.created_at || '').slice(0, 16).replace('T', ' '))}</td>
      <td>${esc(r.merchant_name || '—')}</td><td>${esc(r.fn)}</td>
      <td class="num">${fmtSum(r.total_sum)}</td><td>${esc(r.status)}</td>
      <td class="num">${r.points || 0}</td></tr>`).join('');
    const w = window.open('', '_blank', 'width=820,height=900');
    if (!w) { toast('Разрешите всплывающие окна — и снова нажмите «Печать»', 'warn', 'Чек-Пул'); return; }
    w.document.write('<!doctype html><html><head><meta charset="utf-8"><title>Чек-Пул — мои чеки</title>' +
      '<style>body{font:14px/1.45 -apple-system,"Segoe UI",Arial,sans-serif;color:#222}' +
      'h1{font-size:18px}table{border-collapse:collapse;width:100%}' +
      'th,td{border:1px solid #ddd;padding:6px 8px;text-align:left;font-size:13px}' +
      'th{background:#f5f5f5}.num{text-align:right;font-variant-numeric:tabular-nums}</style></head><body>' +
      '<h1>Ямастер Чек-Пул — мои чеки</h1>' +
      '<table><thead><tr><th>Дата</th><th>Магазин</th><th>ФН</th><th>Сумма</th><th>Статус</th><th>Баллы</th></tr></thead>' +
      `<tbody>${rows}</tbody></table><p style="font-size:12px;color:#777">ООО «Ямастер» · ymaster.ru · ${new Date().toLocaleString('ru-RU')}</p>` +
      '</body></html>');
    w.document.close();
    w.focus();
    w.print();
  }).catch(e => toast(e.message, 'err', 'Чек-Пул'));
}

function bindPoolAuth(root) {
  const refCode = poolRefGet();
  if (refCode) {
    const hint = root.querySelector('#p-ref-hint');
    if (hint) {
      hint.classList.remove('hidden');
      hint.innerHTML = `🎁 Применён код приглашения <b>${esc(refCode)}</b> —
        чеки этого браузера присоединятся к вашему новому кабинету.`;
    }
  }
  const tab = (id) => {
    ['login', 'reg', 'magic'].forEach(k => {
      const f = root.querySelector('#pf-' + k);
      if (f) f.classList.toggle('hidden', k !== id);
      const b = root.querySelector('#ptab-' + k);
      if (b) b.classList.toggle('btn-primary', k === id);
    });
    const msg = root.querySelector('#pool-auth-msg');
    if (msg) msg.classList.add('hidden');
  };
  ['login', 'reg', 'magic'].forEach(k => {
    const b = root.querySelector('#ptab-' + k);
    if (b) b.onclick = () => tab(k);
  });
  const lf = root.querySelector('#pf-login');
  if (lf) lf.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await poolApi('POST', '/api/v1/pool-auth/login',
        { email: root.querySelector('#pl-email').value.trim(), password: root.querySelector('#pl-pass').value });
      pSet(r.token);
      toast('Добро пожаловать!', 'ok', 'Чек-Пул');
      root.innerHTML = poolDashHTML();
      bindPoolDashboard(root);
    } catch (err) { poolShowErr(root, '#pool-auth-msg', err.message); }
  };
  const rf = root.querySelector('#pf-reg');
  if (rf) rf.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await poolApi('POST', '/api/v1/pool-auth/register', {
        email: root.querySelector('#pr-email').value.trim(),
        password: root.querySelector('#pr-pass').value,
        ref_code: poolRefGet() });
      pSet(r.token);
      toast(r.merged_receipts ? `Кабинет создан — перенесено чеков: ${r.merged_receipts}`
        : 'Кабинет создан', 'ok', 'Чек-Пул');
      root.innerHTML = poolDashHTML();
      bindPoolDashboard(root);
    } catch (err) { poolShowErr(root, '#pool-auth-msg', err.message); }
  };
  const mf = root.querySelector('#pf-magic');
  if (mf) mf.onsubmit = async (e) => {
    e.preventDefault();
    try {
      const r = await poolApi('POST', '/api/v1/pool-auth/magic',
        { email: root.querySelector('#pm-email').value.trim() });
      toast(r.message, 'ok', 'Чек-Пул');
    } catch (err) { poolShowErr(root, '#pool-auth-msg', err.message); }
  };
}

function poolLoadInvite(root) {
  poolApi('GET', '/api/v1/pool-my/referrals').then((d) => {
    const box = root.querySelector('#pool-invite');
    if (!box) return;
    box.innerHTML = `
    <div class="form-hint" style="margin-bottom:4px">Ваш код: <b>${esc(d.code)}</b> ·
      приглашено: <b>${d.invited}</b> из ${d.limit} ·
      принесло баллов: <b>${fmtInt(d.earned_total)}</b>
      ${d.next_bonus && d.next_bonus.code
        ? ` · ближайший бонус +${d.next_bonus.points} за ${d.next_bonus.need_receipts}-й
           чек приглашённого (сейчас ${d.next_bonus.have_receipts})` : ''}</div>
    <p class="form-hint" style="margin:4px 0">Бонусы начисляются не сразу, а за реальные
      шаги приглашённого: подтверждение почты (+5), 1-й чек от 100 ₽ (+20),
      5-й (+50), 20-й (+150) и 50-й (+500) — с задержками против накрутки;
      плюс 5% с чеков приглашённого — до 200 баллов в месяц.</p>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0">
      <input id="pool-inv-link" value="${esc(d.link)}" readonly style="max-width:330px">
      <button class="btn btn-sm" id="pool-inv-copy">📋 Копировать ссылку</button>
    </div>
    ${d.items.length ? `<div class="table-wrap"><table style="width:100%">
      <thead><tr><th>Дата</th><th>Кто</th><th>Чеков</th><th>Баллов принес</th></tr></thead>
      <tbody>${d.items.map((x) => `<tr>
        <td class="num">${esc((x.at || '').slice(0, 10))}</td>
        <td>${esc(x.label)}</td>
        <td class="num">${x.receipts_verified}</td>
        <td class="num">${x.earned ? '+' + x.earned : '—'}</td></tr>`).join('')}</tbody>
    </table></div>` : ''}`;
    const cb = root.querySelector('#pool-inv-copy');
    if (cb) cb.onclick = () => {
      try { navigator.clipboard.writeText(d.link); toast('Ссылка скопирована', 'ok', '🎁'); }
      catch (e) { toast('Скопируйте ссылку вручную', 'warn', '🎁'); }
    };
  }).catch(() => {
    const box = root.querySelector('#pool-invite');
    if (box) box.innerHTML = '<span class="form-hint">Приглашения доступны, когда включён приём чеков.</span>';
  });
}

function poolLeadersHTML(d, me) {
  const rows = (d.entries || []).map((e, i) => `<tr>
    <td class="num">${i + 1}</td>
    <td>${esc(e.label)}${me && me.rank === i + 1 ? ' — <b>вы</b>' : ''}</td>
    <td>${esc(e.city || '—')}</td>
    <td class="num">${e.receipts}</td></tr>`).join('');
  const regions = (d.regions || [])
    .map((r, i) => `${i + 1}. ${esc(r.name)} (${r.receipts})`).join(' · ');
  return `
  <div class="card-title" style="margin-top:12px;font-size:15px">🏆 Лидерборд месяца
    <span class="form-hint">${esc(d.month || '')} · участников: ${d.participants || 0}</span></div>
  <div class="table-wrap"><table style="width:100%">
    <thead><tr><th>#</th><th>Участник</th><th>Город</th><th>Чеков за месяц</th></tr></thead>
    <tbody>${rows || '<tr><td colspan="4">Пока пусто — сдайте чек и станьте первым</td></tr>'}</tbody></table></div>
  ${me ? `<div class="form-hint" style="margin-top:4px">Ваше место: <b>${me.rank}</b> (${me.receipts} чеков за месяц)` +
    (me.city ? ` · город «${esc(me.city)}»: ${me.city_rank}-е из ${me.city_participants}` : '') + `</div>` : ''}
  ${regions ? `<div class="form-hint" style="margin-top:4px">Регионы месяца: ${regions}</div>` : ''}`;
}

// ==========================================================================
//  v1.55.0: электронный чек после скана — «бумажный» вид, отправка HTML-чека
//  на почту и мягкое приглашение в кабинет. ООО «Ямастер»
// ==========================================================================
function renderEcheck(anchor, r) {
  if (!anchor || anchor.querySelector('.echeck')) return;
  const when = (r.receipt_date || r.created_at || '').slice(0, 16).replace('T', ' ');
  const rows = (r.items || []).map(i => `<tr>
      <td style="padding:5px 6px;border-bottom:1px dashed #e6ddcf">${esc(i.name)}</td>
      <td class="num" style="padding:5px 6px;border-bottom:1px dashed #e6ddcf;text-align:center">${i.quantity}</td>
      <td class="num" style="padding:5px 6px;border-bottom:1px dashed #e6ddcf;text-align:right">${fmtSum(i.price)}</td>
      <td class="num" style="padding:5px 6px;border-bottom:1px dashed #e6ddcf;text-align:right"><b>${fmtSum(i.total)}</b></td>
    </tr>`).join('');
  const wrap = document.createElement('div');
  wrap.className = 'echeck-wrap';
  wrap.innerHTML = `
    <div class="echeck">
      <div class="echeck-head">ЭЛЕКТРОННЫЙ ЧЕК</div>
      <div class="echeck-sub">совпадает с бумажным · проверен по официальным источникам</div>
      <div class="echeck-merchant">${esc(r.merchant_name || 'Магазин уточняется')}</div>
      ${r.merchant_address ? `<div class="echeck-addr">${esc(r.merchant_address)}</div>` : ''}
      ${rows ? `<table class="echeck-items"><thead><tr><th>Позиция</th><th>Кол-во</th><th>Цена</th><th>Сумма</th></tr></thead><tbody>${rows}</tbody></table>` : ''}
      <div class="echeck-total"><span>Итого</span><span>${fmtSum(r.total_sum)}</span></div>
      <div class="echeck-props">ФН ${esc(r.fn || '—')} · ФД ${esc(r.fd || '—')} · ФП ${esc(r.fp || '—')}<br>
        ${when ? when + '<br>' : ''}Статус: ${poolStatusChip(r.status)}
        ${r.points ? ' · <b>+' + r.points + ' балл' + (r.points > 1 ? 'ов' : '') + '</b>' : ''}</div>
    </div>
    <div class="echeck-mail">
      <b>Пришлём этот чек красивым письмом</b>
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px">
        <input type="email" id="ec-email" class="field-input" placeholder="ваш e-mail"
               autocomplete="email" style="flex:1;min-width:200px">
        <button class="btn btn-primary btn-sm" id="ec-send">✉ Прислать HTML-чек</button>
      </div>
      <div class="form-hint" id="ec-msg" style="margin-top:6px"></div>
    </div>
    <div class="echeck-invite">
      <b>Баллы хранятся в кабинете</b>
      <ul><li>+1 балл за каждый чек, кэшбэк у партнёров — до 100 за чек</li>
      <li>история и статусы всех ваших чеков в одном месте</li>
      <li>кабинет за минуту — по e-mail или временной ссылке входа; чеки этого браузера присоединятся сами</li></ul>
      <button class="btn btn-sm" id="ec-cabinet">Открыть кабинет →</button>
    </div>`;
  anchor.appendChild(wrap);
  const send = wrap.querySelector('#ec-send');
  if (send) send.onclick = async () => {
    const email = wrap.querySelector('#ec-email').value.trim();
    const msg = wrap.querySelector('#ec-msg');
    send.disabled = true;
    try {
      const resp = await poolApi('POST', '/api/v1/public/pool/receipt-email',
        { fn: r.fn || '', email, hp: '' });
      try { localStorage.setItem('ymaster-lead-email', email); } catch (e) {}
      if (msg) { msg.textContent = resp.message || 'Отправлено — проверьте почту';
                 msg.style.color = 'var(--ok)'; }
      toast(resp.message, 'ok', '✉ Чек-Пул');
    } catch (err) {
      if (msg) { msg.textContent = err.message || 'Не удалось отправить'; msg.style.color = ''; }
    } finally { send.disabled = false; }
  };
  const cab = wrap.querySelector('#ec-cabinet');
  if (cab) cab.onclick = () => {
    if (!getToken()) {
      history.replaceState(null, '', location.pathname + '#/my');
      showPoolScreen();
    } else { location.hash = '#/my'; }
  };
  wrap.scrollIntoView({ behavior: 'smooth', block: 'nearest' });
}

function poolLoadLeadersPub(root) {
  api.get('/api/v1/public/pool/leaderboard').then((d) => {
    const box = root.querySelector('#pub-leaders');
    if (!box || !(d.entries || []).length) return;
    box.innerHTML = poolLeadersHTML(d, null);
  }).catch(() => {});
}

function poolLoadEngage(root) {
  Promise.all([
    poolApi('GET', '/api/v1/pool-my/achievements'),
    poolApi('GET', '/api/v1/pool-my/leaders'),
    poolApi('GET', '/api/v1/pool-my/withdraw'),
  ]).then(([ach, lead, wd]) => {
    const box = root.querySelector('#pool-engage');
    if (!box) return;
    const chips = ach.items.map((a) => a.earned
      ? `<span class="chip exported"><span class="dot"></span>🏅 ${esc(a.name)}</span>`
      : `<span class="chip"><span class="dot"></span>${esc(a.name)} · ${a.progress}/${a.goal}</span>`).join(' ');
    const pct = Math.min(100, Math.round(100 * (wd.points || 0) / (wd.goal || 1)));
    box.innerHTML = `
      <div style="display:flex;gap:6px;flex-wrap:wrap;margin:4px 0 10px">${chips}</div>
      <div class="form-hint" style="margin:2px 0">🎯 Цель вывода: <b>${fmtInt(wd.points)}</b> из
        <b>${fmtInt(wd.goal)}</b> баллов · минимум заявки — ${wd.min}.</div>
      <div style="background:#e8e8e8;border-radius:6px;height:10px;max-width:360px;overflow:hidden;margin:6px 0">
        <div style="background:#e5770f;height:100%;width:${pct}%"></div></div>
      ${wd.active ? `<div class="form-hint">Активная заявка на ${fmtInt(wd.active.points)} баллов —
        статус «${wd.active.status === 'pending_sms' ? 'ожидает SMS-подтверждение' : 'принята'}».
        ${wd.active.sms_required ? 'Первый вывод подтверждается по SMS: код придёт, когда подключим шлюз.' : ''}</div>` : ''}
      ${wd.can_withdraw ? `<div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin:6px 0">
        <input id="pool-wd-pts" type="number" min="${wd.min}" max="${wd.points}" value="${wd.min}" style="max-width:110px" aria-label="Сколько баллов вывести">
        <input id="pool-wd-phone" placeholder="+7 900 000-00-00" style="max-width:190px" autocomplete="tel" aria-label="Телефон для выплаты">
        <button class="btn btn-sm" id="pool-wd-send">Вывести баллы</button></div>
        <div class="form-hint">Телефон нужен только для выплаты и хранится только хэшем.</div>` : ''}
      ${poolLeadersHTML(lead, lead.me)}
      ${wd.history.length ? `<div class="form-hint" style="margin-top:6px">Заявки: ${wd.history.map((h) =>
        `${esc((h.at || '').slice(0, 10))} — ${fmtInt(h.points)} б. (${h.status === 'pending_sms' ? 'ждёт SMS' : h.status === 'pending' ? 'принята' : esc(h.status)})`).join(' · ')}</div>` : ''}`;
    const send = root.querySelector('#pool-wd-send');
    if (send) send.onclick = async () => {
      try {
        const r = await poolApi('POST', '/api/v1/pool-my/withdraw', {
          points: parseInt(root.querySelector('#pool-wd-pts').value, 10) || 0,
          phone: root.querySelector('#pool-wd-phone').value.trim() });
        toast(r.message || 'Заявка принята', 'ok', '🏆');
        poolLoadEngage(root);
      } catch (err) { toast(err.message, 'warn', '🏆'); }
    };
  }).catch(() => {
    const box = root.querySelector('#pool-engage');
    if (box) box.innerHTML = '<span class="form-hint">Ачивки и вывод доступны, когда включён приём чеков.</span>';
  });
}

// --------------------------------------------------------------------------
//  v1.37.0: ПОДБОР ИЗ ПУЛА ДЛЯ КОМПАНИИ (#/poolpick, бухгалтер/админ).
//  Фильтры, привязка чеков (квота/мес), авто-подбор ±5%, CSV для АО-1,
//  «свои» сотрудники (сценарий C). Стиль ядра: таблицы, цифры, без капса.
// --------------------------------------------------------------------------
const poolPk = {
  company: '', tab: 'search', page: 1, minePage: 1, dicts: null,
  picked: new Set(), minePicked: new Set(), auto: null,
  f: { from: '', to: '', region: '', city: '', industry: '', inn: '', sumMin: '', sumMax: '', q: '' },
};

function poolPkShell() {
  return `
  <div class="glass card">
    <div class="card-title">🧩 Подбор из пула <span class="form-hint">(Этап 8 · v1.37.0)</span></div>
    <p class="form-hint" style="margin-bottom:10px">Верифицированные чеки открытой базы — под авансовые отчёты.
      Привязанный чек чужим компаниям не виден; участник сохраняет свои баллы.
      <a href="#/public">Сдать чек в пул →</a></p>
    <div style="display:flex;gap:10px;align-items:flex-end;flex-wrap:wrap;margin-bottom:8px">
      <label class="field" id="poolpk-comp-wrap" style="max-width:340px"><span>Компания</span><select id="poolpk-comp"></select></label>
      <div id="poolpk-quota" class="form-hint" style="flex:1;min-width:260px">Загружаем…</div>
    </div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
      <button class="btn btn-sm ${poolPk.tab === 'search' ? 'btn-primary' : ''}" id="poolpk-tab-search">1 · Найти чеки</button>
      <button class="btn btn-sm ${poolPk.tab === 'mine' ? 'btn-primary' : ''}" id="poolpk-tab-mine">2 · Мои чеки компании</button>
      <button class="btn btn-sm ${poolPk.tab === 'auto' ? 'btn-primary' : ''}" id="poolpk-tab-auto">3 · Авто-подбор ±5%</button>
      <button class="btn btn-sm ${poolPk.tab === 'staff' ? 'btn-primary' : ''}" id="poolpk-tab-staff">Сотрудники</button>
    </div>
    <div id="poolpk-body"></div>
  </div>`;
}

function poolPkFline(showGo = true) {
  const f = poolPk.f;
  return `
  <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:8px">
    <label class="field" style="max-width:150px"><span>Чеки с</span><input id="pk-from" type="date" value="${esc(f.from)}"></label>
    <label class="field" style="max-width:150px"><span>по</span><input id="pk-to" type="date" value="${esc(f.to)}"></label>
    <label class="field" style="max-width:220px"><span>Регион</span>
      <select id="pk-region"><option value="">Все регионы</option>${(poolPk.dicts ? poolPk.dicts.regions : [])
        .map((r) => `<option value="${r.code}" ${f.region === r.code ? 'selected' : ''}>${esc(r.name)}</option>`).join('')}</select></label>
    <label class="field" style="max-width:200px"><span>Отрасль</span>
      <select id="pk-industry"><option value="">Все отрасли</option>${(poolPk.dicts ? poolPk.dicts.industries : [])
        .map((i) => `<option value="${i.code}" ${f.industry === i.code ? 'selected' : ''}>${esc(i.name)}</option>`).join('')}</select></label>
    <label class="field" style="max-width:170px"><span>Город</span><input id="pk-city" value="${esc(f.city)}" placeholder="часть названия"></label>
    <label class="field" style="max-width:140px"><span>ИНН</span><input id="pk-inn" value="${esc(f.inn)}"></label>
    <label class="field" style="max-width:120px"><span>Сумма от</span><input id="pk-smin" type="number" value="${esc(f.sumMin)}"></label>
    <label class="field" style="max-width:120px"><span>до</span><input id="pk-smax" type="number" value="${esc(f.sumMax)}"></label>
    <label class="field" style="max-width:220px"><span>Магазин или товар</span><input id="pk-q" value="${esc(f.q)}"></label>
    ${showGo ? '<button class="btn btn-sm btn-primary" id="pk-go">🔍 Найти</button>' : ''}
  </div>`;
}

function poolPkCompId() { return poolPk.company ? `?company_id=${encodeURIComponent(poolPk.company)}` : ''; }

async function poolPkLoadQuota() {
  const box = $('#poolpk-quota');
  if (!box) return;
  try {
    const d = await api.get('/api/v1/pool-company/quota' + poolPkCompId());
    box.innerHTML = `Подобрано в этом месяце: <b>${fmtInt(d.used)}</b> из ${d.limit === 0 ? '— (подбор выключен)' : fmtInt(d.limit)} ·
      привязано всего: <b>${fmtInt(d.mine_total)}</b> · компания «<b>${esc(d.company.name)}</b>»<br>
      <span class="form-hint">${esc(d.tariff)}</span>`;
  } catch (e) { box.textContent = e.message; }
}

async function poolPkLoadSearch() {
  const box = $('#poolpk-body');
  const params = new URLSearchParams();
  const f = poolPk.f;
  [['from', 'date_from'], ['to', 'date_to'], ['region', 'region'], ['city', 'city'],
   ['industry', 'industry'], ['inn', 'inn'], ['sumMin', 'sum_min'], ['sumMax', 'sum_max'], ['q', 'q']]
    .forEach(([k, p]) => { if (f[k]) params.set(p, f[k]); });
  params.set('page', poolPk.page); params.set('per_page', 20);
  try {
    const d = await api.get('/api/v1/pool-company/search?' + params.toString() + (poolPkCompId() ? '&' + poolPkCompId().slice(1) : ''));
    const rows = d.items.map((r) => `
      <tr>
        <td><input type="checkbox" data-id="${r.id}" ${poolPk.picked.has(r.id) ? 'checked' : ''} style="width:auto" aria-label="Выбрать чек"></td>
        <td class="num">${esc(r.date)}</td><td>${esc(r.merchant)}</td><td class="num">${esc(r.inn)}</td>
        <td class="num">${fmtSum(r.sum)}</td><td>${esc(r.city || r.region)}</td><td>${esc(r.industry)}</td>
        <td class="form-hint">${esc(r.items)}</td></tr>`).join('');
    const pages = Math.max(1, Math.ceil(d.total / d.per_page));
    box.innerHTML = `
      ${poolPkFline()}
      <div class="form-hint" style="margin-bottom:6px">Найдено: <b>${fmtInt(d.total)}</b> · выбрано: <b id="pk-cnt">${poolPk.picked.size}</b></div>
      <div class="table-wrap"><table style="width:100%">
        <thead><tr><th></th><th>Дата чека</th><th>Магазин</th><th>ИНН</th><th>Сумма, ₽</th><th>Город</th><th>Отрасль</th><th>Позиции</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="8">Ничего не найдено — ослабьте фильтры</td></tr>'}</tbody></table></div>
      <div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-top:8px">
        <button class="btn btn-sm" id="pk-prev" ${poolPk.page <= 1 ? 'disabled' : ''}>← Назад</button>
        <span class="form-hint">Страница ${poolPk.page} из ${pages}</span>
        <button class="btn btn-sm" id="pk-next" ${poolPk.page >= pages ? 'disabled' : ''}>Вперёд →</button>
        <button class="btn btn-sm btn-primary" id="pk-assign">🔗 Привязать выбранное (${poolPk.picked.size})</button>
      </div>`;
    box.querySelectorAll('input[type="checkbox"][data-id]').forEach((cb) => {
      cb.onchange = () => {
        if (cb.checked) poolPk.picked.add(cb.dataset.id); else poolPk.picked.delete(cb.dataset.id);
        const cnt = $('#pk-cnt'); if (cnt) cnt.textContent = poolPk.picked.size;
        const btn = $('#pk-assign'); if (btn) btn.textContent = `🔗 Привязать выбранное (${poolPk.picked.size})`;
      };
    });
    const prev = $('#pk-prev'); if (prev) prev.onclick = () => { poolPk.page--; poolPkLoadSearch(); };
    const next = $('#pk-next'); if (next) next.onclick = () => { poolPk.page++; poolPkLoadSearch(); };
    $('#pk-go').onclick = () => {
      const g = (id) => ($(id) ? $(id).value.trim() : '');
      poolPk.f = { from: g('#pk-from'), to: g('#pk-to'), region: g('#pk-region'), city: g('#pk-city'),
                   industry: g('#pk-industry'), inn: g('#pk-inn'), sumMin: g('#pk-smin'), sumMax: g('#pk-smax'), q: g('#pk-q') };
      poolPk.page = 1; poolPkLoadSearch();
    };
    $('#pk-assign').onclick = async () => {
      if (!poolPk.picked.size) { toast('Отметьте чеки в таблице', 'warn', '🧩'); return; }
      try {
        const r = await api.post('/api/v1/pool-company/assign' + poolPkCompId(),
                                 { receipt_ids: [...poolPk.picked] });
        toast(r.message, 'ok', '🧩'); poolPk.picked.clear(); void poolPkLoadQuota(); poolPkLoadSearch();
      } catch (e) { toast(e.message, 'warn', '🧩'); }
    };
  } catch (e) { box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; }
}

async function poolPkLoadMine() {
  const box = $('#poolpk-body');
  const params = new URLSearchParams();
  if (poolPk.f.from) params.set('date_from', poolPk.f.from);
  if (poolPk.f.to) params.set('date_to', poolPk.f.to);
  params.set('page', poolPk.minePage); params.set('per_page', 20);
  try {
    const d = await api.get('/api/v1/pool-company/mine?' + params.toString() + (poolPkCompId() ? '&' + poolPkCompId().slice(1) : ''));
    const rows = d.items.map((r) => `
      <tr>
        <td><input type="checkbox" data-id="${r.id}" ${poolPk.minePicked.has(r.id) ? 'checked' : ''} style="width:auto" aria-label="Вернуть в пул"></td>
        <td class="num">${esc(r.date)}</td><td>${esc(r.merchant)}</td><td class="num">${esc(r.inn)}</td>
        <td class="num">${fmtSum(r.sum)}</td><td>${esc(r.city)}</td><td>${esc(r.industry)}</td>
        <td class="form-hint">${esc(r.items)}</td><td class="num form-hint">${esc(r.assigned_at)}</td></tr>`).join('');
    const pages = Math.max(1, Math.ceil(d.total / d.per_page));
    box.innerHTML = `
      <div class="form-hint" style="margin-bottom:6px">Привязано к компании: <b>${fmtInt(d.total)}</b> чеков.
        Выгрузка — CSV для АО-1 и Excel.</div>
      <div class="table-wrap"><table style="width:100%">
        <thead><tr><th></th><th>Дата чека</th><th>Магазин</th><th>ИНН</th><th>Сумма, ₽</th><th>Город</th><th>Отрасль</th><th>Позиции</th><th>Привязан</th></tr></thead>
        <tbody>${rows || '<tr><td colspan="9">Пока пусто — подберите чеки во вкладке «Найти чеки»</td></tr>'}</tbody></table></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-top:8px">
        <button class="btn btn-sm" id="pk-m-prev" ${poolPk.minePage <= 1 ? 'disabled' : ''}>← Назад</button>
        <span class="form-hint">Страница ${poolPk.minePage} из ${pages}</span>
        <button class="btn btn-sm" id="pk-m-next" ${poolPk.minePage >= pages ? 'disabled' : ''}>Вперёд →</button>
        <button class="btn btn-sm" id="pk-csv">⬇️ CSV (АО-1, Excel)</button>
        <button class="btn btn-sm" id="pk-unassign">↩️ Вернуть в пул (${poolPk.minePicked.size})</button>
      </div>`;
    box.querySelectorAll('input[type="checkbox"][data-id]').forEach((cb) => {
      cb.onchange = () => {
        if (cb.checked) poolPk.minePicked.add(cb.dataset.id); else poolPk.minePicked.delete(cb.dataset.id);
      };
    });
    const p = $('#pk-m-prev'); if (p) p.onclick = () => { poolPk.minePage--; poolPkLoadMine(); };
    const n = $('#pk-m-next'); if (n) n.onclick = () => { poolPk.minePage++; poolPkLoadMine(); };
    $('#pk-csv').onclick = () => {
      const qs = new URLSearchParams();
      if (poolPk.f.from) qs.set('date_from', poolPk.f.from);
      if (poolPk.f.to) qs.set('date_to', poolPk.f.to);
      if (poolPk.company) qs.set('company_id', poolPk.company);
      window.location = '/api/v1/pool-company/export.csv?' + qs.toString();
    };
    $('#pk-unassign').onclick = async () => {
      if (!poolPk.minePicked.size) { toast('Отметьте чеки', 'warn', '🧩'); return; }
      try {
        const r = await api.post('/api/v1/pool-company/unassign' + poolPkCompId(),
                                 { receipt_ids: [...poolPk.minePicked] });
        toast(r.message, 'ok', '🧩'); poolPk.minePicked.clear(); void poolPkLoadQuota(); poolPkLoadMine();
      } catch (e) { toast(e.message, 'warn', '🧩'); }
    };
  } catch (e) { box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; }
}

async function poolPkLoadAuto() {
  const box = $('#poolpk-body');
  box.innerHTML = `
    ${poolPkFline(false)}
    <div style="display:flex;gap:8px;align-items:flex-end;flex-wrap:wrap;margin-bottom:8px">
      <label class="field" style="max-width:200px"><span>Сумма отчёта, ₽</span>
        <input id="pk-target" type="number" min="100" placeholder="например, 5000"></label>
      <button class="btn btn-sm btn-primary" id="pk-auto-go">🎯 Подобрать набор ±5%</button>
    </div>
    <div id="pk-auto-out"></div>`;
  const go = $('#pk-auto-go');
  if (go) go.onclick = async () => {
    const out = $('#pk-auto-out');
    try {
      const d = await api.post('/api/v1/pool-company/autosuggest' + poolPkCompId(), {
        target_sum: parseFloat($('#pk-target').value) || 0,
        date_from: ($('#pk-from') || {}).value || '', date_to: ($('#pk-to') || {}).value || '',
        region: ($('#pk-region') || {}).value || '', city: ($('#pk-city') || {}).value || '',
        industry: ($('#pk-industry') || {}).value || '', inn: ($('#pk-inn') || {}).value || '',
      });
      poolPk.auto = d;
      out.innerHTML = `
        <div class="${d.ok ? 'info-callout' : 'form-error'}" style="margin-bottom:8px">${esc(d.message)}${d.ok
          ? `: <b>${d.count}</b> чеков на <b>${fmtSum(d.sum)}</b> ₽ (отклонение ${d.diff_pct > 0 ? '+' : ''}${d.diff_pct}%)` : ''}</div>
        ${d.ok ? `<div class="table-wrap"><table style="width:100%">
          <thead><tr><th>Дата чека</th><th>Магазин</th><th>Сумма, ₽</th><th>Город</th></tr></thead>
          <tbody>${d.items.map((r) => `<tr><td class="num">${esc(r.date)}</td><td>${esc(r.merchant)}</td>
            <td class="num">${fmtSum(r.sum)}</td><td>${esc(r.city)}</td></tr>`).join('')}</tbody></table></div>
          <button class="btn btn-primary btn-sm" id="pk-auto-assign" style="margin-top:8px">🔗 Привязать этот набор (${d.count})</button>` : ''}`;
      const ab = $('#pk-auto-assign');
      if (ab) ab.onclick = async () => {
        try {
          const r = await api.post('/api/v1/pool-company/assign' + poolPkCompId(), { receipt_ids: d.ids });
          toast(r.message, 'ok', '🧩'); void poolPkLoadQuota();
        } catch (e) { toast(e.message, 'warn', '🧩'); }
      };
    } catch (e) { out.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; }
  };
}

async function poolPkLoadStaff() {
  const box = $('#poolpk-body');
  try {
    const d = await api.get('/api/v1/pool-company/employees' + poolPkCompId());
    box.innerHTML = `
      <p class="form-hint" style="margin-bottom:8px">Сценарий C: если подтверждённый e-mail аккаунта пула совпадает
        с логином сотрудника, его чеки уходят компании, минуя общий пул. Сотрудник входит по приглашению
        (логин = корпоративный e-mail), сдает чек на странице «Сдать чек» и подтверждает e-mail в кабинете пула.</p>
      <div class="table-wrap"><table style="width:100%">
        <thead><tr><th>Сотрудник</th><th>Логин (e-mail)</th><th>Роль</th><th>Чек-Пул</th></tr></thead>
        <tbody>${d.items.map((u) => `<tr><td>${esc(u.full_name || u.username)}</td><td>${esc(u.username)}</td>
          <td>${u.role === 'accountant' ? 'бухгалтер' : 'пользователь'}</td>
          <td>${u.pool_linked ? '<span class="chip exported"><span class="dot"></span>подтверждён</span>'
                               : '<span class="chip"><span class="dot"></span>нет в пуле</span>'}</td></tr>`).join('')
          || '<tr><td colspan="4">Сотрудников нет — пригласите в разделе «Пользователи»</td></tr>'}</tbody></table></div>`;
  } catch (e) { box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; }
}

function poolPkBindTabs() {
  const tabs = { search: ['#poolpk-tab-search', poolPkLoadSearch],
                 mine: ['#poolpk-tab-mine', poolPkLoadMine],
                 auto: ['#poolpk-tab-auto', poolPkLoadAuto],
                 staff: ['#poolpk-tab-staff', poolPkLoadStaff] };
  Object.entries(tabs).forEach(([key, [sel, fn]]) => {
    const b = $(sel);
    if (b) b.onclick = () => {
      poolPk.tab = key; poolPk.page = 1; poolPk.minePage = 1;
      Object.values(tabs).forEach(([s]) => { const x = $(s); if (x) x.classList.toggle('btn-primary', s === sel); });
      fn();
    };
  });
}

async function viewPoolPick(container) {
  container.innerHTML = poolPkShell();
  try { if (!poolPk.dicts) poolPk.dicts = await api.get('/api/v1/pool-company/dicts'); } catch (e) {}
  // компания: админ выбирает; бухгалтер — своя (селектор скрыт, если одна)
  try {
    const q0 = await api.get('/api/v1/pool-company/quota');
    const wrap = $('#poolpk-comp-wrap');
    const comps = Array.isArray(state.companies) ? state.companies.filter((c) => c.is_active) : [];
    if (comps.length > 1) {
      wrap.classList.remove('hidden');
      const sel = $('#poolpk-comp');
      sel.innerHTML = comps.map((c) => `<option value="${c.id}">${esc(compName(c))}</option>`).join('');
      if (poolPk.company) sel.value = poolPk.company;
      poolPk.company = sel.value;
      sel.onchange = () => { poolPk.company = sel.value; poolPk.picked.clear(); poolPk.minePicked.clear(); void poolPkLoadQuota(); };
    } else {
      wrap.classList.add('hidden');
      poolPk.company = comps.length === 1 ? comps[0].id : poolPk.company;
    }
  } catch (e) {}
  poolPkBindTabs();
  void poolPkLoadQuota();
  ({ search: poolPkLoadSearch, mine: poolPkLoadMine, auto: poolPkLoadAuto, staff: poolPkLoadStaff })[poolPk.tab]();
}

async function poolApiKeysLoad() {
  const box = $('#pool-api-box');
  if (!box) return;
  try {
    const d = await api.get('/api/v1/pool-admin/api-keys');
    box.innerHTML = `
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin-bottom:8px">
        <label class="field" style="max-width:240px"><span>Партнёр</span><input id="pool-api-name" placeholder="ООО «Аналитика»"></label>
        <label class="field" style="max-width:110px"><span>Запросов/час</span><input id="pool-api-rate" type="number" min="1" value="60"></label>
        <label class="field" style="max-width:110px"><span>Квота/мес</span><input id="pool-api-quota" type="number" min="1" value="5000"></label>
        <button class="btn btn-sm btn-primary" id="pool-api-create">🔑 Выдать ключ</button>
      </div>
      <div id="pool-api-newkey"></div>
      ${d.items.length ? `<div class="table-wrap"><table style="width:100%">
        <thead><tr><th>Партнёр</th><th>Ключ</th><th>Лимит</th><th>За месяц</th><th>Использован</th><th></th></tr></thead>
        <tbody>${d.items.map((k) => `<tr>
          <td>${esc(k.name)}</td>
          <td class="form-hint">${esc(k.prefix)}</td>
          <td class="num">${k.rate_per_hour}/ч · ${fmtInt(k.monthly_quota)}/мес</td>
          <td class="num">${fmtInt(k.used_month)}</td>
          <td class="num form-hint">${esc(k.last_used_at)}</td>
          <td>${k.active
            ? `<button class="btn btn-sm" data-revoke="${k.id}">Отозвать</button>`
            : '<span class="chip failed"><span class="dot"></span>отозван</span>'}</td></tr>`).join('')}
        </tbody></table></div>`
        : '<div class="form-hint">Ключей пока нет — выдайте первый партнёру.</div>'}`;
    $('#pool-api-create').onclick = async () => {
      try {
        const r = await api.post('/api/v1/pool-admin/api-keys', {
          name: $('#pool-api-name').value.trim(),
          rate_per_hour: parseInt($('#pool-api-rate').value, 10) || 60,
          monthly_quota: parseInt($('#pool-api-quota').value, 10) || 5000 });
        $('#pool-api-newkey').innerHTML = `
          <div class="info-callout" style="margin-bottom:8px">🔑 Ключ партнёру (покажите сейчас — полностью он больше не виден):<br>
            <input value="${esc(r.key)}" readonly style="max-width:420px;margin-top:4px">
            <button class="btn btn-sm" id="pool-api-copy">📋 Копировать</button></div>`;
        $('#pool-api-copy').onclick = () => {
          try { navigator.clipboard.writeText(r.key); toast('Ключ скопирован', 'ok', '🔑'); }
          catch (e) { toast('Скопируйте вручную', 'warn', '🔑'); }
        };
        toast(r.message, 'ok', '🔑');
        void poolApiKeysLoad();
      } catch (e) { toast(e.message, 'warn', '🔑'); }
    };
    box.querySelectorAll('[data-revoke]').forEach((b) => {
      b.onclick = async () => {
        try {
          const r = await api.post('/api/v1/pool-admin/api-keys/' + b.dataset.revoke + '/revoke', {});
          toast(r.message, 'warn', '🔑');
          void poolApiKeysLoad();
        } catch (e) { toast(e.message, 'err', '🔑'); }
      };
    });
  } catch (e) { box.innerHTML = `<span class="form-hint">${esc(e.message)}</span>`; }
}

function bindPoolDashboard(root) {

  // v1.51.0: сдать чек прямо из кабинета — та же форма и тот же сканер
  // (камера / фото / перетаскивание / строка), что на странице «Сдать чек»
  const pso = root.querySelector('#pool-submit-open');
  if (pso) pso.onclick = () => {
    const { slot, close } = openModal(`
      <div class="modal-title">🧾 Сдать чек в Чек-Пул</div>
      <div id="pool-submit-form"></div>`);
    const box = slot.querySelector('#pool-submit-form');
    box.innerHTML = publicFormHTML();
    const leaders = box.querySelector('#pub-leaders');
    if (leaders) leaders.remove();          // в кабинете лидерборд не нужен
    const mine = box.querySelector('#pub-mine');
    if (mine) mine.remove();                // «мои чеки» — прямо в кабинете
    bindPoolForm(box, { onDone: () => {
      poolLoadSummary(root); poolLoadReceipts(root, 1);
      toast('Чек принят — статус виден в кабинете', 'ok', '🧾 Чек-Пул');
      setTimeout(close, 2500);
    } });
  };

  poolLoadSummary(root);
  poolLoadReceipts(root, 1);
  poolLoadInvite(root);
  poolLoadEngage(root);              // v1.36.0: ачивки, лидерборд, вывод
  const csv = root.querySelector('#pool-csv');
  if (csv) csv.onclick = poolDownloadCsv;
  const pr = root.querySelector('#pool-print');
  if (pr) pr.onclick = () => poolPrintList(root);
  const ps = root.querySelector('#pool-pass-save');
  if (ps) ps.onclick = async () => {
    try {
      const r = await poolApi('POST', '/api/v1/pool-my/password', {
        old_password: root.querySelector('#pool-oldp').value,
        new_password: root.querySelector('#pool-newp').value });
      toast(r.message, 'ok', 'Чек-Пул');
      root.querySelector('#pool-oldp').value = '';
      root.querySelector('#pool-newp').value = '';
    } catch (e) { toast(e.message, 'err', 'Чек-Пул'); }
  };
  const es = root.querySelector('#pool-email-save');
  if (es) es.onclick = async () => {
    try {
      const r = await poolApi('POST', '/api/v1/pool-my/email', {
        password: root.querySelector('#pool-oldp').value,
        email: root.querySelector('#pool-newemail').value.trim() });
      toast(r.message, 'ok', 'Чек-Пул');
      poolLoadSummary(root);
    } catch (e) { toast(e.message, 'err', 'Чек-Пул'); }
  };
  const lo = root.querySelector('#pool-logout');
  if (lo) lo.onclick = () => { pClear(); root.innerHTML = poolAuthHTML(); bindPoolAuth(root); };
  const del = root.querySelector('#pool-delete');
  if (del) del.onclick = () => {
    const { slot } = openModal(`
      <div class="modal-title">Удаление аккаунта Чек-Пула</div>
      <p class="form-hint">E-mail и пароль будут стёрты, баллы — сгорят.
      Сданные чеки останутся в открытой базе обезличенными (это согласовано офертой).</p>
      <label class="field"><span>Пароль для подтверждения</span><input id="pool-del-pass" type="password"></label>
      <div style="display:flex;gap:8px;margin-top:10px">
        <button class="btn btn-sm" data-close>Отмена</button>
        <button class="btn btn-sm" id="pool-del-go" style="color:#b3261e">Удалить навсегда</button>
      </div>`);
    slot.querySelector('#pool-del-go').onclick = async () => {
      try {
        const r = await poolApi('POST', '/api/v1/pool-my/delete',
          { password: slot.querySelector('#pool-del-pass').value });
        pClear();
        toast(r.message, 'ok', 'Чек-Пул');
        root.innerHTML = poolAuthHTML();
        bindPoolAuth(root);
      } catch (e) { toast(e.message, 'err', 'Чек-Пул'); }
    };
  };
}

function bindPoolAccount(root) {
  if (pGet()) { bindPoolDashboard(root); } else { bindPoolAuth(root); }
}

function showPoolScreen() {
  $('#register-screen').classList.add('hidden');
  $('#login-screen').classList.add('hidden');
  $('#app-shell').classList.add('hidden');
  const root = $('#pool-account-root');
  root.innerHTML = pGet() ? poolDashHTML() : poolAuthHTML();
  const back = document.getElementById('pool-to-login');
  if (back) back.onclick = (e) => { e.preventDefault(); history.replaceState(null, '', location.pathname); showLogin(); };
  const pub = document.getElementById('pool-to-public');
  if (pub) pub.onclick = (e) => { e.preventDefault(); history.replaceState(null, '', location.pathname + '#/public'); showPublicScreen(); };
  bindPoolAccount(root);
}

// Кабинет внутри программы (пункт меню «Мой Чек-Пул»)
async function viewPoolAccount(container) {
  container.innerHTML = `
  <div class="glass card" style="max-width:760px">
    <div class="card-title">👤 Мой Чек-Пул <span class="form-hint">(Этап 3 · v1.32.0)</span></div>
    <p class="form-hint" style="margin-bottom:10px">Кабинет участника открытой базы — отдельный от рабочей
    программы: пароли и доступы не смешиваются. Здесь видны только чеки, сданные в пул.</p>
    ${pGet() ? poolDashHTML() : poolAuthHTML()}
  </div>`;
  bindPoolAccount(container);
}


// --------------------------------------------------------------------------
//  v1.33.0: ПАНЕЛЬ МОДЕРАЦИИ «ЧЕК-ПУЛА» (#/pooladmin, только админ).
//  Гео/отрасли: покрытие, дообогащение, ручная разметка; модерация в 2 клика;
//  поиск по ФН/ИНН/магазину; выгрузка CSV. Стиль ядра: таблицы, без капса.
// --------------------------------------------------------------------------
const poolAdm = { q: '', status: '', missingGeo: false, missingInd: false, page: 1, dicts: null };

function poolAdmChip(ok, text) {
  return ok ? '<span class="chip exported"><span class="dot"></span>' + text + '</span>'
            : '<span class="chip failed"><span class="dot"></span>' + text + '</span>';
}

function poolAdmShell() {
  return `
  <div class="glass card">
    <div class="card-title">🧩 Чек-Пул: гео, отрасли и модерация <span class="form-hint">(Этап 4 · v1.33.0)</span></div>
    <div id="pooladm-stats" class="form-hint" style="margin-bottom:10px">Загружаем…</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <input id="pooladm-q" placeholder="Поиск: ФН, ИНН или магазин" style="max-width:280px" value="${esc(poolAdm.q)}">
      <select id="pooladm-status">
        <option value="">Все статусы</option>
        <option value="pending">На ручной проверке</option>
        <option value="verified">Принятые</option>
        <option value="rejected">Не принятые</option>
      </select>
      <label style="display:flex;gap:6px;align-items:center;cursor:pointer"><input type="checkbox" id="pooladm-mgeo" style="width:auto"> Без региона</label>
      <label style="display:flex;gap:6px;align-items:center;cursor:pointer"><input type="checkbox" id="pooladm-mind" style="width:auto"> Без отрасли</label>
      <button class="btn btn-sm" id="pooladm-enrich">🪄 Разметить недостающее</button>
      <button class="btn btn-sm" id="pooladm-csv">⬇️ CSV</button>
    </div>
    <div id="pooladm-table"></div>
  </div>`;
}

async function poolAdmLoadStats() {
  try {
    const d = await api.get('/api/v1/pool-admin/overview');
    const box = $('#pooladm-stats');
    if (!box) return;
    box.innerHTML = `
      <b>${fmtInt(d.receipts_total)}</b> чеков · принято <b>${fmtInt(d.verified)}</b> ·
      на проверке <b>${fmtInt(d.pending)}</b> · не принято <b>${fmtInt(d.rejected)}</b> ·
      участников <b>${fmtInt(d.users_total)}</b> · баллов <b>${fmtInt(d.points_total)}</b><br>
      Покрытие регионом ${poolAdmChip(d.geo_coverage >= 70, '≥70%: ' + d.geo_coverage + '%')}
      отраслью ${poolAdmChip(d.industry_coverage >= 60, '≥60%: ' + d.industry_coverage + '%')}`;
  } catch (e) { /* не критично для таблицы */ }
}

async function poolAdmLoadTable() {
  const box = $('#pooladm-table');
  if (!box) return;
  const params = new URLSearchParams();
  if (poolAdm.q) params.set('q', poolAdm.q);
  if (poolAdm.status) params.set('status', poolAdm.status);
  if (poolAdm.missingGeo) params.set('missing_geo', '1');
  if (poolAdm.missingInd) params.set('missing_industry', '1');
  params.set('page', poolAdm.page);
  params.set('page_size', 20);
  try {
    const d = await api.get('/api/v1/pool-admin/receipts?' + params.toString());
    const rn = (code) => { const x = (poolAdm.dicts || { regions: [] }).regions.find(r => r.code === code); return x ? x.name : (code || '—'); };
    const inm = (code) => { const x = (poolAdm.dicts || { industries: [] }).industries.find(i => i.code === code); return x ? x.name : (code || '—'); };
    box.innerHTML = `
    <div class="table-wrap"><table style="width:100%">
      <thead><tr><th>Сдан</th><th>Магазин</th><th>Сумма</th><th>ФН</th>
      <th>Регион</th><th>Отрасль</th><th>Статус</th><th>Действия</th></tr></thead>
      <tbody>${d.items.length ? d.items.map(r => `<tr>
        <td class="num">${esc((r.created_at || '').slice(0, 16).replace('T', ' '))}</td>
        <td>${esc(r.merchant_name || '—')}<div class="form-hint">ИНН ${esc(r.merchant_inn || '—')}</div></td>
        <td class="num">${fmtSum(r.total_sum)}</td>
        <td class="num">${esc(r.fn || '—')}</td>
        <td>${r.region_code ? esc(rn(r.region_code)) + (r.city ? ' · ' + esc(r.city) : '')
             : '<span class="form-hint">— разметить</span>'}</td>
        <td>${r.industry ? esc(inm(r.industry)) : '<span class="form-hint">—</span>'}</td>
        <td>${poolStatusChip(r.status)}</td>
        <td style="white-space:nowrap">
          ${r.status === 'pending' ? `<button class="btn btn-sm pooladm-ok" data-id="${r.id}" title="Одобрить">✅</button>
             <button class="btn btn-sm pooladm-no" data-id="${r.id}" title="Отклонить">❌</button>` : ''}
          <button class="btn btn-sm pooladm-edit" data-id="${r.id}" title="Разметить вручную">✏️</button>
        </td></tr>`).join('')
      : '<tr><td colspan="8" class="form-hint">Ничего не найдено по фильтрам.</td></tr>'}</tbody>
    </table></div>
    ${d.total > d.page_size ? `<div class="form-hint" style="margin-top:6px">Показано ${d.items.length} из ${fmtInt(d.total)} — уточните фильтры.</div>` : ''}`;
    box.querySelectorAll('.pooladm-ok').forEach(b => { b.onclick = () => poolAdmModerate(b.dataset.id, 'approve', ''); });
    box.querySelectorAll('.pooladm-no').forEach(b => { b.onclick = () => poolAdmRejectDialog(b.dataset.id); });
    box.querySelectorAll('.pooladm-edit').forEach(b => { b.onclick = () => poolAdmMarkDialog(b.dataset.id); });
  } catch (e) { box.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; }
}

async function poolAdmModerate(id, action, comment) {
  try {
    const r = await api.post(`/api/v1/pool-admin/receipt/${id}/moderate`,
                             { action, comment });
    toast(action === 'approve'
      ? `Чек принят в пул${r.points_awarded ? ' — начислен балл участнику' : ''}`
      : 'Чек отклонён', action === 'approve' ? 'ok' : 'warn', '🧩');
    poolAdmLoadStats();
    poolAdmLoadTable();
  } catch (e) { toast(e.message, 'err', '🧩'); }
}

function poolAdmRejectDialog(id) {
  const { slot } = openModal(`
    <div class="modal-title">Отклонить чек?</div>
    <label class="field"><span>Причина (необязательно)</span>
      <input id="pooladm-reject-comment" placeholder="например: данные не подтвердились"></label>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn btn-sm" data-close>Отмена</button>
      <button class="btn btn-sm" id="pooladm-reject-go" style="color:#b3261e">Отклонить</button>
    </div>`);
  slot.querySelector('#pooladm-reject-go').onclick = () => {
    poolAdmModerate(id, 'reject', slot.querySelector('#pooladm-reject-comment').value);
    slot._dc && slot._dc.click ? slot._dc.click() : null;
  };
}

function poolAdmMarkDialog(id) {
  if (!poolAdm.dicts) return;
  const opts = (list, cur) => list.map(x =>
    `<option value="${x.code}" ${x.code === cur ? 'selected' : ''}>${esc(x.name)}</option>`).join('');
  const { slot } = openModal(`
    <div class="modal-title">Ручная разметка чека</div>
    <label class="field"><span>Регион</span>
      <select id="pooladm-ed-region"><option value="">— не задан —</option>
        ${opts(poolAdm.dicts.regions, '')}</select></label>
    <label class="field"><span>Город (необязательно)</span><input id="pooladm-ed-city"></label>
    <label class="field"><span>Отрасль</span>
      <select id="pooladm-ed-industry"><option value="">— не задана —</option>
        ${opts(poolAdm.dicts.industries, '')}</select></label>
    <div style="display:flex;gap:8px;margin-top:10px">
      <button class="btn btn-sm" data-close>Отмена</button>
      <button class="btn btn-sm btn-primary" id="pooladm-ed-save">Сохранить</button>
    </div>`);
  api.get(`/api/v1/pool-admin/receipts?page=1&page_size=200`).then(d => {
    const r = d.items.find(x => x.id === id);
    if (!r) return;
    const rs = slot.querySelector('#pooladm-ed-region');
    if (r.region_code) rs.value = r.region_code;
    const city = slot.querySelector('#pooladm-ed-city');
    if (city) city.value = r.city || '';
    const ins = slot.querySelector('#pooladm-ed-industry');
    if (r.industry) ins.value = r.industry;
  }).catch(() => {});
  slot.querySelector('#pooladm-ed-save').onclick = async () => {
    try {
      await api.patch(`/api/v1/pool-admin/receipt/${id}`, {
        region_code: slot.querySelector('#pooladm-ed-region').value,
        city: slot.querySelector('#pooladm-ed-city').value,
        industry: slot.querySelector('#pooladm-ed-industry').value });
      toast('Разметка сохранена', 'ok', '🧩');
      slot._dc && slot._dc.click ? slot._dc.click() : null;
      poolAdmLoadStats();
      poolAdmLoadTable();
    } catch (e) { toast(e.message, 'err', '🧩'); }
  };
}

// v1.41.0: УЧАСТНИКИ ЧЕК-ПУЛА (только админ) — полный список + вход в
// кабинет глазами участника (кнопка «👁», только у активных).
async function viewPoolPeople(container) {
  container.innerHTML = `
    <div class="info-callout">Участник Чек-Пула — отдельная роль: человек
    сам регистрируется в кабинете и сдаёт чеки за баллы. Кнопка «👁» открывает
    его кабинет <b>глазами участника</b>; включение и выход — в журнале действий.
    Ядро-логин администратора при этом не прерывается.</div>
    <div class="glass card">
      <div class="card-title">Участники <span class="spacer"></span>
        <input id="pp-q" placeholder="Поиск по e-mail" style="max-width:240px" value=""></div>
      <div class="table-wrap"><table class="data"><thead><tr>
        <th>E-mail</th><th>Статус</th><th>Чеков</th><th>Баллов</th><th>Риск</th><th>В пуле с</th><th></th>
      </tr></thead><tbody id="pp-rows"><tr style="cursor:default"><td colspan="7"><span class="spinner"></span></td></tr></tbody></table></div>
    </div>`;
  const load = async () => {
    const qv = $('#pp-q').value.trim();
    const rows = $('#pp-rows');
    try {
      const d = await api.get('/api/v1/pool-admin/participants?limit=200'
        + (qv ? '&q=' + encodeURIComponent(qv) : ''));
      rows.innerHTML = d.items.length ? d.items.map(u => `<tr data-id="${esc(u.id)}" style="cursor:default">
          <td><b>${esc(u.email || '—')}</b>${u.nickname ? `<div class="form-hint">${esc(u.nickname)}</div>` : ''}${u.email_verified ? '' : ' <span class="chip unknown mono">e-mail не подтверждён</span>'}</td>
          <td>${u.is_blocked
            ? '<span class="chip failed"><span class="dot"></span>заблокирован</span>'
            : '<span class="chip verified"><span class="dot"></span>активен</span>'}</td>
          <td class="cell-num">${fmtInt(u.receipts)}</td>
          <td class="cell-num">${fmtInt(u.points)}</td>
          <td class="cell-num">${fmtInt(u.risk_score)}</td>
          <td class="cell-date">${fmtDate(u.created_at)}</td>
          <td>${u.is_blocked ? '—' : `<button class="btn btn-sm pp-view" data-id="${esc(u.id)}" data-name="${esc(u.email || u.nickname || u.id)}" title="Открыть кабинет глазами участника">👁</button>`}</td>
        </tr>`).join('')
        : `<tr style="cursor:default"><td colspan="7">${emptyState('🧩', 'Участников не найдено')}</td></tr>`;
      rows.querySelectorAll('.pp-view').forEach(b => {
        b.onclick = () => startViewAsPool(b.dataset.id, b.dataset.name);
      });
    } catch (e) {
      rows.innerHTML = `<tr style="cursor:default"><td colspan="7">${esc(e.message || 'Не удалось загрузить')}</td></tr>`;
    }
  };
  let ppdeb = 0;
  $('#pp-q').oninput = () => { clearTimeout(ppdeb); ppdeb = setTimeout(load, 300); };
  await load();
}

async function viewPoolAdmin(container) {
  container.innerHTML = poolAdmShell();
  if (!poolAdm.dicts) {
    try { poolAdm.dicts = await api.get('/api/v1/pool-admin/dicts'); } catch (e) {}
  }
  const q = $('#pooladm-q');
  let deb = 0;
  q.oninput = () => {
    clearTimeout(deb);
    deb = setTimeout(() => { poolAdm.q = q.value.trim(); poolAdm.page = 1; poolAdmLoadTable(); }, 350);
  };
  $('#pooladm-status').value = poolAdm.status;
  $('#pooladm-status').onchange = (e) => { poolAdm.status = e.target.value; poolAdm.page = 1; poolAdmLoadTable(); };
  $('#pooladm-mgeo').checked = poolAdm.missingGeo;
  $('#pooladm-mgeo').onchange = (e) => { poolAdm.missingGeo = e.target.checked; poolAdm.page = 1; poolAdmLoadTable(); };
  $('#pooladm-mind').checked = poolAdm.missingInd;
  $('#pooladm-mind').onchange = (e) => { poolAdm.missingInd = e.target.checked; poolAdm.page = 1; poolAdmLoadTable(); };
  $('#pooladm-enrich').onclick = async () => {
    const b = $('#pooladm-enrich');
    b.disabled = true;
    try {
      const r = await api.post('/api/v1/pool-admin/enrich-missing', {});
      toast(r.message, 'ok', '🪄');
      poolAdmLoadStats();
      poolAdmLoadTable();
    } catch (e) { toast(e.message, 'err', '🪄'); }
    b.disabled = false;
  };
  $('#pooladm-csv').onclick = () => {
    const p = new URLSearchParams();
    if (poolAdm.q) p.set('q', poolAdm.q);
    if (poolAdm.status) p.set('status', poolAdm.status);
    if (poolAdm.missingGeo) p.set('missing_geo', '1');
    if (poolAdm.missingInd) p.set('missing_industry', '1');
    fetch('/api/v1/pool-admin/export.csv?' + p.toString(),
          { headers: { Authorization: 'Bearer ' + getToken() } })
      .then(r => { if (!r.ok) throw new Error('Не удалось выгрузить'); return r.blob(); })
      .then(blob => {
        const a = document.createElement('a');
        a.href = URL.createObjectURL(blob);
        a.download = 'chek-pool-admin.csv';
        a.click();
        setTimeout(() => URL.revokeObjectURL(a.href), 4000);
      })
      .catch(e => toast(e.message, 'err', '🧩'));
  };
  poolAdmLoadStats();
  poolAdmLoadTable();
}


// --------------------------------------------------------------------------
//  v1.34.0: ПАНЕЛЬ АНТИФРОДА (#/fraud, только админ) — 5 слоёв.
//  Лента сигналов по severity, карточка участника (устройства, IP, связи),
//  разбор (ложное/подтверждено), карантин и аннулирование баллов.
// --------------------------------------------------------------------------
const fraud = { status: 'new', minSev: 0, page: 1 };

function fraudSevChip(sev) {
  const cls = sev >= 4 ? 'failed' : (sev === 3 ? 'new' : '');
  return `<span class="chip ${cls}"><span class="dot"></span>S${sev}</span>`;
}

function fraudShell() {
  return `
  <div class="glass card">
    <div class="card-title">🛡 Чек-Пул: антифрод <span class="form-hint">(Этап 5 · v1.34.0)</span></div>
    <div id="fraud-stats" class="form-hint" style="margin-bottom:10px">Загружаем…</div>
    <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center;margin-bottom:8px">
      <select id="fraud-status">
        <option value="new">Новые</option>
        <option value="reviewing">На разборе</option>
        <option value="false_positive">Ложные</option>
        <option value="confirmed">Подтверждённые</option>
        <option value="">Все</option>
      </select>
      <select id="fraud-sev">
        <option value="0">Любая важность</option>
        <option value="3">S3+ (серьёзные)</option>
        <option value="4">S4+ (критичные)</option>
      </select>
      <button class="btn btn-sm" id="fraud-bulk-fp" disabled>Ложное срабатывание (выбранные)</button>
      <button class="btn btn-sm" id="fraud-bulk-confirm" disabled>Подтвердить (выбранные)</button>
      <button class="btn btn-sm" id="fraud-purge" title="152-ФЗ: техданные храним ≤ 12 месяцев">🧹 Чистка техданных &gt; года</button>
      <button class="btn btn-sm" id="fraud-graph-btn">🕸 Граф связей</button>
    </div>
    <div id="fraud-graph" class="hidden" style="margin-bottom:10px"></div>
    <div id="fraud-table"></div>
  </div>`;
}

async function fraudLoadStats() {
  try {
    const d = await api.get('/api/v1/pool-fraud/summary');
    const box = $('#fraud-stats');
    if (!box) return;
    const codes = Object.entries(d.by_code).map(([k, v]) => `${k}: <b>${v}</b>`).join(' · ');
    box.innerHTML = `Новых сигналов: <b>${fmtInt(d.new_signals)}</b> ·
      в карантине участников: <b>${fmtInt(d.quarantined)}</b>
      (порог risk ≥ ${d.quarantine_threshold})${codes ? '<br>' + codes : ''}`;
  } catch (e) { /* не критично */ }
}

async function fraudLoadTable() {
  const box = $('#fraud-table');
  if (!box) return;
  const p = new URLSearchParams();
  if (fraud.status) p.set('status', fraud.status);
  if (fraud.minSev) p.set('min_severity', fraud.minSev);
  p.set('page', fraud.page);
  p.set('page_size', 30);
  try {
    const d = await api.get('/api/v1/pool-fraud/signals?' + p.toString());
    box.innerHTML = `
    <div class="table-wrap"><table style="width:100%">
      <thead><tr><th></th><th>Когда</th><th>Сигнал</th><th>Участник</th><th>Детали</th><th>Статус</th><th>Разбор</th></tr></thead>
      <tbody>${d.items.length ? d.items.map(x => `<tr>
        <td><input type="checkbox" class="fraud-sel" data-id="${x.id}" style="width:auto"></td>
        <td class="num">${esc((x.created_at || '').slice(0, 16).replace('T', ' '))}</td>
        <td>${fraudSevChip(x.severity)} <b>${esc(x.code)}</b>
          <div class="form-hint">+${x.points} к риску</div></td>
        <td><a href="#" class="fraud-user" data-uid="${x.user.id}">${esc(x.user.label)}</a>
          ${x.user.quarantined ? '<div class="form-hint">в карантине</div>' : ''}</td>
        <td class="form-hint" style="max-width:260px;overflow:hidden;text-overflow:ellipsis">${esc(x.details || '—')}</td>
        <td>${esc(x.status)}${x.resolved_by ? '<div class="form-hint">' + esc(x.resolved_by) + '</div>' : ''}</td>
        <td style="white-space:nowrap">
          <button class="btn btn-sm fraud-fp" data-id="${x.id}" title="Ложное срабатывание">🙂</button>
          <button class="btn btn-sm fraud-conf" data-id="${x.id}" title="Подтвердить">⚠️</button>
        </td></tr>`).join('')
      : '<tr><td colspan="7" class="form-hint">Сигналов нет — честные участники не помечаются.</td></tr>'}</tbody>
    </table></div>
    ${d.total > d.page_size ? `<div class="form-hint" style="margin-top:6px">Показано ${d.items.length} из ${fmtInt(d.total)} — уточните фильтры.</div>` : ''}`;
    const upd = () => {
      const n = box.querySelectorAll('.fraud-sel:checked').length;
      $('#fraud-bulk-fp').disabled = !n;
      $('#fraud-bulk-confirm').disabled = !n;
    };
    box.querySelectorAll('.fraud-sel').forEach(c => { c.onchange = upd; });
    box.querySelectorAll('.fraud-fp').forEach(b => { b.onclick = () => fraudResolve([b.dataset.id], 'false_positive'); });
    box.querySelectorAll('.fraud-conf').forEach(b => { b.onclick = () => fraudResolve([b.dataset.id], 'confirmed'); });
    box.querySelectorAll('.fraud-user').forEach(a => { a.onclick = (e) => { e.preventDefault(); fraudUserCard(a.dataset.uid); }; });
  } catch (e) { box.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; }
}

async function fraudResolve(ids, status) {
  try {
    if (ids.length === 1) {
      await api.post(`/api/v1/pool-fraud/signal/${ids[0]}/resolve`, { status });
    } else {
      await api.post('/api/v1/pool-fraud/signal/resolve-bulk', { ids, status });
    }
    toast(status === 'false_positive' ? 'Помечено как ложное срабатывание'
      : 'Подтверждено — риск пересчитан', 'ok', '🛡');
    fraudLoadStats();
    fraudLoadTable();
  } catch (e) { toast(e.message, 'err', '🛡'); }
}

async function fraudUserCard(uid) {
  try {
    const d = await api.get(`/api/v1/pool-fraud/user/${uid}`);
    const u = d.user;
    const { slot } = openModal(`
      <div class="modal-title">🛡 Участник: ${esc(u.email || ('гость ' + (u.vid || '').slice(0, 8)))}</div>
      <div class="form-hint" style="margin-bottom:8px">
        Риск <b>${u.risk_score}</b>/100 ${u.quarantined ? '· <b>в карантине</b>' : ''} ·
        баллов <b>${fmtInt(u.points)}</b> · доверие <b>${u.trust_level}</b> ·
        связей: <b>${d.related_users.length}</b></div>
      ${d.related_users.length ? `<p class="form-hint">Связан по устройствам/подсетям с:
        ${d.related_users.map(x => esc(x.label)).join(', ')}</p>` : ''}
      ${d.devices.length ? `<div class="form-hint">Устройства: ${d.devices.map(x => esc(x.visitor) + '×' + x.seen).join(', ')}</div>` : ''}
      ${d.ips.length ? `<div class="form-hint">IP: ${d.ips.map(x => esc(x.ip)).join(', ')}</div>` : ''}
      ${d.signals.length ? `<div class="table-wrap"><table style="width:100%;margin-top:8px">
        <thead><tr><th>Сигнал</th><th>Риск</th><th>Статус</th></tr></thead>
        <tbody>${d.signals.map(x => `<tr><td>${esc(x.code)}</td><td class="num">+${x.points}</td><td>${esc(x.status)}</td></tr>`).join('')}</tbody>
      </table></div>` : ''}
      <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:12px">
        ${u.quarantined ? '<button class="btn btn-sm btn-primary" id="fu-release">Снять карантин</button>' : ''}
        ${u.points > 0 ? '<button class="btn btn-sm" id="fu-annul" style="color:#b3261e">Аннулировать баллы</button>' : ''}
        <button class="btn btn-sm" id="fu-trust1">Доверие: 1</button>
        <button class="btn btn-sm" id="fu-trust0">Доверие: 0</button>
      </div>`);
    const rel = slot.querySelector('#fu-release');
    if (rel) rel.onclick = async () => {
      try { toast((await api.post(`/api/v1/pool-fraud/user/${uid}/action`, { action: 'release' })).message, 'ok', '🛡'); fraudLoadStats(); }
      catch (e) { toast(e.message, 'err', '🛡'); }
    };
    const an = slot.querySelector('#fu-annul');
    if (an) an.onclick = async () => {
      try { toast((await api.post(`/api/v1/pool-fraud/user/${uid}/action`, { action: 'annul_points' })).message, 'warn', '🛡'); }
      catch (e) { toast(e.message, 'err', '🛡'); }
    };
    const setTrust = (v) => async () => {
      try { toast((await api.post(`/api/v1/pool-fraud/user/${uid}/action`, { action: 'set_trust', value: v })).message, 'ok', '🛡'); }
      catch (e) { toast(e.message, 'err', '🛡'); }
    };
    const t1 = slot.querySelector('#fu-trust1');
    if (t1) t1.onclick = setTrust(1);
    const t0 = slot.querySelector('#fu-trust0');
    if (t0) t0.onclick = setTrust(0);
  } catch (e) { toast(e.message, 'err', '🛡'); }
}

// --------------------------------------------------------------------------
//  v1.38.0: ПАРТНЁРЫ И КЭШБЭК (#/partners) — партнёрский канал из плана.
//  Список партнёров, кэшбэк баллами за их чеки, QR «на кассе» (SVG).
// --------------------------------------------------------------------------
async function viewPartners(container) {
  container.innerHTML = `
  <div class="glass card">
    <div class="card-title">🤝 Партнёры и кэшбэк <span class="form-hint">(Этап 9 · v1.38.0)</span></div>
    <div id="partners-body" class="form-hint" style="margin-bottom:10px">Загружаем…</div>
    <div id="partners-table"></div>
  </div>`;
  try {
    const d = await api.get('/api/v1/public/pool/partners');
    const box = $('#partners-body');
    if (box) box.innerHTML = `${esc(d.note)} · баллы зачисляются после верификации чека,
      в карантине антифрода кэшбэк приостанавливается.`;
    const rows = d.items.map((p) => `
      <tr>
        <td>${esc(p.name)}</td><td>${esc(p.city || '—')}</td>
        <td class="num"><b>${p.pct}%</b></td>
        <td class="num">${fmtInt(d.cap)}</td>
        <td><img src="/api/v1/public/pool/partners/qr.svg?code=${encodeURIComponent(p.code)}"
             alt="QR партнёра ${esc(p.name)}" width="88" height="88"
             style="border:1px solid #e3e3e3;border-radius:6px;background:#fff"></td>
        <td><button class="btn btn-sm" data-code="${esc(p.code)}" data-name="${esc(p.name)}">🖨 Печать QR</button></td>
      </tr>`).join('');
    $('#partners-table').innerHTML = `
      <div class="form-hint" style="margin-bottom:6px">QR ведёт на страницу «Сдать чек» — участник сдаёт чек партнёра
        и получает кэшбэк баллами. QR печатается партнёром и ставится у кассы.</div>
      <div class="table-wrap"><table style="width:100%">
        <thead><tr><th>Партнёр</th><th>Город</th><th>Кэшбэк</th><th>Кап, баллов/чек</th><th>QR на кассу</th><th></th></tr></thead>
        <tbody>${rows || '<tr><td colspan="6">Партнёры скоро появятся</td></tr>'}</tbody></table></div>`;
    $$('#partners-table button[data-code]').forEach((b) => {
      b.onclick = () => {
        const w = window.open('', '_blank');
        if (!w) return;
        w.document.write(`<html><head><title>QR — ${esc(b.dataset.name)}</title></head>
          <body style="font-family:Arial,sans-serif;text-align:center;padding:40px">
          <h2>${esc(b.dataset.name)}</h2>
          <p>Сдайте чек в «Чек-Пул» — получите кэшбэк баллами</p>
          <img src="/api/v1/public/pool/partners/qr.svg?code=${encodeURIComponent(b.dataset.code)}" width="320" height="320" style="margin:0 auto">
          <p style="color:#777;font-size:12px">ООО «Ямастер» · ymaster.ru</p>
          </body></html>`);
        w.document.close();
        w.focus();
        w.print();
      };
    });
  } catch (e) {
    const box = $('#partners-body');
    if (box) box.innerHTML = `Не удалось загрузить партнёров: ${esc(e.message)}`;
  }
}

async function viewFraud(container) {
  container.innerHTML = fraudShell();
  $('#fraud-status').value = fraud.status;
  $('#fraud-status').onchange = (e) => { fraud.status = e.target.value; fraud.page = 1; fraudLoadTable(); };
  $('#fraud-sev').value = String(fraud.minSev);
  $('#fraud-sev').onchange = (e) => { fraud.minSev = parseInt(e.target.value, 10) || 0; fraud.page = 1; fraudLoadTable(); };
  $('#fraud-bulk-fp').onclick = () => {
    const ids = [...container.querySelectorAll('.fraud-sel:checked')].map(c => c.dataset.id);
    if (ids.length) fraudResolve(ids, 'false_positive');
  };
  $('#fraud-bulk-confirm').onclick = () => {
    const ids = [...container.querySelectorAll('.fraud-sel:checked')].map(c => c.dataset.id);
    if (ids.length) fraudResolve(ids, 'confirmed');
  };
  $('#fraud-purge').onclick = async () => {
    try { toast((await api.post('/api/v1/pool-fraud/purge', {})).message, 'ok', '🧹'); }
    catch (e) { toast(e.message, 'err', '🧹'); }
  };
  $('#fraud-graph-btn').onclick = () => {
    const box = $('#fraud-graph');
    if (box.classList.contains('hidden')) { box.classList.remove('hidden'); fraudLoadGraph(box); }
    else box.classList.add('hidden');
  };
  fraudLoadStats();
  fraudLoadTable();
}

// v1.38.0: граф связей — SVG-кластеры участников (устройство/подсеть/реферал)
const FRAUD_EDGE_COLORS = { device: '#c0392b', subnet: '#e5770f', referral: '#2471a3' };

async function fraudLoadGraph(box) {
  box.innerHTML = '<div class="form-hint">Строим граф…</div>';
  try {
    const d = await api.get('/api/v1/pool-fraud/graph?days=30&max_nodes=60');
    if (!d.nodes.length) {
      box.innerHTML = '<div class="info-callout">Связанных групп не найдено — признаки общих устройств, подсетей и реферальных пар за 30 дней отсутствуют.</div>';
      return;
    }
    // раскладка: кластеры сеткой, участники — по окружности центра кластера
    const ids = d.nodes.map((n) => n.id);
    const pos = {};
    const edges = d.edges.map((e) => ({ ...e }));
    // кластеры уже приходят связные; раскладываем по компонентам
    const adj = {}; ids.forEach((i) => adj[i] = []);
    edges.forEach((e) => { adj[e.a].push(e.b); adj[e.b].push(e.a); });
    const seen = new Set(); const comps = [];
    ids.forEach((s) => { if (seen.has(s)) return;
      const q = [s]; const comp = []; seen.add(s);
      while (q.length) { const v = q.pop(); comp.push(v);
        adj[v].forEach((w) => { if (!seen.has(w)) { seen.add(w); q.push(w); } }); }
      comps.push(comp); });
    comps.sort((a, b) => b.length - a.length);
    const perRow = 3, cellW = 300, cellH = 260;
    const rows = Math.ceil(comps.length / perRow);
    const W = Math.min(perRow, comps.length) * cellW, H = rows * cellH;
    comps.forEach((comp, ci) => {
      const cx = (ci % perRow) * cellW + cellW / 2;
      const cy = Math.floor(ci / perRow) * cellH + cellH / 2;
      const R = Math.min(cellW, cellH) / 2 - 46;
      comp.forEach((uid, i) => {
        const a = (2 * Math.PI * i) / comp.length - Math.PI / 2;
        pos[uid] = [cx + R * Math.cos(a), cy + R * Math.sin(a)];
      });
    });
    const byId = {}; d.nodes.forEach((n) => byId[n.id] = n);
    const lines = edges.map((e) => {
      const [x1, y1] = pos[e.a], [x2, y2] = pos[e.b];
      const c = FRAUD_EDGE_COLORS[e.kind] || '#999';
      return `<line x1="${x1.toFixed(1)}" y1="${y1.toFixed(1)}" x2="${x2.toFixed(1)}" y2="${y2.toFixed(1)}" stroke="${c}" stroke-width="1.6" stroke-opacity="0.75"><title>${e.kind === 'device' ? 'общее устройство' : e.kind === 'subnet' ? 'общая подсеть /24' : 'реферальная пара'}</title></line>`;
    }).join('');
    const dots = d.nodes.map((n) => {
      const [x, y] = pos[n.id];
      const fill = n.quarantined ? '#c0392b' : n.risk >= 40 ? '#e5770f' : '#4a7f4a';
      return `<g class="fraud-node" data-uid="${n.id}" style="cursor:pointer">
        <circle cx="${x.toFixed(1)}" cy="${y.toFixed(1)}" r="9" fill="${fill}" stroke="#fff" stroke-width="1.5"><title>${esc(n.label)} · риск ${n.risk}${n.quarantined ? ' · карантин' : ''}</title></circle>
        <text x="${x.toFixed(1)}" y="${(y + 24).toFixed(1)}" text-anchor="middle" font-size="10" fill="#555">${esc(n.label.slice(0, 16))}</text></g>`;
    }).join('');
    box.innerHTML = `
      <div class="info-callout" style="margin-bottom:6px">Группы участников за ${d.days} дн. Цвет точки: красный — карантин, оранжевый — риск ≥ 40, зелёный — норма.
        Рёбра: <span style="color:#c0392b">■</span> общее устройство · <span style="color:#e5770f">■</span> общая подсеть · <span style="color:#2471a3">■</span> реферальная пара.
        Нажмите на участника — карточка. Групп: <b>${comps.length}</b>.</div>
      <div style="overflow-x:auto"><svg viewBox="0 0 ${W} ${H}" width="${Math.min(W, 900)}" height="${H * Math.min(W, 900) / W}" style="background:#fafafa;border:1px solid #e3e3e3;border-radius:8px">${lines}${dots}</svg></div>`;
    box.querySelectorAll('.fraud-node').forEach((g) => {
      g.onclick = () => fraudUserCard(g.dataset.uid);
    });
  } catch (e) { box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`; }
}

// --------------------------------------------------------------------------
//  Регистрация по приглашению
// --------------------------------------------------------------------------
async function showRegister(token) {
  $('#login-screen').classList.add('hidden');
  $('#app-shell').classList.add('hidden');
  $('#register-screen').classList.remove('hidden');
  // Всегда есть выход на обычный вход (приглашение может оказаться недействительным)
  const back = document.getElementById('reg-back-to-login');
  if (back) back.onclick = (e) => { e.preventDefault(); history.replaceState(null, '', location.pathname); showLogin(); };

  const badge = $('#reg-invite-badge');
  try {
    const info = await api.get(`/api/v1/auth/invite-info?token=${encodeURIComponent(token)}`);
    if (!info.valid) {
      badge.className = 'chip failed';
      badge.innerHTML = '<span class="dot"></span>Приглашение недействительно';
      $('#register-form').classList.add('hidden');
      toast(info.message || 'Приглашение недействительно', 'err', 'Регистрация');
      return;
    }
    badge.className = `chip ${info.role === 'accountant' ? 'exported' : 'new'}`;
    badge.innerHTML = `<span class="dot"></span>Роль: ${roleLabel(info.role)}${info.company_name ? ' · компания: <b>' + esc(info.company_name) + '</b>' : ''}${info.note ? ' · ' + esc(info.note) : ''}`;
  } catch (e) {
    badge.className = 'chip failed';
    badge.innerHTML = '<span class="dot"></span>Ошибка проверки приглашения';
    return;
  }

  $('#register-form').onsubmit = async (e) => {
    e.preventDefault();
    const btn = $('#register-submit');
    btn.disabled = true;
    btn.innerHTML = '<span class="spinner"></span>';
    $('#register-error').classList.add('hidden');
    try {
      const r = await api.post('/api/v1/auth/register', {
        token,
        username: $('#reg-username').value.trim(),
        password: $('#reg-password').value,
        full_name: $('#reg-fullname').value.trim(),
      });
      setToken(r.access_token);
      state.me = r.user;
      history.replaceState(null, '', location.pathname + '#/dashboard');
      toast(`Аккаунт создан. Ваша роль: ${roleLabel(r.user.role)}`, 'ok', 'Добро пожаловать!');
      enterApp();
    } catch (err) {
      const el = $('#register-error');
      el.textContent = err.message || 'Ошибка регистрации';
      el.classList.remove('hidden');
    } finally {
      btn.disabled = false;
      btn.textContent = 'Создать аккаунт';
    }
  };
}

// --------------------------------------------------------------------------
//  Оболочка приложения
// --------------------------------------------------------------------------
function enterApp() {
  $('#login-screen').classList.add('hidden');
  $('#register-screen').classList.add('hidden');
  $('#app-shell').classList.remove('hidden');
  $('#user-name').textContent = state.me.full_name || state.me.username;
  $('#user-role').textContent = roleLabel(state.me.role);
  $('#user-avatar').textContent = (state.me.full_name || state.me.username)[0].toUpperCase();
  $$('.admin-only').forEach(el => el.classList.toggle('hidden', !isAdmin()));
  $$('.accountant-only').forEach(el => el.classList.toggle('hidden', !isAccountant()));
  applyViewAsMode();               // v1.18.0: полоса режима просмотра + меню профиля
  connectWS();
  if (isAdmin()) initCompanyFilter();          // v1.11.0: селектор пространства

  // Один обработчик выхода (без дублирования при повторных входах)
  if (!window.__ymasterLogoutBound) {
    window.__ymasterLogoutBound = true;
    window.addEventListener('ymaster:logout', () => logout());
  }

  // При входе по ссылке-приглашению убираем токен приглашения из адреса
  // (replaceState — без лишнего hashchange, иначе route() сработает дважды)
  if (!location.hash || location.hash.startsWith('#/register')) {
    history.replaceState(null, '', location.pathname + '#/dashboard');
  }
  // v1.18.0: в режиме просмотра окно смены пароля не мешает осмотру
  if (state.me.must_change_password && !isViewingAs()) forcePasswordChange();
  showWhatsNew();
  route();
  if (isAdmin()) checkUpdatesSilently();   // v1.4.0: авто-проверка при запуске
  const pwaBtn = document.getElementById('pwa-install-btn');
  if (pwaBtn && !pwaBtn.dataset.bound) {
    pwaBtn.dataset.bound = '1';
    pwaBtn.onclick = showInstallDialog;
  }
  refreshBadges();
  window.addEventListener('online', flushOfflineQueue);
  flushOfflineQueue();
}

function logout() {
  clearToken();
  // Очищаем и серверную cookie сессии (fire-and-forget)
  try { fetch('/api/v1/auth/logout', { method: 'POST' }); } catch (e) {}
  // v1.18.0: выход из режима просмотра не оставляет «хвостов»
  viewAsForget();
  state.me = null;
  state.receiptsSelected.clear();
  if (state.ws) { try { state.ws.close(); } catch (e) {} state.ws = null; }
  if (state.camera) { state.camera.stop(); state.camera = null; }
  history.replaceState(null, '', location.pathname);
  location.reload();
}

// ==========================================================================
// v1.18.0: РЕЖИМ ПРОСМОТРА — администратор видит приложение глазами
// бухгалтера или сотрудника. Переключение — по иконке профиля в правом
// верхнем углу; ПАРОЛЬ НЕ ЗАПРАШИВАЕТСЯ: админ-сессия сохраняется в
// sessionStorage и возвращается одной кнопкой. Токен просмотра выдан
// сервером на целевого пользователя (claim «act» = администратор), поэтому
// админ-эндпоинты в этом режиме недоступны — всё ровно как у пользователя.
// ==========================================================================
const VIEWAS_TOK = 'ymaster_viewas_master_token';   // админ-токен на время просмотра
const VIEWAS_NAME = 'ymaster_viewas_admin_name';    // имя администратора для полосы
// v1.41.0: просмотр кабинета участника Чек-Пула (ядро-логин админа не меняется)
const VIEWAS_POOL = 'ymaster_viewas_pool';            // {id, name} участника
const VIEWAS_POOL_PREV = 'ymaster_viewas_pool_prev';  // прежний токен кабинета

const isViewingAs = () => !!(state.me && state.me.viewing_as);

const isViewingPool = () => {
  try { return !!sessionStorage.getItem(VIEWAS_POOL); } catch (e) { return false; }
};
function viewAsPoolInfo() {
  try { return JSON.parse(sessionStorage.getItem(VIEWAS_POOL) || 'null'); }
  catch (e) { return null; }
}
function viewAsPoolForget() {
  try { sessionStorage.removeItem(VIEWAS_POOL); } catch (e) {}
  try { sessionStorage.removeItem(VIEWAS_POOL_PREV); } catch (e) {}
}

function viewAsForget() {
  try { sessionStorage.removeItem(VIEWAS_TOK); } catch (e) {}
  try { sessionStorage.removeItem(VIEWAS_NAME); } catch (e) {}
  viewAsPoolForget();
}

// Полоса возврата под шапкой + скрытие «Выйти» в режиме просмотра
function applyViewAsMode() {
  const bar = $('#viewas-bar');
  const chip = $('#user-chip');
  if (!bar || !chip) return;
  const viewing = isViewingAs();
  const poolV = isViewingPool();
  bar.classList.toggle('hidden', !viewing && !poolV);
  $('#btn-logout').classList.toggle('hidden', viewing || poolV);
  if (poolV) {
    const info = viewAsPoolInfo();
    $('#viewas-text').innerHTML =
      `Вы смотрите <b>кабинет Чек-Пула</b> глазами участника ` +
      `<b>${esc((info && info.name) || '')}</b> ` +
      `<span class="viewas-role">(участник пула)</span>`;
    $('#btn-viewas-back').onclick = stopViewAsPool;
  }
  if (viewing) {
    const nm = state.me.full_name || state.me.username;
    $('#viewas-text').innerHTML =
      `Вы смотрите приложение как <b>${esc(nm)}</b> ` +
      `<span class="viewas-role">(${roleLabel(state.me.role)})</span>` +
      (state.me.must_change_password
        ? ' <span class="viewas-warn">этот пользователь ещё не сменил временный пароль</span>' : '');
    $('#btn-viewas-back').onclick = stopViewAs;
  }
  chip.setAttribute('aria-expanded', 'false');
  // v1.42.0: меню профиля/переключения ролей — только администратор
  // (в режиме просмотра — возврат). Бухгалтер и сотрудник меню не видят.
  if (isAdmin() || viewing) {
    chip.onclick = (e) => {
      if (e.target.closest('#btn-logout')) return; // «Выйти» — отдельное действие
      togglePersonaMenu();
    };
    chip.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePersonaMenu(); }
    };
  } else {
    chip.onclick = null;
    chip.onkeydown = null;
  }
}

function closePersonaMenu() {
  const m = $('#persona-menu');
  if (m) { m.classList.add('hidden'); $('#user-chip').setAttribute('aria-expanded', 'false'); }
}

function togglePersonaMenu() {
  const m = $('#persona-menu');
  if (!m) return;
  if (!m.classList.contains('hidden')) { closePersonaMenu(); return; }
  m.classList.remove('hidden');
  $('#user-chip').setAttribute('aria-expanded', 'true');
  renderPersonaMenu(m);
}

async function renderPersonaMenu(m) {
  if (isViewingAs()) {
    // В режиме просмотра меню предлагает только возврат
    m.innerHTML =
      `<div class="pm-head">👁 Режим просмотра</div>
       <div class="pm-note">Вы вошли как <b>${esc(state.me.full_name || state.me.username)}</b>
       (${roleLabel(state.me.role)}). Действия выполняются от его имени.</div>
       <button class="pm-item pm-back" id="pm-return">🛡 Вернуться в администратора</button>`;
    $('#pm-return').onclick = stopViewAs;
    return;
  }
  if (!isAdmin()) {
    // v1.42.0: меню переключения ролей — ТОЛЬКО у администратора.
    // Бухгалтер и сотрудник видят только свой профиль.
    m.innerHTML =
      `<div class="pm-head">Мой профиль</div>
       <div class="pm-item pm-self">👤 <span class="pm-name">${esc(state.me.full_name || state.me.username)}</span>
         <span class="pm-tag">${esc(roleLabel(state.me.role))} · это вы</span></div>
       <div class="pm-note">Переключение между профилями доступно только администратору.</div>`;
    return;
  }
  m.innerHTML =
    `<div class="pm-head">Мой профиль</div>
     <div class="pm-item pm-self">🛡 <span class="pm-name">${esc(state.me.full_name || state.me.username)}</span>
       <span class="pm-tag">администратор · это вы</span></div>
     <div class="pm-head">Посмотреть глазами <span class="pm-hint">пароль не нужен</span></div>
     <div class="pm-body"><div class="pm-none"><span class="spinner"></span></div></div>`;
  try {
    const users = await api.get('/api/v1/users');
    const act = users.filter(u => u.is_active && u.role !== 'admin');
    const acc = act.filter(u => u.role === 'accountant');
    const usr = act.filter(u => u.role === 'user');
    const row = (u) =>
      `<button class="pm-item" data-uid="${esc(u.id)}" role="menuitem">
         <span class="pm-dot ${u.role === 'accountant' ? 'acc' : 'usr'}"></span>
         <span class="pm-name">${esc(u.full_name || u.username)}</span>
         <span class="pm-sub">${esc(u.username)}${u.company_name ? ' · ' + esc(u.company_name) : ''}</span>
       </button>`;
    m.querySelector('.pm-body').innerHTML =
      `<div class="pm-group">Бухгалтеры</div>` +
      (acc.length ? acc.map(row).join('') : '<div class="pm-none">нет бухгалтеров</div>') +
      `<div class="pm-group">Сотрудники</div>` +
      (usr.length ? usr.map(row).join('') : '<div class="pm-none">нет сотрудников</div>');
    m.querySelectorAll('.pm-item[data-uid]').forEach(b => {
      b.onclick = () => startViewAs(
        b.dataset.uid, b.querySelector('.pm-name').textContent);
    });
    // v1.41.0: третья персона — участник Чек-Пула (кабинет глазами участника)
    if (isAdmin()) {
      const box = document.createElement('div');
      box.innerHTML =
        `<div class="pm-head">Участники Чек-Пула <span class="pm-hint">кабинет участника</span></div>
         <div class="pm-body" id="pm-pool"><div class="pm-none"><span class="spinner"></span></div></div>`;
      m.appendChild(box);
      const pb = box.querySelector('#pm-pool');
      try {
        const ppl = await api.get('/api/v1/pool-admin/participants?limit=6');
        const prow = (u) =>
          `<button class="pm-item" data-pid="${esc(u.id)}" role="menuitem">
             <span class="pm-dot ${u.is_blocked ? '' : 'acc'}"></span>
             <span class="pm-name">${esc(u.email || u.nickname || u.id)}</span>
             <span class="pm-sub">${fmtInt(u.points)} баллов · ${fmtInt(u.receipts)} чеков${u.is_blocked ? ' · заблокирован' : ''}</span>
           </button>`;
        pb.innerHTML =
          (ppl.items.length
            ? ppl.items.map(prow).join('')
            : '<div class="pm-none">участников пока нет</div>') +
          `<button class="pm-item" id="pm-people-all" role="menuitem">
             <span class="pm-name">Все участники →</span>
             <span class="pm-sub">раздел «Чек-Пул: участники»</span></button>`;
        pb.querySelectorAll('.pm-item[data-pid]').forEach(b => {
          b.onclick = () => startViewAsPool(
            b.dataset.pid, b.querySelector('.pm-name').textContent);
        });
        const all = pb.querySelector('#pm-people-all');
        if (all) all.onclick = () => {
          closePersonaMenu();
          if (location.hash === '#/poolpeople') route(true);
          else location.hash = '#/poolpeople';
        };
      } catch (e) {
        pb.innerHTML = `<div class="pm-none">${esc(e.message || 'Список недоступен')}</div>`;
      }
    }
  } catch (e) {
    m.querySelector('.pm-body').innerHTML =
      `<div class="pm-none">${esc(e.message || 'Не удалось загрузить список')}</div>`;
  }
}

async function startViewAs(uid, name) {
  closePersonaMenu();
  const { slot, close } = openModal(
    `<div class="modal-title">👁 Режим просмотра</div>
     <p class="modal-text">Открыть приложение глазами <b>${esc(name)}</b>?<br>
     Экран перезагрузится в его профиле; действия будут выполняться от его имени.
     Возврат — кнопка «↩ Вернуться в администратора» вверху. Пароль не требуется.</p>
     <div class="modal-actions">
       <button class="btn" id="va-cancel">Отмена</button>
       <button class="btn btn-primary" id="va-go">Открыть его профиль</button>
     </div>`);
  $('#va-cancel', slot).onclick = close;
  $('#va-go', slot).onclick = async () => {
    try {
      const r = await api.post(`/api/v1/admin/impersonate/${uid}`);
      try {
        sessionStorage.setItem(VIEWAS_TOK, getToken());
        sessionStorage.setItem(VIEWAS_NAME,
          state.me.full_name || state.me.username || 'Администратор');
      } catch (e) {}
      setToken(r.access_token);
      close();
      location.reload();      // полный перезапуск интерфейса в новой роли
    } catch (e) {
      toast(e.message || 'Не удалось переключиться', 'err', 'Режим просмотра');
    }
  };
}

async function stopViewAs() {
  closePersonaMenu();
  try {
    const r = await api.post('/api/v1/admin/impersonate/stop');
    viewAsForget();
    setToken(r.access_token);
  } catch (e) {
    // Запасной путь: возвращаем сохранённый админ-токен
    let saved = null;
    try { saved = sessionStorage.getItem(VIEWAS_TOK); } catch (err) {}
    if (!saved) { toast(e.message || 'Не удалось вернуться', 'err', 'Режим просмотра'); return; }
    viewAsForget();
    setToken(saved);
  }
  location.reload();
}

// v1.41.0: просмотр кабинета участника Чек-Пула глазами самого участника.
// Ядро-логин администратора сохраняется; кабинету выдаётся его pool-токен.
async function startViewAsPool(pid, name) {
  closePersonaMenu();
  const { slot, close } = openModal(
    `<div class="modal-title">👁 Режим просмотра · Чек-Пул</div>
     <p class="modal-text">Открыть <b>кабинет участника</b> глазами <b>${esc(name)}</b>?<br>
     Вы увидите его чеки, баллы и настройки пула ровно так, как видит их он.
     Действия будут выполняться от его имени. Возврат — кнопка
     «↩ Вернуться в администратора» вверху. Пароль не требуется.</p>
     <div class="modal-actions">
       <button class="btn" id="pv-cancel">Отмена</button>
       <button class="btn btn-primary" id="pv-go">Открыть кабинет</button>
     </div>`);
  $('#pv-cancel', slot).onclick = close;
  $('#pv-go', slot).onclick = async () => {
    try {
      const r = await api.post(`/api/v1/pool-admin/impersonate-pool/${pid}`);
      try {
        sessionStorage.setItem(VIEWAS_POOL_PREV, pGet());
        sessionStorage.setItem(VIEWAS_POOL, JSON.stringify({ id: pid, name }));
      } catch (e) {}
      pSet(r.pool_token);
      close();
      if (location.hash === '#/my') route(true); else location.hash = '#/my';
      applyViewAsMode();
      toast(r.message, 'ok', 'Режим просмотра');
    } catch (e) {
      toast(e.message || 'Не удалось открыть кабинет', 'err', 'Режим просмотра');
    }
  };
}

async function stopViewAsPool() {
  const info = viewAsPoolInfo();
  try {
    if (info) await api.post('/api/v1/pool-admin/impersonate-pool/stop',
                             { participant_id: info.id });
  } catch (e) { /* выходим из просмотра в любом случае */ }
  let prev = '';
  try { prev = sessionStorage.getItem(VIEWAS_POOL_PREV) || ''; } catch (e) {}
  viewAsPoolForget();
  if (prev) pSet(prev); else pClear();
  if (location.hash === '#/dashboard') route(true); else location.hash = '#/dashboard';
  applyViewAsMode();
  toast('Вы вернулись в профиль администратора', 'ok', 'Режим просмотра');
}

// Обязательная смена временного пароля (первый вход администратора / после сброса)
function forcePasswordChange() {
  if (document.getElementById('force-change-overlay')) return; // уже показано
  const overlay = document.createElement('div');
  overlay.id = 'force-change-overlay';
  overlay.style.cssText = 'position:fixed;inset:0;z-index:200;background:rgba(4,7,16,.85);backdrop-filter:blur(6px);display:flex;align-items:center;justify-content:center;padding:20px';
  overlay.innerHTML = `
    <div class="glass" style="width:min(440px,94vw);padding:30px">
      <h2 style="font-size:19px;margin-bottom:8px">🔒 Смените пароль</h2>
      <p style="color:var(--text-dim);font-size:13px;margin-bottom:16px">
        Вход выполнен с временным паролем. Для защиты системы задайте собственный —
        это обязательный шаг.</p>
      <div style="display:flex;flex-direction:column;gap:12px">
        <label class="field"><span>Временный пароль</span>
          <input type="password" id="fc-old"></label>
        <label class="field"><span>Новый пароль (мин. 6 символов)</span>
          <input type="password" id="fc-new"></label>
        <label class="field"><span>Повторите новый пароль</span>
          <input type="password" id="fc-new2"></label>
        <div id="fc-error" class="form-error hidden"></div>
        <button class="btn btn-primary btn-block" id="fc-save">Сохранить пароль</button>
        <button class="btn btn-block" id="fc-logout">Выйти из системы</button>
      </div>
    </div>`;
  document.body.appendChild(overlay);
  $('#fc-save', overlay).onclick = async () => {
    const oldP = $('#fc-old', overlay).value;
    const newP = $('#fc-new', overlay).value;
    const err = $('#fc-error', overlay);
    if (newP.length < 6) { err.textContent = 'Минимум 6 символов'; err.classList.remove('hidden'); return; }
    if (newP !== $('#fc-new2', overlay).value) { err.textContent = 'Пароли не совпадают'; err.classList.remove('hidden'); return; }
    try {
      await api.post('/api/v1/auth/change-password', { old_password: oldP, new_password: newP });
      state.me.must_change_password = false;
      overlay.remove();
      toast('Пароль изменён. Добро пожаловать в систему!', 'ok', 'Готово');
    } catch (e) {
      err.textContent = e.message; err.classList.remove('hidden');
    }
  };
  $('#fc-logout', overlay).onclick = () => logout();
}

// --------------------------------------------------------------------------
//  WebSocket: живые статусы
// --------------------------------------------------------------------------
// v1.2.0: WS без «мерцания» — пауза между попытками растёт до 60 с
// (с разбросом), статус меняется с гистерезисом, «офлайн» показываем
// только при реальной недоступности, а не на каждом переподключении.
const WS_BACKOFF = [2, 5, 10, 20, 30, 60, 60];   // секунды

function connectWS() {
  if (document.hidden) { scheduleReconnect(5); return; }  // в фоне не дёргаемся
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  let ws;
  try { ws = new WebSocket(`${proto}://${location.host}/ws/status`); }
  catch (e) { scheduleReconnect(10); return; }
  state.ws = ws;

  ws.onopen = () => {
    state.wsAttempts = 0;
    setWsStatus(true);
    // heartbeat клиента: не даём прокси считать соединение пустым
    clearInterval(state.wsPing);
    state.wsPing = setInterval(() => {
      if (document.hidden) return;                 // v1.6.0: в фоне не тратим трафик
      if (ws.readyState === 1) { try { ws.send('ping'); } catch (e) {} }
    }, 25000);
  };
  ws.onclose = () => {
    clearInterval(state.wsPing);
    setWsStatus(false);
    const delay = WS_BACKOFF[Math.min(state.wsAttempts || 0, WS_BACKOFF.length - 1)];
    state.wsAttempts = (state.wsAttempts || 0) + 1;
    scheduleReconnect(delay);
  };
  ws.onerror = () => { try { ws.close(); } catch (e) {} };
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      if (msg.type === 'ping') return;             // серверный heartbeat
      handleWsEvent(msg.type, msg.payload);
    } catch { /* ignore */ }
  };
}

function scheduleReconnect(seconds) {
  clearTimeout(state.wsTimer);
  const jitter = seconds * (0.8 + Math.random() * 0.4);   // ±20% — анти-шторм
  state.wsTimer = setTimeout(connectWS, jitter * 1000);
}

// Возврат в приложение (телефон «проснулся») — одно мягкое переподключение
document.addEventListener('visibilitychange', () => {
  if (!document.hidden) {
    updateConnIndicator(true);
    if (!state.ws || state.ws.readyState > 1) {
      clearTimeout(state.wsTimer);
      try { if (state.ws) state.ws.close(); } catch (e) {}
      connectWS();
    }
    // v1.8.0: не чаще раза в 8 с — телефон «просыпается» часто,
    // а каждая перерисовка выглядит как мигание сайта
    if (state.me && Date.now() - (state._lastVisRoute || 0) > 8000) {
      state._lastVisRoute = Date.now();
      route(true);
    }
  }
});
window.addEventListener('online', () => updateConnIndicator(true));
window.addEventListener('offline', () => updateConnIndicator(false));

function updateConnIndicator(force) {
  const lastOk = window.__ymLastApiOk || 0;
  // «Офлайн» только если: браузер считает сеть недоступной ИЛИ и WS не может
  // подключиться (3+ попыток), и API не отвечал дольше 30 секунд.
  const apiStale = Date.now() - lastOk > 30000;
  const reallyOffline = !navigator.onLine ||
    ((state.wsAttempts || 0) >= 3 && apiStale && !(state.ws && state.ws.readyState === 1));
  const ind = $('#offline-indicator');
  if (!ind) return;
  if (force) ind.dataset.lastFlip = '0';
  const now = Date.now();
  const last = Number(ind.dataset.lastFlip || 0);
  if (now - last < 8000) return;                  // гистерезис: не мигаем
  const isHidden = ind.classList.contains('hidden');
  if (reallyOffline && isHidden) { ind.classList.remove('hidden'); ind.dataset.lastFlip = now; }
  if (!reallyOffline && !isHidden) { ind.classList.add('hidden'); ind.dataset.lastFlip = now; }
}

function setWsStatus(ok) {
  // v1.7.0: трогаем DOM только при РЕАЛЬНОЙ смене состояния — иначе текст
  // дёргается при каждом переподключении и кажется, что «мерцает сайт»
  if (state.wsOk === ok) return;
  state.wsOk = ok;
  const dot = $('#ws-status .live-dot');
  const txt = $('#ws-status-text');
  if (dot) { dot.classList.toggle('on', ok); dot.classList.toggle('off', !ok); }
  if (txt) {
    // v1.8.0: одиночный разрыв не показываем — статус перестаёт мигать
    if (ok) txt.textContent = 'Живое подключение';
    else if ((state.wsAttempts || 0) >= 2) txt.textContent = 'Переподключение…';
  }
  updateConnIndicator(false);
}

// v1.6.0: события сливаются в одну перерисовку — при пачке чеков не дёргаем
// сервер N полными запросами, а обновляемся один раз через 1.2 с
let _routeRefreshTimer = null;
function scheduleRouteRefresh(ms = 1200) {
  if (_routeRefreshTimer) return;
  _routeRefreshTimer = setTimeout(() => {
    _routeRefreshTimer = null;
    if (!document.hidden && state.me) {
      // v1.25.1: «Чеки» обновляем НА МЕСТЕ (viewReceipts._refresh) — перестроение
      // вида route() затирало применённые фильтры и оставляло скелетон вместо
      // списка, если данные не изменились (список «пропадал» до F5)
      if (state.view === 'receipts' && typeof viewReceipts._refresh === 'function') {
        viewReceipts._refresh();
      } else {
        route(true);
      }
    }
  }, ms);
}

function handleWsEvent(type, p) {
  if (p && p.demo) { refreshBadges(); if (state.view === 'dashboard' || state.view === 'receipts') scheduleRouteRefresh(); return; }
  switch (type) {
    case 'receipt_created':
      toast(`Чек ФН ${p.fn || ''} ФД ${p.fd || ''} на ${fmtSum(p.total_sum)} принят`, 'ok', 'Новый чек');
      refreshBadges();
      if (state.view === 'dashboard' || state.view === 'receipts') scheduleRouteRefresh();
      break;
    case 'receipt_duplicate':
      toast(`Чек ФН ${p.fn} ФД ${p.fd} уже есть в базе — дубликат отсеян`, 'warn', 'Дубликат');
      break;
    case 'receipt_verifying':
      if (state.view === 'receipts' || state.view === 'dashboard') scheduleRouteRefresh();
      break;
    case 'receipt_verified': {
      const ok = p.fns_status === 'valid';
      toast(`ФНС: чек ФД ${p.fd} — ${statusLabel(p.fns_status)}`, ok ? 'ok' : (p.fns_status === 'invalid' ? 'err' : 'warn'), 'Проверка ФНС');
      refreshBadges();
      if (state.view === 'dashboard' || state.view === 'receipts') scheduleRouteRefresh();
      break;
    }
    case 'receipt_deleted':
      refreshBadges();
      if (state.view === 'receipts') scheduleRouteRefresh();
      break;
    case 'server_update':  // v1.21.0: сервер обновлён — сообщаем и ждём решения
      // кэш чистим тихо (чтобы интерфейс точно подтянулся), перезагрузка — по кнопке
      hardReset(true, false).catch(() => {});
      showUpdateBanner(p.to || '');
      break;
  }
}

async function refreshBadges() {
  try {
    const s = await api.get('/api/v1/dashboard/stats?days=7');
    const badge = $('#nav-receipts-badge');
    badge.textContent = fmtInt(s.total);
    badge.classList.add('accent');
    const q = offlineQueue.size();
    const qb = $('#nav-queue-badge');
    qb.textContent = q;
    qb.classList.toggle('hidden', q === 0);
    qb.classList.toggle('accent', q > 0);
  } catch { /* тихо */ }
}

// --------------------------------------------------------------------------
//  Роутер
// --------------------------------------------------------------------------
// v1.11.0: «открыть пространство» из виджета «По компаниям» (делегирование)
document.addEventListener('click', (e) => {
  const b = e.target.closest('.c-open');
  if (!b) return;
  state.companyFilter = b.dataset.id;
  try { localStorage.setItem('ymaster-company', b.dataset.id); } catch (err) {}
  const sel = $('#company-filter');
  if (sel) sel.value = b.dataset.id;
  route();
});

function route(silent = false) {
  if (state.camera) { state.camera.stop(); state.camera = null; }
  const hash = (location.hash.replace(/^#\//, '') || 'dashboard').split('?')[0];
  const parts = hash.split('/');
  const view = parts[0].split('?')[0];
  if (view === 'register') { location.hash = '#/dashboard'; return; }
  state.routeParam = parts.slice(1).join('/') || '';
  state.view = view;
  const container = $('#view-container');
  $('#page-title').textContent = VIEW_TITLES[view] || 'Ямастер Чек';
  $$('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  $('#sidebar').classList.remove('open');

  // Защита разделов по ролям на клиенте (сервер дублирует)
  const guard = {
    export: isAccountant(), mapping: isAccountant(),
    users: isAdmin(), audit: isAdmin(), companies: isAdmin(),
    pooladmin: isAdmin(),                // v1.33.0: модерация пула
    poolpeople: isAdmin(),               // v1.41.0: участники + просмотр
    fraud: isAdmin(),                    // v1.34.0: антифрод
    poolpick: isAccountant(),            // v1.37.0: подбор для компании
  };
  if (view in guard && !guard[view]) { location.hash = '#/dashboard'; return; }

  const renderers = {
    dashboard: viewDashboard, scan: viewScan, receipts: viewReceipts,
    export: viewExport, mapping: viewMapping, users: viewUsers,
    audit: viewAudit, settings: viewSettings, companies: viewCompanies,
    public: viewPublic,                   // v1.31.0: приём чека в пул
    partners: viewPartners,               // v1.38.0: партнёры и кэшбэк
    my: viewPoolAccount,                  // v1.32.0: кабинет участника пула
    pooladmin: viewPoolAdmin,             // v1.33.0: гео/отрасли + модерация
    poolpeople: viewPoolPeople,           // v1.41.0: участники + просмотр кабинета
    fraud: viewFraud,                     // v1.34.0: сигналы, карантин
    poolpick: viewPoolPick,               // v1.37.0: подбор из пула
  };
  // v1.55.0: сбой отрисовки не оставляет белый экран — возвращаем
  // пользователя на рабочий экран (гость → сдача чека, свой → дашборд)
  const _fallback = (err) => {
    try { console.error("render:", err); } catch (e) {}
    try {
      if (!getToken()) showPublicScreen();
      else location.hash = "#/dashboard";
    } catch (e2) {}
  };
  try {
    Promise.resolve((renderers[view] || viewDashboard)(container)).catch(_fallback);
  } catch (err) { _fallback(err); }
  if (!silent) { void container.offsetWidth; container.classList.add('view-enter'); }
}


// ==========================================================================
//  v1.11.0: МУЛЬТИКОМПАНИЙНОСТЬ — компании, фильтр пространства, перемещение
// ==========================================================================
async function initCompanyFilter() {
  const sel = $('#company-filter');
  if (!sel) return;
  try { state.companies = await api.get('/api/v1/companies'); }
  catch (e) { return; }
  sel.innerHTML = '<option value="all">🏢 Все компании</option>' +
    state.companies.filter(c => c.is_active).map(c =>
      `<option value="${c.id}" ${state.companyFilter === c.id ? 'selected' : ''}>${esc(compName(c))}</option>`).join('');
  sel.classList.remove('hidden');
  sel.onchange = () => {
    state.companyFilter = sel.value;
    try { localStorage.setItem('ymaster-company', sel.value); } catch (e) {}
    route();
  };
}

function companyIdParam() {
  // Параметр фильтра пространства для GET-запросов (только админ)
  return (isAdmin() && state.companyFilter !== 'all') ? state.companyFilter : null;
}

function companyChip(cid) {
  // Бейдж компании для админа (когда не включён фильтр одной компании)
  if (!isAdmin() || !cid) return '';
  const c = state.companies.find(x => x.id === cid);
  if (!c) return '';
  // v1.22.0: сокращённое наименование из карточки, полное — в подсказке
  return ` <span class="chip company-chip" title="Компания: ${esc(c.name)}">${esc(compName(c))}</span>`;
}

function scanCompanyId() {
  // Компания для НОВОГО чека (админ сканирует в выбранное пространство)
  return companyIdParam();
}

// v1.11.0: приглашение с привязкой к компании (глобальная — используется
// и на экране «Пользователи», и на экране «Компании»)
function inviteDialog(companyIdPref = '', companyName = '') {
    // v1.12.0: компания — первичное поле: выбрать из списка или ввести новую
    // (создастся автоматически); по компании определяется группа доступа
    const prefName = companyName || (state.companies.find(c => c.id === companyIdPref) || {}).name || '';
    const { slot } = openModal(`
      <div class="modal-title">✉️ Новое приглашение</div>
      <div class="form-grid">
        ${isAdmin() ? `<label class="field full"><span>Компания — группа доступа (выберите или введите новую)</span>
          <input id="iv-company-name" list="iv-company-list" value="${esc(prefName)}"
                 placeholder="ООО «Партнёр-СВ», ИП Иванов…">
          <datalist id="iv-company-list">
            ${state.companies.filter(c => c.is_active).map(c =>
              `<option value="${esc(c.name)}"></option>`).join('')}
          </datalist></label>` : ''}
        <label class="field"><span>Роль нового пользователя</span>
          <select id="iv-role">
            <option value="accountant">Бухгалтер (расширенные права)</option>
            <option value="user">Пользователь (сканирование)</option>
          </select></label>
        <label class="field"><span>Срок действия, часов</span>
          <input id="iv-hours" type="number" value="72" min="1" max="8760"></label>
        <label class="field"><span>Сколько раз можно использовать</span>
          <input id="iv-uses" type="number" value="1" min="1" max="200"></label>
        <label class="field full"><span>Памятка (для кого ссылка, необязательно)</span>
          <input id="iv-note" placeholder="Иванова — бухгалтерия"></label>
      </div>
      <div class="modal-actions">
        <button class="btn" data-close>Отмена</button>
        <button class="btn btn-primary" id="iv-save">Создать ссылку</button>
      </div>`);
    slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
    slot.querySelector('#iv-save').onclick = async () => {
      try {
        // v1.12.0: резолюция компании по названию (есть — берём, нет — создаём)
        const nameInput = slot.querySelector('#iv-company-name');
        let companyNameVal = nameInput ? nameInput.value.trim() : '';
        if (companyNameVal && !state.companies.some(
            c => c.name.toLowerCase() === companyNameVal.toLowerCase())) {
          const created = await api.post('/api/v1/companies', { name: companyNameVal });
          state.companies.push(created);
        }
        const inv = await api.post('/api/v1/invites', {
          role: slot.querySelector('#iv-role').value,
          expires_hours: +slot.querySelector('#iv-hours').value || 72,
          max_uses: +slot.querySelector('#iv-uses').value || 1,
          note: slot.querySelector('#iv-note').value.trim(),
          ...(companyNameVal ? { company_name: companyNameVal } : {}),
        });
        $('#modal-root').classList.add('hidden');
        const url = `${location.origin}/#/register/${inv.token}`;
        const { slot: s2 } = openModal(`
          <div class="modal-title">🔗 Ссылка-приглашение готова</div>
          <div class="info-callout">Роль: <b>${roleLabel(inv.role)}</b> ·
            использований: ${inv.max_uses} · действует до ${inv.expires_at ? fmtDate(inv.expires_at) : '∞'}</div>
          <div class="token-line"><input readonly value="${esc(url)}" id="iv-url">
            <button class="btn btn-sm" id="iv-copy">копировать</button>
            <button class="btn btn-sm btn-primary" id="iv-qr">▣ QR-код</button></div>
          <p class="form-hint" style="margin-top:10px">Отправьте ссылку сотруднику (мессенджер, почта)
            или покажите QR-код — ему достаточно навести камеру телефона.
            После перехода он создаст логин и пароль — роль присвоится автоматически.</p>
          <div class="modal-actions"><button class="btn btn-primary" data-close>Готово</button></div>`);
        s2.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
        s2.querySelector('#iv-copy').onclick = () => {
          navigator.clipboard && navigator.clipboard.writeText(url);
          toast('Скопировано', 'ok');
        };
        // v1.8.2: показать QR-код для сканирования с телефона
        s2.querySelector('#iv-qr').onclick = () => openInviteQr(inv, url);
        route(true);
      } catch (e) { toast(e.message, 'err'); }
    };
  }

// --- Экран «Компании» ------------------------------------------------------
async function viewCompanies(container) {
  let comps = [];
  try { comps = await api.get('/api/v1/companies'); } catch (e) {
    container.innerHTML = `<div class="glass card"><p class="form-error">${esc(e.message)}</p></div>`;
    return;
  }
  state.companies = comps;
  const sel = $('#company-filter');
  if (sel) initCompanyFilter();
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">🏢 Компании-клиенты <span class="spacer"></span>
        <button class="btn btn-primary btn-sm" id="cp-add">＋ Новая компания</button>
        <button class="btn btn-sm" id="cp-bulk-refresh" title="Актуализировать реквизиты ЕГРЮЛ всех компаний с ИНН (лимит Checko — 100 запросов/день)">⟳ Обновить ЕГРЮЛ (все)</button>
        <button class="btn btn-sm" id="cp-ao-all" title="Сводный авансовый отчёт по всем компаниям за период">🧾 АО по всем</button></div>
      <p class="form-hint" style="margin-bottom:12px">Каждая компания (ООО, ИП) — изолированное пространство:
      свои сотрудники, свои чеки, своя отчётность. Сотрудники видят только свою компанию,
      вы видите всё и можете перемещать чеки между компаниями.</p>
      <div class="table-wrap" id="cp-table"></div>
    </div>`;
  const rows = comps.map(c => `
    <tr data-id="${c.id}" class="${c.is_active ? '' : 'archived-row'}">
      <td><b title="${esc(c.name)}">${esc(compName(c))}</b>${c.inn ? `<div class="form-hint">ИНН ${esc(c.inn)}</div>` : ''}
          ${c.note ? `<div class="form-hint">${esc(c.note)}</div>` : ''}</td>
      <td>${c.is_active ? '<span class="chip verified">активна</span>' : '<span class="chip unknown">архив</span>'}</td>
      <td>${c.accountants} бух. · ${c.users} сотр.</td>
      <td>${fmtInt(c.receipts)} на ${fmtSum(c.receipts_sum)}</td>
      <td>${c.last_activity ? fmtDate(c.last_activity) : '—'}</td>
      <td style="white-space:nowrap">
        <button class="btn btn-sm cp-card" data-id="${c.id}" title="Карточка компании: ЕГРЮЛ, сотрудники, удаление">🗂</button>
        <button class="btn btn-sm cp-open" data-id="${c.id}" title="Смотреть данные только этой компании">🔓 Открыть</button>
        <button class="btn btn-sm cp-invite" data-id="${c.id}" title="Пригласить сотрудника в компанию">✉️</button>
        <button class="btn btn-sm cp-edit" data-id="${c.id}" title="Переименовать / архив">✏️</button>
      </td>
    </tr>`).join('');
  $('#cp-table').innerHTML = comps.length ? `
    <table class="data"><thead><tr>
      <th>Компания</th><th>Статус</th><th>Сотрудники</th><th>Чеки</th><th>Активность</th><th></th>
    </tr></thead><tbody>${rows}</tbody></table>`
    : emptyState('🏢', 'Компаний пока нет — добавьте первую компанию-клиента');

  $('#cp-add').onclick = () => companyDialog();
  // v1.16.0: партнёрский кабинет — массовые операции
  const cbr = $('#cp-bulk-refresh');
  if (cbr) cbr.onclick = async () => {
    if (!confirm('Обновить карточки ЕГРЮЛ у компаний с ИНН?\n' +
                 'Расходуется лимит Checko (100 запросов/день, до 40 за раз).')) return;
    cbr.disabled = true; cbr.textContent = '…обновляю';
    try {
      const r = await api.post('/api/v1/companies/bulk-refresh', { limit: 40 });
      toast(r.message, 'ok', '🗂');
      if (r.errors && r.errors.length) console.warn('bulk-refresh errors:', r.errors);
      load();
    } catch (e) { toast(e.message, 'err'); }
    cbr.disabled = false; cbr.textContent = '⟳ Обновить ЕГРЮЛ (все)';
  };
  const cao = $('#cp-ao-all');
  if (cao) cao.onclick = () => openAO1Modal();
  $$('.cp-open').forEach(b => b.onclick = () => {
    state.companyFilter = b.dataset.id;
    try { localStorage.setItem('ymaster-company', b.dataset.id); } catch (e) {}
    if (sel) { sel.value = b.dataset.id; }
    location.hash = '#/dashboard';
    toast('Пространство компании включено — «Все компании» в шапке вернёт общий вид', 'ok', '🔓');
  });
  $$('.cp-invite').forEach(b => b.onclick = () => {
    const c = comps.find(x => x.id === b.dataset.id);
    inviteDialog(b.dataset.id, c ? c.name : '');
  });
  $$('.cp-card').forEach(b => b.onclick = () => openCompanyCard(b.dataset.id));
  $$('.cp-edit').forEach(b => b.onclick = () => companyDialog(comps.find(x => x.id === b.dataset.id)));
}

function companyDialog(c = null) {
  // v1.13.0: ИНН (с проверкой контрольных цифр), «Заполнить из Checko»,
  // подсказка о похожих компаниях — дубли отсекаются до создания
  const { slot } = openModal(`
    <div class="modal-title">${c ? '✎ ' + esc(c.name) : '＋ Новая компания-клиент'}</div>
    <div class="form-grid">
      <label class="field"><span>ИНН (10 цифр ООО / 12 цифр ИП)</span>
        <div style="display:flex;gap:8px">
          <input id="cp-inn" value="${esc(c?.inn || '')}" inputmode="numeric" placeholder="7801234564" style="flex:1;min-width:0">
          <button class="btn btn-sm" id="cp-checko" type="button" title="Заполнить карточку из ЕГРЮЛ/ЕГРИП (Checko)">🔍 ЕГРЮЛ</button>
        </div></label>
      <label class="field"><span>Название (ООО «…», ИП …)</span>
        <input id="cp-name" value="${esc(c?.name || '')}" placeholder="ООО «Партнёр-СВ»"></label>
      <div class="full" id="cp-similar" style="display:none"></div>
      ${c ? `<label class="field"><span>Статус</span>
        <select id="cp-active"><option value="1" ${c.is_active ? 'selected' : ''}>Активна</option>
        <option value="0" ${!c.is_active ? 'selected' : ''}>Архив</option></select></label>` : ''}
      <label class="field full"><span>Памятка (договор, контакт)</span>
        <input id="cp-note" value="${esc(c?.note || '')}"></label>
    </div>
    <div class="modal-actions">
      <button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" id="cp-save">Сохранить</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');

  const nameInput = slot.querySelector('#cp-name');
  const innInput = slot.querySelector('#cp-inn');
  const simBox = slot.querySelector('#cp-similar');
  let _simTimer = null;

  async function checkSimilar() {
    const name = nameInput.value.trim();
    if (name.length < 3 || (c && name === c.name)) { simBox.style.display = 'none'; return; }
    try {
      const sims = await api.get('/api/v1/companies/similar?name=' + encodeURIComponent(name)
        + (c ? '&exclude_id=' + c.id : ''));
      if (!sims.length) { simBox.style.display = 'none'; return; }
      simBox.innerHTML = `<div class="info-callout" style="margin:0">⚠ Похожая компания уже есть:
        <b>${esc(sims[0].name)}</b>${sims[0].inn ? ' (ИНН ' + esc(sims[0].inn) + ')' : ''}.
        Если это она — не создавайте дубль, работайте с существующей.</div>`;
      simBox.style.display = 'block';
    } catch (e) { simBox.style.display = 'none'; }
  }
  nameInput.addEventListener('input', () => {
    clearTimeout(_simTimer);
    _simTimer = setTimeout(checkSimilar, 500);
  });

  slot.querySelector('#cp-checko').onclick = async () => {
    const inn = innInput.value.replace(/\D/g, '');
    if (inn.length !== 10 && inn.length !== 12) return toast('ИНН: 10 цифр (ООО) или 12 (ИП)', 'warn');
    const btn = slot.querySelector('#cp-checko');
    btn.disabled = true; btn.textContent = '…ищу';
    try {
      const card = await api.post('/api/v1/companies/lookup-checko', { inn });
      if (!nameInput.value.trim()) nameInput.value = card.name_full;
      toast(`Найдено: ${card.name_full}${card.status ? ' · ' + card.status : ''}`,
            'ok', '🗂 ЕГРЮЛ');
    } catch (e) {
      if (/ключ не задан/i.test(e.message)) {
        const key = prompt('Укажите API-ключ Checko (checko.ru → личный кабинет → API):');
        if (key) {
          try { await api.put('/api/v1/settings/checko', { api_key: key.trim() });
            toast('Ключ сохранён — повторите «ЕГРЮЛ»', 'ok', '🔑'); }
          catch (e2) { toast(e2.message, 'err'); }
        }
      } else { toast(e.message, 'err'); }
    }
    btn.disabled = false; btn.textContent = '🔍 ЕГРЮЛ';
  };

  slot.querySelector('#cp-save').onclick = async () => {
    const name = nameInput.value.trim();
    if (name.length < 2) return toast('Укажите название компании', 'err');
    try {
      if (c) {
        const active = slot.querySelector('#cp-active');
        await api.patch('/api/v1/companies/' + c.id, {
          name, inn: innInput.value.trim(),
          note: slot.querySelector('#cp-note').value.trim(),
          ...(active ? { is_active: active.value === '1' } : {}),
        });
      } else {
        await api.post('/api/v1/companies', {
          name, inn: innInput.value.trim(),
          note: slot.querySelector('#cp-note').value.trim(),
        });
      }
      $('#modal-root').classList.add('hidden');
      toast(c ? 'Компания обновлена' : 'Компания создана — теперь пригласите её бухгалтера', 'ok', '🏢');
      route(true);
    } catch (e) { toast(e.message, 'err'); }
  };
}

// --- Карточка компании: ЕГРЮЛ (Checko), команда, CSV, умное удаление --------
async function openCompanyCard(id) {
  let d;
  try { d = await api.get('/api/v1/companies/' + id + '/card'); }
  catch (e) { return toast(e.message, 'err'); }
  const c = d.company;
  // v1.15.0: все ключевые реквизиты ЕГРЮЛ/ЕГРИП из Checko — в карточке
  const k = d.card || {};
  // v1.16.0: светофор контрагента
  const RISK = {
    green: { icon: '🟢', label: 'Риск не выявлен', color: 'var(--ok, #34d399)' },
    yellow: { icon: '🟡', label: 'Требует внимания', color: '#fbbf24' },
    red: { icon: '🔴', label: 'ВЫСОКИЙ РИСК', color: '#f87171' },
    none: { icon: '⚪', label: 'Нет данных ЕГРЮЛ', color: 'var(--text-faint)' },
  };
  const rk = RISK[(d.risk && d.risk.level) || 'none'] || RISK.none;
  const riskBlock = `<div class="info-callout" style="margin:8px 0;border-left:4px solid ${rk.color}">
      <b>${rk.icon} ${rk.label}</b>
      ${(d.risk && d.risk.reasons || []).length
        ? '<div class="form-hint" style="margin-top:4px">' + d.risk.reasons.map(esc).join('<br>') + '</div>' : ''}
    </div>`;
  const egryl = (k.name_full || k.ogrn) ? `
    <div class="info-callout" style="margin:10px 0">
      ${k.kind === 'individual' ? '<span class="chip">ИП</span> ' : '<span class="chip">ЮЛ</span> '}
      <b>${esc(k.name_full || '')}</b>
      ${k.name_short && k.name_short !== k.name_full ? `<div class="form-hint">${esc(k.name_short)}</div>` : ''}
      ${k.opf ? `<div class="form-hint">${esc(k.opf)}</div>` : ''}
      <div style="margin-top:4px">
        ${k.inn ? 'ИНН <b>' + esc(k.inn) + '</b>' : ''}${k.kpp ? ' · КПП ' + esc(k.kpp) : ''}
        ${k.ogrn ? ' · ' + (k.kind === 'individual' ? 'ОГРНИП' : 'ОГРН') + ' ' + esc(k.ogrn) : ''}
      </div>
      ${k.okpo ? `<div>ОКПО: ${esc(k.okpo)}</div>` : ''}
      ${k.region ? `<div>Регион: ${esc(k.region)}</div>` : ''}
      ${k.reg_date ? `<div>Зарегистрирован(о): ${esc(k.reg_date)}</div>` : ''}
      ${k.address_invalid ? `<div style="color:#f87171">⚠ Адрес признан недостоверным (ЕГРЮЛ)${k.address_invalid_note ? ': ' + esc(k.address_invalid_note) : ''}</div>` : ''}
      ${(k.mass_address_count || 0) >= 10 ? `<div style="color:#fbbf24">⚠ Массовый адрес: ещё ${k.mass_address_count} организаций по тому же адресу</div>` : ''}
      ${(k.founders_count || 0) ? `<div>Учредителей: ${k.founders_count}</div>` : ''}
      ${(k.branches_count || 0) ? `<div>Филиалов/представительств: ${k.branches_count}</div>` : ''}
      ${k.director ? `<div>Руководитель: ${esc(k.director)}${k.management_post ? ' (' + esc(k.management_post) + ')' : ''}</div>` : ''}
      ${k.capital ? `<div>Уставный капитал: ${esc(k.capital)}</div>` : ''}
      ${k.tax_office ? `<div>ИФНС: ${esc(k.tax_office)}${k.tax_office_code ? ' (код ' + esc(k.tax_office_code) + ')' : ''}</div>` : ''}
      ${k.address ? `<div style="margin-top:2px">${esc(k.address)}</div>` : ''}
      ${k.email || k.phone ? `<div>Контакты: ${[k.email, k.phone].filter(Boolean).map(esc).join(' · ')}</div>` : ''}
      ${k.okved ? `<div style="margin-top:2px">ОКВЭД (основной): <b>${esc(k.okved)}</b></div>` : ''}
      ${Array.isArray(k.okved_extra) && k.okved_extra.length
        ? `<div class="form-hint">Доп. ОКВЭД: ${k.okved_extra.map(esc).join('; ')}</div>` : ''}
      ${k.status ? `<div>Статус: <b>${esc(k.status)}</b></div>` : ''}
      ${d.card_updated_at ? `<div class="form-hint">обновлено ${fmtDate(d.card_updated_at)} (Checko.ru)</div>` : ''}
    </div>` : `
    <div class="form-hint" style="margin:10px 0">Карточка ЕГРЮЛ не заполнена — укажите ИНН и нажмите «Обновить из Checko» (ключ — в Настройках).</div>`;
  const { slot, close } = openModal(`
    <div class="modal-title">🗂 ${esc(compName(c))}</div>
    ${c.short_name && c.short_name !== c.name ? `<div class="form-hint" style="margin:-6px 0 4px">полное: ${esc(c.name)}</div>` : ''}
    ${riskBlock}
    <div class="kpi-grid" style="grid-template-columns:repeat(3,1fr);gap:8px;margin-bottom:6px">
      <div class="glass card" style="padding:8px;text-align:center"><div class="kpi-value" style="font-size:18px">${fmtInt(d.receipts)}</div><div class="form-hint">чеков</div></div>
      <div class="glass card" style="padding:8px;text-align:center"><div class="kpi-value" style="font-size:18px">${fmtSum(d.receipts_sum)}</div><div class="form-hint">сумма, ₽</div></div>
      <div class="glass card" style="padding:8px;text-align:center"><div class="kpi-value" style="font-size:18px">${d.team.length}</div><div class="form-hint">сотрудников</div></div>
    </div>
    <div class="form-hint">ИНН: ${esc(c.inn || 'не указан')} · ${c.is_active ? 'активна' : 'в архиве'}</div>
    ${egryl}
    <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
      <button class="btn btn-sm btn-accent" id="cc-reg" title="Свежие данные реестра: сначала официальный сайт ФНС (PDF), затем Checko.ru">⬇ Скачать свежие данные ЕГРЮЛ/ЕГРИП</button>
      <button class="btn btn-sm" id="cc-refresh">⟳ Обновить из Checko</button>
      <button class="btn btn-sm" id="cc-csv">⬇ CSV все чеки</button>
      <button class="btn btn-sm" id="cc-ao">🧾 Авансовый отчёт</button>
      <button class="btn btn-sm" id="cc-inn">✎ ИНН/название</button>
      ${c.is_active ? '<button class="btn btn-sm" id="cc-archive">📦 В архив</button>' : ''}
    </div>
    <div class="modal-actions" style="justify-content:space-between">
      <button class="btn btn-bad" id="cc-delete">🗑 Удалить компанию…</button>
      <button class="btn" data-close>Закрыть</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = close;
  slot.querySelector('#cc-inn').onclick = () => { close(); companyDialog(c); };
  slot.querySelector('#cc-archive').onclick = async () => {
    try { await api.patch('/api/v1/companies/' + c.id, { is_active: false });
      toast('Компания в архиве', 'ok', '📦'); close(); route(true); }
    catch (e) { toast(e.message, 'err'); }
  };
  slot.querySelector('#cc-ao').onclick = () => { close(); openAO1Modal({ companyId: c.id }); };
  slot.querySelector('#cc-csv').onclick = async () => {
    try {
      const { blob, filename } = await api.download('/api/v1/receipts/export-csv?company_id=' + c.id);
      downloadBlob(blob, filename);
    } catch (e) { toast(e.message, 'err'); }
  };
  // v1.22.0: скачивание свежих данных реестра (ФНС PDF → Checko HTML)
  slot.querySelector('#cc-reg').onclick = async () => {
    const btn = slot.querySelector('#cc-reg');
    btn.disabled = true; btn.textContent = '…запрашиваю реестр';
    try {
      const { blob, filename } = await api.download(`/api/v1/companies/${c.id}/registry/download`, {});
      downloadBlob(blob, filename);
      const isPdf = /\.pdf$/i.test(filename || '');
      toast(isPdf
        ? 'Официальная выписка ФНС (PDF) скачана'
        : 'Выписка сформирована по данным Checko.ru (HTML) — карточка в системе обновлена',
        'ok', '⬇ Свежие данные');
      btn.disabled = false; btn.textContent = '⬇ Скачать свежие данные ЕГРЮЛ/ЕГРИП';
    } catch (e) {
      btn.disabled = false; btn.textContent = '⬇ Скачать свежие данные ЕГРЮЛ/ЕГРИП';
      toast(e.message || 'Реестр недоступен', 'err', '⬇ Свежие данные');
    }
  };
  slot.querySelector('#cc-refresh').onclick = async () => {
    if (!c.inn) { close(); return companyDialog(c); }
    const btn = slot.querySelector('#cc-refresh');
    btn.disabled = true; btn.textContent = '…запрашиваю Checko';
    try { await api.post(`/api/v1/companies/${c.id}/refresh-card`, {});
      toast('Карточка обновлена из ЕГРЮЛ', 'ok', '🗂'); close(); openCompanyCard(id); }
    catch (e) {
      btn.disabled = false; btn.textContent = '⟳ Обновить из Checko';
      if (/ключ не задан/i.test(e.message)) {
        const key = prompt('Укажите API-ключ Checko (checko.ru → личный кабинет → API):');
        if (key) { try { await api.put('/api/v1/settings/checko', { api_key: key.trim() });
          toast('Ключ сохранён — повторите', 'ok', '🔑'); } catch (e2) { toast(e2.message, 'err'); } }
      } else { toast(e.message, 'err'); }
    }
  };
  slot.querySelector('#cc-delete').onclick = () => { close(); openCompanyDelete(d); };
}

function openCompanyDelete(d) {
  const c = d.company;
  const others = d.other_companies.filter(x => x.is_active);
  const { slot, close } = openModal(`
    <div class="modal-title">🗑 Удаление компании «${esc(c.name)}»</div>
    <div class="info-callout" style="margin-bottom:10px">Чеков: <b>${d.receipts}</b> ·
      сотрудников: <b>${d.team.length}</b>. Что сделать с данными компании?</div>
    ${others.length ? `
    <label style="display:block;margin:8px 0"><input type="radio" name="dl-mode" value="move" checked style="width:auto">
      <b>Перенести в другую компанию</b> — чеки переедут целиком (флаг 1С сбросится)</label>
    <label style="display:flex;gap:8px;align-items:center;margin:0 0 10px 26px">
      <select id="dl-target" style="flex:1;min-width:0">${others.map(x =>
        `<option value="${x.id}">${esc(x.name)}</option>`).join('')}</select></label>
    <label style="display:block;margin:6px 0"><input type="checkbox" id="dl-users" checked style="width:auto">
      сотрудники тоже перейдут в выбранную компанию</label>
    <label style="display:block;margin:8px 0"><input type="radio" name="dl-mode" value="wipe" style="width:auto">
      <b>Удалить чеки безвозвратно</b> — все ${d.receipts} чеков будут стёрты, сотрудники открепятся</label>` : `
    <label style="display:block;margin:8px 0"><input type="radio" name="dl-mode" value="wipe" checked style="width:auto">
      <b>Удалить чеки безвозвратно</b> — все ${d.receipts} чеков будут стёрты, сотрудники открепятся
      (других активных компаний нет — переносить некуда)</label>`}
    <div class="modal-actions" style="justify-content:space-between">
      <button class="btn" data-close>Отмена</button>
      <button class="btn btn-bad" id="dl-go">Удалить компанию</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = close;
  slot.querySelector('#dl-go').onclick = async () => {
    const mode = slot.querySelector('input[name="dl-mode"]:checked').value;
    if (mode === 'wipe' && !confirm(`Удалить «${c.name}» и ${d.receipts} чеков БЕЗВОЗВРАТНО?`)) return;
    try {
      const payload = { mode };
      if (mode === 'move') {
        payload.target_company_id = slot.querySelector('#dl-target').value;
        payload.move_users = slot.querySelector('#dl-users').checked;
      } else {
        payload.move_users = true;
      }
      const r = await api.post(`/api/v1/companies/${c.id}/delete`, payload);
      $('#modal-root').classList.add('hidden');
      toast(r.message, 'ok', '🗑');
      if (state.companyFilter === c.id) { state.companyFilter = 'all'; try { localStorage.setItem('ymaster-company', 'all'); } catch (e) {} }
      route(true);
    } catch (e) { toast(e.message, 'err'); }
  };
}

// --- Перемещение чеков между компаниями (админ) -----------------------------
function openMoveDialog() {
  const ids = [...state.receiptsSelected];
  if (!ids.length) return toast('Отметьте чеки галочками — перемещу выбранные', 'info', '🏢');
  const active = state.companies.filter(c => c.is_active);
  const { slot } = openModal(`
    <div class="modal-title">🏢 Переместить чеки в компанию</div>
    <p class="form-hint" style="margin-bottom:10px">Выбрано чеков: <b>${ids.length}</b>.
      Чек переезжает целиком: реквизиты, позиции, статус проверки. Флаг «выгружен в 1С»
      сбрасывается — выгрузку новой компании контролирует её бухгалтер.</p>
    <div class="form-grid">
      <label class="field full"><span>Компания назначения</span>
        <select id="mv-company">${active.map(c =>
          `<option value="${c.id}" ${state.companyFilter === c.id ? 'selected' : ''}>${esc(compName(c))}</option>`).join('')}</select></label>
      <label class="field full"><span>Назначить подотчётное лицо (необязательно)</span>
        <input id="mv-assignee" placeholder="Иванов И.И. — оставить как есть, если пусто"></label>
    </div>
    <div class="modal-actions">
      <button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" id="mv-save">Переместить</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
  slot.querySelector('#mv-save').onclick = async () => {
    try {
      const r = await api.post('/api/v1/receipts/move', {
        receipt_ids: ids,
        company_id: slot.querySelector('#mv-company').value,
        ...(slot.querySelector('#mv-assignee').value.trim()
            ? { assignee: slot.querySelector('#mv-assignee').value.trim() } : {}),
      });
      $('#modal-root').classList.add('hidden');
      state.receiptsSelected.clear();
      toast(r.message, 'ok', '🏢');
      route(true);
    } catch (e) { toast(e.message, 'err'); }
  };
}

function bindShell() {
  $('#btn-logout').onclick = showLogoutDialog;   // v1.21.0: сначала подтверждение
  $('#btn-manual').onclick = openManual;   // v1.21.0: инструкция по приложению
  // v1.18.0: меню профиля закрывается кликом мимо и по Esc (один обработчик)
  if (!window.__ymasterPersonaBound) {
    window.__ymasterPersonaBound = true;
    document.addEventListener('click', (e) => {
      const m = document.getElementById('persona-menu');
      if (m && !m.classList.contains('hidden') &&
          !e.target.closest('#persona-menu') && !e.target.closest('#user-chip')) {
        closePersonaMenu();
      }
    });
    document.addEventListener('keydown', (e) => {
      if (e.key === 'Escape') closePersonaMenu();
    });
  }
  // v1.10.0: меню можно закрыть ВСЕГДА — гамбургер, фон, ✕, Esc, свайп влево
  const _sbMobile = () => window.matchMedia('(max-width: 860px)').matches;
  const closeSidebar = () => {
    $('#sidebar').classList.remove('open');
    document.body.classList.remove('sb-open');
    $('#btn-sidebar').setAttribute('aria-expanded', 'false');   // v1.10.1: a11y
    if (_sbMobile()) $('#sidebar').setAttribute('aria-hidden', 'true');   // v1.56.0: a11y
  };
  $('#btn-sidebar').onclick = () => {
    const opened = $('#sidebar').classList.toggle('open');
    document.body.classList.toggle('sb-open', opened);
    $('#btn-sidebar').setAttribute('aria-expanded', String(opened)); // v1.10.1: a11y
    $('#sidebar').setAttribute('aria-hidden', String(!opened));      // v1.56.0: a11y
  };
  // v1.56.0: на десктопе (>860px) меню постоянно видно — служебные
  // атрибуты и состояние «открыто» сбрасываем (в т.ч. при первом старте);
  // проверки на методы — чтобы код не падал в DOM-песочницах/заглушках
  const _sbSyncDesktop = () => {
    if (_sbMobile()) return;
    const sb = $('#sidebar'), bt = $('#btn-sidebar');
    if (!sb || !bt) return;
    if (typeof sb.removeAttribute === 'function') sb.removeAttribute('aria-hidden');
    if (sb.classList) sb.classList.remove('open');
    if (document.body.classList) document.body.classList.remove('sb-open');
    if (typeof bt.setAttribute === 'function') bt.setAttribute('aria-expanded', 'false');
  };
  window.addEventListener('resize', _sbSyncDesktop);
  _sbSyncDesktop();
  $('#sidebar-backdrop').onclick = closeSidebar;
  $('#sb-close').onclick = closeSidebar;
  // v1.10.2: делегирование на самом меню — тап по пункту выбирает раздел ВСЕГДА,
  // даже если нативное поведение ссылки чем-то отменено. Переход выполняем сами:
  // клик по текущему разделу просто закрывает меню и перерисовывает вид.
  $('#sidebar').addEventListener('click', (e) => {
    const a = e.target.closest('a.nav-item');
    if (!a) return;
    e.preventDefault();
    const target = a.getAttribute('href');
    closeSidebar();
    if (location.hash === target) route();   // тот же раздел — перерисовать и закрыть
    else location.hash = target;             // hashchange вызовет route()
  });
  document.addEventListener('keydown', (e) => {
    if (e.key === 'Escape') closeSidebar();
  });
  // свайп влево по сайдбару закрывает (телефон/планшет)
  let _sbTouchX = null;
  $('#sidebar').addEventListener('touchstart', (e) => {
    _sbTouchX = e.touches[0].clientX;
  }, { passive: true });
  $('#sidebar').addEventListener('touchend', (e) => {
    if (_sbTouchX != null && _sbTouchX - e.changedTouches[0].clientX > 60) closeSidebar();
    _sbTouchX = null;
  }, { passive: true });
  window.addEventListener('hashchange', () => {
    if (location.hash.startsWith('#/register')) return;
    // v1.32.0/v1.35.0: служебные ссылки обрабатываются при загрузке страницы
    if (location.hash.startsWith('#/pool-magic/')
        || location.hash.startsWith('#/pool-verify/')
        || location.hash.match(/^#\/r\//)) return;
    closeSidebar();                       // v1.10.1: перешли в другой раздел — меню закрыто
    // v1.31.0: гость открыл «Сдать чек» с экрана входа — показываем без shell
    if (location.hash.startsWith('#/public') && !getToken()) { showPublicScreen(); return; }
    // v1.32.0: кабинет пула доступен и без входа в программу
    if (location.hash.startsWith('#/my') && !getToken()) { showPoolScreen(); return; }
    route();
  });
}

// ==========================================================================
//  ЭКРАН: Дашборд
// ==========================================================================
async function viewDashboard(container) {
  // v1.8.0: сначала данные — если ничего не изменилось, DOM не трогаем
  // (устраняет «мерцание сайта» при WS-обновлениях и возврате на вкладку)
  const cq = companyIdParam() ? '&company_id=' + companyIdParam() : '';   // v1.11.0
  const [dashStats, dashFeed] = await Promise.all([
    api.get('/api/v1/dashboard/stats?days=14' + cq),
    api.get('/api/v1/dashboard/recent?limit=12').catch(() => []),
  ]);
  const dashSig = JSON.stringify([dashStats, dashFeed]);
  if (viewDashboard._sig === dashSig && container.children.length) return;
  viewDashboard._sig = dashSig;

  container.innerHTML = `
    <div class="dash-toolbar">
      <button class="btn btn-sm" id="btn-focus" title="Остаться только на задачах, требующих внимания">🎯 Умный фокус</button>
      <span class="form-hint">затемняет всё, кроме задач, требующих внимания · выход — Esc</span>
    </div>
    <div class="grid kpi-grid">
      ${kpiCard('kpi-total', 'Чеков в системе', 'всё время')}
      ${kpiCard('kpi-sum', 'Общая сумма', 'все чеки')}
      ${kpiCard('kpi-fns', 'Проверено ФНС', 'действительных')}
      ${kpiCard('kpi-export', 'Выгружено в 1С', 'ждут выгрузки')}
      ${isAccountant() ? `
      ${kpiCard('kpi-attention', '⏳ Требуют внимания', 'не обработаны > 3 дней').replace('class="glass kpi"', 'class="glass kpi ym-focus-target"')}
      ${kpiCard('kpi-notified', '🔔 Уведомления', 'от сотрудников')}
      ${kpiCard('kpi-vat', 'НДС за месяц', 'сумма к учёту')}` : ''}
    </div>
    <div class="grid cards-2" style="margin-top:16px">
      <div class="glass card">
        <div class="card-title">Чеки за 14 дней <span class="spacer"></span>
          <span class="form-hint">наведите на столбец</span></div>
        <div id="chart-daily"><div class="skeleton" style="height:240px"></div></div>
      </div>
      <div class="glass card">
        <div class="card-title">Проверка в ФНС</div>
        <div id="chart-fns"><div class="skeleton" style="height:190px"></div></div>
      </div>
    </div>
    <div class="grid cards-2-even" style="margin-top:16px">
      <div class="glass card">
        <div class="card-title">Источники чеков</div>
        <div id="chart-src"><div class="skeleton" style="height:150px"></div></div>
      </div>
      <div class="glass card">
        <div class="card-title">Лента событий</div>
        <div id="dash-feed"><div class="skeleton" style="height:150px"></div></div>
      </div>
    </div>
    ${isAccountant() ? `
    <div class="glass card ym-focus-target" id="card-assignee" style="margin-top:16px">
      <div class="card-title">👥 По подотчётным лицам <span class="spacer"></span>
        <span class="form-hint">кто сколько принёс (все чеки)</span>
        <button class="btn btn-sm" id="btn-statement" style="margin-left:10px">📋 Ведомость за месяц</button></div>
      <div id="dash-assignee"><div class="skeleton" style="height:80px"></div></div>
    </div>` : ''}
    ${isAdmin() && !companyIdParam() && dashStats.by_company && dashStats.by_company.length ? `
    <div class="glass card" style="margin-top:16px">
      <div class="card-title">🏢 По компаниям <span class="spacer"></span>
        <span class="form-hint">чеки и суммы каждой компании-клиента</span></div>
      <div class="table-wrap"><table class="data"><thead><tr>
        <th>Компания</th><th>Чеков</th><th>Сумма, ₽</th><th></th></tr></thead><tbody>
        ${dashStats.by_company.map(c => `<tr>
          <td title="${esc(c.name)}">${esc(compName(c))}</td><td>${fmtInt(c.count)}</td><td>${fmtSum(c.sum)}</td>
          <td><button class="btn btn-sm c-open" data-id="${c.id}">открыть пространство</button></td></tr>`).join('')}
      </tbody></table></div>
    </div>` : ''}
    <div id="demo-zone"></div>`;

  const stats = dashStats;
  bindSmartFocus();
  animateNumber($('#kpi-total .kpi-value'), stats.total);
  animateNumber($('#kpi-sum .kpi-value'), stats.total_sum, fmtSum);
  if (isAccountant()) {
    animateNumber($('#kpi-attention .kpi-value'), stats.attention_count || 0);
    animateNumber($('#kpi-notified .kpi-value'), stats.notified_count || 0);
    animateNumber($('#kpi-vat .kpi-value'), stats.vat_month || 0, fmtSum);
    const bs = $('#btn-statement');
    if (bs) bs.onclick = () => openStatementModal();
    const az = $('#dash-assignee');
    if (az) {
      // v1.20.0: цветовая карта отчёта — тепловая карта по срокам
      const heat = (a) => a.late > 0
        ? `<span class="heat-chip heat-late">🔴 просрочка: ${a.late}</span>`
        : (a.soon > 0
          ? `<span class="heat-chip heat-soon">🟠 близко к сроку: ${a.soon}</span>`
          : '<span class="heat-chip heat-ok">🟢 в срок</span>');
      az.innerHTML = (stats.by_assignee || []).length
        ? `<table class="data heat-table" style="min-width:0"><thead><tr><th>Сотрудник</th><th>Чеков</th><th>Сумма</th><th>Срок отчёта</th></tr></thead><tbody>
           ${(stats.by_assignee || []).map(a => `<tr><td>${esc(a.name)}</td><td class="tnum">${a.count}</td><td class="cell-sum tnum">${fmtSum(a.sum)}</td><td>${heat(a)}</td></tr>`).join('')}
           </tbody></table>
           <p class="form-hint heat-legend">🟢 в срок · 🟠 до конца срока авансового отчёта осталось меньше 40% времени · 🔴 просрочка (п. 6.3 Указания ЦБ 3210-У)</p>`
        : '<p class="form-hint">Пока нет чеков с назначенным сотрудником</p>';
    }
  }
  // v1.6.0: тренд «неделя к неделе» из daily за 14 дней
  const dd = stats.daily || [];
  if (dd.length >= 14) {
    const last7 = dd.slice(-7).reduce((a, x) => a + (x.count || 0), 0);
    const prev7 = dd.slice(0, 7).reduce((a, x) => a + (x.count || 0), 0);
    const sub = $('#kpi-total .kpi-sub');
    if (sub) {
      if (last7 + prev7 === 0) sub.textContent = 'за 7 дней: 0';
      else {
        const pct = prev7 ? Math.round((last7 - prev7) / prev7 * 100) : 100;
        sub.innerHTML = `за 7 дней: ${last7} · <span class="${pct >= 0 ? 'trend-up' : 'trend-down'}">${pct >= 0 ? '▲' : '▼'} ${Math.abs(pct)}%</span> к предыдущей неделе`;
      }
    }
  }

  const validCount = (stats.by_fns.valid || 0);
  animateNumber($('#kpi-fns .kpi-value'), validCount);
  $('#kpi-fns .kpi-sub').textContent =
    `из ${Object.values(stats.by_fns).reduce((a, b) => a + b, 0)} проверок · ${stats.duplicates_blocked} дублей отсеяно`;
  animateNumber($('#kpi-export .kpi-value'), stats.exported);
  $('#kpi-export .kpi-sub').textContent = `ожидают выгрузки: ${stats.pending_export}`;

  barChart($('#chart-daily'), stats.daily);
  donutChart($('#chart-fns'), [
    { label: 'Действителен', value: stats.by_fns.valid || 0, color: '#34d399' },
    { label: 'Не найден', value: stats.by_fns.not_found || 0, color: '#fbbf24' },
    { label: 'Недействителен', value: stats.by_fns.invalid || 0, color: '#f87171' },
    { label: 'Не проверен', value: stats.by_fns.unknown || 0, color: '#6b7494' },
  ], { centerTitle: fmtInt(stats.total), centerSub: 'всего чеков' });

  const srcLabels = { camera: 'Камера (PWA)', image: 'Изображение', web: 'Веб-вставка', manual: 'Вручную', api: 'API', mobile: 'Мобильный' };
  donutChart($('#chart-src'), Object.entries(stats.by_source).map(([k, v]) => ({
    label: srcLabels[k] || k, value: v,
  })), { size: 150, centerTitle: fmtInt(stats.total), centerSub: 'чеков' });

  if (isAccountant()) {
    const feed = dashFeed;
    const feedEl = $('#dash-feed');
    feedEl.innerHTML = !feed.length ? emptyState('🔔', 'События появятся здесь')
      : feed.map(f => `
        <div class="feed-item"><span class="feed-time">${fmtDate(f.created_at)}</span>
        <span class="feed-text">${esc(feedActionText(f))}</span></div>`).join('');
  } else {
    $('#dash-feed').innerHTML = emptyState('🔑', 'Лента событий — для бухгалтера и администратора');
  }

  if (stats.total === 0 && isAdmin()) {
    $('#demo-zone').innerHTML = `
      <div class="glass card" style="margin-top:16px;text-align:center">
        <h3 style="margin-bottom:8px">Пустая база — посмотрите систему в действии</h3>
        <p style="color:var(--text-dim);margin-bottom:14px">Загрузите демонстрационные чеки за 14 дней
        (реальные ФН не используются), затем удалите их или начните сканировать свои.</p>
        <button class="btn btn-primary" id="btn-demo">✨ Загрузить демо-чеки</button>
      </div>`;
    $('#btn-demo').onclick = async () => {
      const r = await api.post('/api/v1/dashboard/demo-data?count=32', {});
      toast(r.message, r.ok ? 'ok' : 'warn');
      route(true);
      refreshBadges();
    };
  }
}

// ==========================================================================
// v1.21.0: СВОРАЧИВАЕМЫЕ БЛОКИ НАСТРОЕК — заголовок блока виден всегда,
// сворачивается содержимое; состояние запоминается на устройстве.
// ==========================================================================
function foldKey(card) {
  const title = card.querySelector('.card-title');
  return 'f' + ((title ? title.textContent : '?').trim().replace(/\s+/g, ' ').slice(0, 48));
}
function foldState() {
  try { return JSON.parse(localStorage.getItem('ymaster-fold') || '{}'); }
  catch (e) { return {}; }
}
function makeSettingsCollapsible(container) {
  const st = foldState();
  container.querySelectorAll('.settings-grid > .glass.card, .settings-grid .glass.card').forEach(card => {
    const title = card.querySelector('.card-title');
    if (!title || card.classList.contains('foldable')) return;
    card.classList.add('foldable');
    // тело = всё, кроме заголовка
    const body = document.createElement('div');
    body.className = 'fold-body';
    while (title.nextSibling) body.appendChild(title.nextSibling);
    card.appendChild(body);
    // «стрелка»
    const ch = document.createElement('span');
    ch.className = 'fold-chevron';
    ch.textContent = '▼';
    ch.setAttribute('aria-hidden', 'true');
    title.appendChild(ch);
    // применить сохранённое состояние
    const key = foldKey(card);
    if (st[key]) card.classList.add('collapsed');
    const apply = () => {
      const collapsed = card.classList.toggle('collapsed');
      const s = foldState();
      s[key] = collapsed;
      try { localStorage.setItem('ymaster-fold', JSON.stringify(s)); } catch (e) {}
    };
    title.setAttribute('role', 'button');
    title.setAttribute('tabindex', '0');
    title.setAttribute('aria-expanded', String(!card.classList.contains('collapsed')));
    title.onclick = apply;
    title.onkeydown = (e) => {
      if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); apply(); }
    };
  });
}

// ==========================================================================
// v1.20.0: ОФОРМЛЕНИЕ — акцентный цвет, плотность, «Спокойный час»
// (ТЗ «Ямастер» п.6: пользователь выбирает акцент и плотность; хранится
// локально, применяется до отрисовки — без вспышки)
// ==========================================================================
const QUIET_KEY = 'ymaster-quiet-until';

const quietActive = () => {
  try { return Date.now() < (parseInt(localStorage.getItem(QUIET_KEY), 10) || 0); }
  catch (e) { return false; }
};

function bindAppearance() {
  // v1.25.0: акцент и плотность удалены — стиль фиксированный (светлый «Ямастер»);
  // остался только «Спокойный час» (управление уведомлениями, не оформление)
  const q = $('#btn-quiet');
  if (q && !q.dataset.bound) {
    q.dataset.bound = '1';
    q.onclick = () => {
      if (quietActive()) {
        try { localStorage.removeItem(QUIET_KEY); } catch (e) {}
        toast('Спокойный час отключён — уведомления снова показываются', 'info', 'Уведомления');
      } else {
        const mins = parseInt(($('#quiet-dur') || {}).value || '60', 10);
        try { localStorage.setItem(QUIET_KEY, String(Date.now() + mins * 60000)); } catch (e) {}
        toast(`Спокойный час включён до ${new Date(Date.now() + mins * 60000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`, 'info', 'Уведомления');
      }
      renderAppearanceState();
    };
  }
  renderAppearanceState();
}

function renderAppearanceState() {
  // v1.25.0: только статус «Спокойного часа»
  const q = $('#btn-quiet'), qs = $('#quiet-status');
  if (q) {
    if (quietActive()) {
      q.textContent = '🔕 Выключить';
      q.classList.add('btn-accent');
      const until = parseInt(localStorage.getItem(QUIET_KEY), 10);
      if (qs) qs.textContent = `Уведомления скрыты до ${new Date(until).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`;
    } else {
      q.textContent = '🔕 Включить';
      q.classList.remove('btn-accent');
      if (qs) qs.textContent = '';
    }
  }
}

// ==========================================================================
// v1.21.0: ИНСТРУКЦИЯ — всплывающее окно «классическая инструкция».
// Содержание хранится НА СЕРВЕРЕ (/api/v1/manual), версия = версия
// приложения: обновляется вместе с программой. Администратор может
// посмотреть инструкцию любой роли (вкладки).
// ==========================================================================
async function openManual() {
  let data;
  try {
    data = await api.get('/api/v1/manual');
  } catch (e) {
    toast(e.message || 'Инструкция недоступна', 'err', '📖 Инструкция');
    return;
  }
  const { slot, close } = openModal(
    `<div class="manual-shell" id="manual-shell">
       <button class="btn btn-sm" id="manual-close" style="float:right">✕ Закрыть</button>
       <div class="card-title font-accent" style="margin-bottom:2px">📖 Инструкция</div>
       <div class="manual-meta">Ямастер Чек · актуально для версии
         <b>v${esc(data.version)}</b> · ${esc(data.vendor || '')}</div>
       ${data.show_tabs ? `<div class="manual-tabs" id="manual-tabs">
         <button class="manual-tab" data-aud="admin">Администратор</button>
         <button class="manual-tab" data-aud="accountant">Бухгалтер</button>
         <button class="manual-tab" data-aud="user">Сотрудник</button>
       </div>` : ''}
       <div id="manual-body"></div>
       <div class="manual-foot">© ООО «Ямастер» — разработка и идея · ymaster.ru ·
         info@ymaster.ru. Инструкция обновляется вместе с программой.</div>
     </div>`, { onClose: null });
  $('#manual-close', slot).onclick = close;
  const body = $('#manual-body', slot);
  const myRole = data.your_role || 'user';

  const render = (aud) => {
    const list = (data.sections || [])
      .filter(s => aud === 'all' || s.audience === 'all' || s.audience === aud);
    let n = 0;
    body.innerHTML = list.map(s => {
      n += 1;
      return `<div class="manual-sec">
        <h3><span class="manual-num">${n}</span>${s.icon || ''} ${esc(s.title)}</h3>
        ${s.image ? `<img src="${esc(s.image)}" alt="" loading="lazy">` : ''}
        ${s.html || ''}
      </div>`;
    }).join('');
  };

  if (data.show_tabs) {
    const tabs = $('#manual-tabs', slot);
    tabs.querySelectorAll('.manual-tab').forEach(b => {
      b.onclick = () => {
        tabs.querySelectorAll('.manual-tab').forEach(x => x.classList.remove('active'));
        b.classList.add('active');
        render(b.dataset.aud);
        $('#manual-shell', slot).scrollTop = 0;
      };
    });
    (tabs.querySelector(`[data-aud="${myRole}"]`) || tabs.firstElementChild).classList.add('active');
    render((tabs.querySelector('.manual-tab.active') || { dataset: { aud: 'admin' } }).dataset.aud);
  } else {
    render('all');
  }
}

// ==========================================================================
// v1.21.0: ДИАЛОГ ВЫХОДА — предлагаем «Остаться» (по умолчанию): случайный
// Enter не завершает сеанс. Корпоративный стиль, Esc = остаться.
// ==========================================================================
function showLogoutDialog() {
  const { slot, close } = openModal(
    `<div class="modal-title">👋 Выйти из аккаунта?</div>
     <p class="modal-text">Вы всегда сможете вернуться — данные и настройки
     сохранятся на сервере.</p>
     <div class="modal-actions">
       <button class="btn btn-primary" id="lo-stay">Остаться</button>
       <button class="btn btn-danger" id="lo-exit">Выйти</button>
     </div>`);
  const stay = $('#lo-stay', slot);
  const exit = $('#lo-exit', slot);
  stay.focus();                       // Enter = «Остаться»
  stay.onclick = close;
  exit.onclick = () => { close(); logout(); };
}

// v1.20.0: «Умный фокус» — остаётся только текущая задача, остальное затемнено
function bindSmartFocus() {
  const btn = $('#btn-focus');
  if (!btn || btn.dataset.bound) return;
  btn.dataset.bound = '1';
  btn.onclick = () => setSmartFocus(!document.body.classList.contains('ym-focus'));
}
function setSmartFocus(on) {
  document.body.classList.toggle('ym-focus', on);
  const btn = $('#btn-focus');
  if (btn) btn.classList.toggle('btn-accent', on);
}
document.addEventListener('keydown', (e) => {
  if (e.key === 'Escape') setSmartFocus(false);
});

function kpiCard(id, label, sub) {
  return `<div class="glass kpi" id="${id}">
    <div class="kpi-label">${label}</div>
    <div class="kpi-value">0</div>
    <div class="kpi-sub">${sub}</div></div>`;
}
function emptyState(ico, text) {
  // v1.20.0: иллюстрация пустого состояния — «комиксный» документ с
  // галочкой и печатью (ТЗ «Ямастер» п.5.1/п.9.4), фирменные цвета
  return `<div class="empty-state">
    <span class="empty-illo" role="img" aria-label="${esc(ico)}">
      <svg width="132" height="86" viewBox="0 0 132 86" fill="none" xmlns="http://www.w3.org/2000/svg">
        <rect x="30" y="6" width="62" height="74" rx="8" fill="#fff" stroke="#d8d3ea" stroke-width="2"/>
        <rect x="40" y="20" width="42" height="5" rx="2.5" fill="#4B0082" opacity=".45"/>
        <rect x="40" y="31" width="34" height="5" rx="2.5" fill="#4B0082" opacity=".25"/>
        <rect x="40" y="42" width="38" height="5" rx="2.5" fill="#4B0082" opacity=".25"/>
        <rect x="40" y="53" width="26" height="5" rx="2.5" fill="#4B0082" opacity=".25"/>
        <circle cx="92" cy="60" r="17" fill="#FF7A00"/>
        <path d="M84.5 60.5l5.5 5.5 10-11" stroke="#fff" stroke-width="4" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
        <circle cx="38" cy="14" r="6" fill="#2E8B57"/>
        <path d="M35.4 14l1.8 1.8 3.4-3.6" stroke="#fff" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round" fill="none"/>
      </svg>
    </span>
    ${text}</div>`;
}
function feedActionText(f) {
  // v1.19.0: сервер присылает готовую человеческую формулировку
  if (f.human) return f.human + (f.comment ? ` «${f.comment}»` : '');
  const map = {
    receipt_created: 'отсканирован новый чек', receipt_duplicate: 'отсеян дубликат чека',
    login: 'вход в систему', login_failed: 'неудачная попытка входа',
    receipts_exported: 'выгрузка чеков в 1С', verify_queued: 'запущена проверка в ФНС',
    receipt_deleted: 'удалён чек', demo_data_loaded: 'загружены демо-данные',
    user_created: 'создан пользователь', mapping_updated: 'обновлён маппинг',
    fns_settings_updated: 'изменены настройки ФНС', onec_pull: '1С забрала чеки',
    onec_ack: '1С подтвердила загрузку', invite_created: 'создано приглашение',
    register: 'зарегистрирован пользователь', admin_transferred: 'передача прав администратора',
    receipts_assigned: 'назначен сотрудник на чеки', receipts_exported_csv: 'выгружен CSV',
    app_settings_updated: 'изменены общие настройки',
  };
  const d = (() => { try { return JSON.parse(f.details || '{}'); } catch { return {}; } })();
  const base = map[f.action] || f.action;
  const who = f.username ? `<b>${esc(f.username)}</b>` : 'система';
  if (f.action === 'receipt_created') {
    return `${who}: чек на <b>${fmtSum(d.sum || 0)}</b> (ФД ${esc(d.fd || '—')})`;
  }
  if (f.action === 'receipts_exported' || f.action === 'verify_queued') {
    return `${who}: ${base.toLowerCase()} — ${d.count || 0} шт.`;
  }
  return `${who}: ${base}`;
}

// ==========================================================================
//  ЭКРАН: Сканирование
// ==========================================================================
async function viewScan(container) {
  const queueSize = offlineQueue.size();
  // v1.11.0: подсказка — в какую компанию попадут новые чеки (админ меняет в шапке)
  container.innerHTML = `
    <div class="scan-layout">
      <div class="glass camera-card">
        <div class="card-title">Камера устройства <span class="spacer"></span>
          <span class="chip ${state.wsOk ? 'verified' : 'unknown'}"><span class="dot"></span>${state.wsOk ? 'онлайн' : 'офлайн'}</span>
        </div>
        <div class="camera-viewport">
          <video id="scan-video" playsinline muted></video>
          <div class="scan-frame" id="scan-frame" style="display:none"><span class="corner"></span></div>
          <div class="scan-line" id="scan-line" style="display:none"></div>
          <div class="camera-overlay" id="camera-overlay">
            <span class="big-ico">📷</span>
            <div>Наведите камеру на QR-код чека.<br>Система распознаёт код автоматически, с антисбливанием.</div>
            <button class="btn btn-primary" id="btn-camera-start">▶ Включить камеру</button>
            <span class="form-hint">или используйте способы ниже</span>
          </div>
        </div>
        <div class="scan-actions">
          <button class="btn" id="btn-upload">🖼 Загрузить фото чека</button>
          <button class="btn" id="btn-paste">📋 Вставить QR-текст</button>
          <button class="btn" id="btn-manual">⌨ Ввести вручную</button>
          <input type="file" id="file-input" accept="image/*" multiple class="hidden">
        </div>
        <div class="dropzone" id="dropzone">
          <span class="big-ico">⬇️</span>
          Перетащите сюда фотографии чеков — можно несколько сразу
        </div>
        <div class="torch-note">Совет: чеки с экрана телефона тоже распознаются. Держите QR в рамке при хорошем свете.
        После скана чек автоматически уходит на проверку в ФНС.</div>
        ${isAdmin() ? `<div class="torch-note" id="scan-space-hint">🏢</div>` : ''}
      </div>

      <div>
        ${queueSize > 0 ? `
        <div class="glass card" style="margin-bottom:16px">
          <div class="card-title">⚡ Офлайн-очередь: ${queueSize}</div>
          <p style="color:var(--text-dim);font-size:13px;margin-bottom:12px">
            Эти сканы были сделаны без связи с сервером и ждут синхронизации.</p>
          <button class="btn btn-primary btn-sm" id="btn-flush">Синхронизировать сейчас</button>
        </div>` : ''}
        <div class="glass card">
          <div class="card-title">Последние сканы</div>
          <div id="scan-recents"></div>
        </div>
        <div class="glass card" style="margin-top:16px">
          <div class="card-title">Как это работает</div>
          <div class="steps">
            <div class="step"><span class="step-num"></span><div>Отсканируйте QR-код камерой, загрузите фото или введите реквизиты вручную.</div></div>
            <div class="step"><span class="step-num"></span><div>Система разбирает реквизиты 54-ФЗ: <b>ФН, ФД, ФП</b>, сумму и дату, отсекает дубликаты по ФН+ФД+ФП.</div></div>
            <div class="step"><span class="step-num"></span><div>Чек автоматически проверяется в API ФНС.</div></div>
            <div class="step"><span class="step-num"></span><div>Бухгалтер назначает сотрудника и выгружает чеки в 1С — без ручного ввода.</div></div>
          </div>
        </div>
      </div>
    </div>`;

  renderRecents();

  const _hint = $('#scan-space-hint');
  if (_hint) {
    const c = companyIdParam() ? state.companies.find(x => x.id === companyIdParam()) : null;
    _hint.innerHTML = c
      ? `Новые чеки попадут в компанию: <b>${esc(c.name)}</b> (меняется селектором в шапке)`
      : 'Новые чеки попадут без компании (платформенные) — выберите компанию селектором в шапке';
  }
  $('#btn-camera-start').onclick = async () => {
    const video = $('#scan-video');
    try {
      state.camera = new CameraScanner(video, (text) => handleScannedText(text, 'camera'));
      await state.camera.start();
      $('#camera-overlay').classList.add('hidden');
      $('#scan-frame').style.display = '';
      $('#scan-line').style.display = '';
      const stopBtn = document.createElement('button');
      stopBtn.className = 'btn btn-sm';
      stopBtn.textContent = '⏹ Остановить камеру';
      stopBtn.onclick = () => { state.camera.stop(); state.camera = null; stopBtn.remove(); route(true); };
      $('.scan-actions').appendChild(stopBtn);
      toast('Камера включена — наведите на QR-код чека', 'info');
    } catch (e) {
      toast(e.message, 'err', 'Камера недоступна');
    }
  };

  $('#btn-upload').onclick = () => $('#file-input').click();
  $('#file-input').onchange = (e) => handleFiles([...e.target.files]);
  const dz = $('#dropzone');
  dz.onclick = () => $('#file-input').click();
  dz.ondragover = (e) => { e.preventDefault(); dz.classList.add('dragover'); };
  dz.ondragleave = () => dz.classList.remove('dragover');
  dz.ondrop = (e) => {
    e.preventDefault(); dz.classList.remove('dragover');
    handleFiles([...e.dataTransfer.files]);
  };

  $('#btn-paste').onclick = () => pasteDialog();
  $('#btn-manual').onclick = () => manualDialog();
  const flush = $('#btn-flush');
  if (flush) flush.onclick = () => flushOfflineQueue(true);
}

function renderRecents() {
  const el = $('#scan-recents');
  if (!el) return;
  const items = state.recents.slice(0, 8);
  el.innerHTML = items.length ? items.map(r => `
    <div class="recent-scan-item">
      <div class="rs-icon" style="background:${r.duplicate ? 'rgba(251,191,36,.15)' : 'rgba(52,211,153,.15)'}">
        ${r.duplicate ? '⚠️' : '✅'}</div>
      <div class="rs-main">
        <div class="rs-title">${fmtSum(r.sum)} ${r.duplicate ? '· дубликат' : ''}</div>
        <div class="rs-sub">ФН ${r.fn} · ФД ${r.fd} · ФП ${r.fp} · ${fmtDate(r.at)}</div>
      </div>
    </div>`).join('') : emptyState('🧾', 'Пока ничего не отсканировано');
}

function pushRecent(r) {
  state.recents.unshift({
    fn: r.fn, fd: r.fd, fp: r.fp, sum: r.total_sum,
    duplicate: false,
    at: new Date().toISOString(),
  });
  state.recents = state.recents.slice(0, 12);
  localStorage.setItem('ymaster_recents', JSON.stringify(state.recents));
  renderRecents();
}

// ==========================================================================
//  v1.2.0: Диалог редактирования чека (бухгалтер/админ — все поля и позиции)
// ==========================================================================
function _dtLocal(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (isNaN(d)) return '';
  const p = (n) => String(n).padStart(2, '0');
  return `${d.getFullYear()}-${p(d.getMonth() + 1)}-${p(d.getDate())}T${p(d.getHours())}:${p(d.getMinutes())}`;
}

// ==========================================================================
//  v1.57.1: «Полные данные чека» — всё, что вернул proverkacheka.com
//  (receipts.ext_json): место расчётов, касса/смена, налог, свойства
//  заказа, итоги НДС, ФФД и пр. Показывается в карточке чека.
// ==========================================================================
function extBlockHTML(ext) {
  if (!ext || typeof ext !== 'object') return '';
  const rows = [];
  const add = (label, v) => {
    if (v === undefined || v === null || v === '') return;
    rows.push(`<dt>${esc(label)}</dt><dd>${esc(String(v))}</dd>`);
  };
  const rub = (v) => (v === undefined || v === null || v === '') ? null
    : Number(v).toFixed(2) + ' ₽';
  add('Место расчётов', ext.retail_place);
  add('Адрес (из источника)', ext.source_address);
  add('Регион', ext.region);
  add('ККТ (рег. номер)', ext.kkt_reg_id);
  add('ККТ (заводской №)', ext.number_kkt);
  add('Смена', ext.shift_number);
  add('Чек в смене', ext.request_number);
  add('Налогообложение', ext.taxation);
  if (ext.nds0 === 0 || ext.nds0 === '0') add('НДС 0%', 'есть (без НДС)');
  add('Формат ФФД', ext.ffd_version);
  add('Контрольный знак сообщения', ext.message_fiscal_sign);
  add('Код ответа источника', ext.source_code);
  add('Служебная маска источника', ext.redefine_mask);
  add('ОФД', ext.ofd_id);
  add('Тип документа', ext.doc_subtype);
  add('Предоплата', rub(ext.prepaid_sum));
  add('Кредит', rub(ext.credit_sum));
  add('Встречная предоставление', rub(ext.provision_sum));
  add('Получено источником', ext.receive_date);
  add('ID чека в источнике', ext.source_receipt_id);
  add('Создан в источнике', ext.source_date_create);
  if (Array.isArray(ext.nds_totals) && ext.nds_totals.length) {
    const t = ext.nds_totals.map(n =>
      `НДС ${n.nds}% — ${(Number(n.ndsSum || 0) / 100).toFixed(2)} ₽`).join('; ');
    rows.push(`<dt>Итоги НДС</dt><dd>${esc(t)}</dd>`);
  }
  if (Array.isArray(ext.properties) && ext.properties.length) {
    const p = ext.properties.map(x => `${x.name}: ${x.value}`).join('; ');
    rows.push(`<dt>Свойства заказа</dt><dd>${esc(p)}</dd>`);
  }
  if (Array.isArray(ext.items_meta) && ext.items_meta.length) {
    const NAMES = { paymenttype: 'оплата', producttype: 'товар',
                    itemsquantitymeasure: 'мера' };
    const m = ext.items_meta.map(x => {
      const parts = ['paymenttype', 'producttype', 'itemsquantitymeasure']
        .filter(k => x[k] !== undefined && x[k] !== '')
        .map(k => `${NAMES[k]} ${x[k]}`);
      return `№${x.pos}: ${parts.join(', ')}`;
    }).join('; ');
    rows.push(`<dt>Признаки позиций</dt><dd>${esc(m)}</dd>`);
  }
  if (!rows.length) return '';
  return `<details class="manual-check" style="margin-top:12px">
    <summary style="cursor:pointer;font-size:13px;color:var(--text-dim)">🧾 Полные данные чека — из proverkacheka.com</summary>
    <dl class="kv" style="font-size:12.5px;margin:10px 0 4px">${rows.join('')}</dl>
  </details>`;
}

function openEditReceipt(r, onSaved) {
  const isAdminUser = isAdmin();
  const exportedWarn = r.exported && !isAdminUser
    ? '<div class="info-callout" style="margin-bottom:12px">⚠️ Чек уже выгружен в 1С: реквизиты и позиции менять нельзя (только администратор).</div>' : '';
  const { slot, close } = openModal(`
    <div class="modal-title">✏️ Чек ${esc(r.fn)} / ${esc(r.fd)} / ${esc(r.fp)}</div>
    ${exportedWarn}
    <div class="form-grid">
      <label class="field"><span>Дата и время</span>
        <input id="er-date" type="datetime-local" value="${_dtLocal(r.receipt_date)}"
          ${r.exported && !isAdminUser ? 'disabled' : ''}></label>
      <label class="field"><span>Сумма, ₽</span>
        <input id="er-sum" type="number" step="0.01" min="0" value="${r.total_sum}"
          ${r.exported && !isAdminUser ? 'disabled' : ''}></label>
      <label class="field"><span>Личные, ₽ <span class="form-hint">(не для учёта)</span></span>
        <input id="er-personal" type="number" step="0.01" min="0" value="${r.personal_sum || 0}"
          ${r.exported && !isAdminUser ? 'disabled' : ''}>
        <span class="form-hint" id="er-work-hint">к учёту: ${fmtSum((r.total_sum || 0) - (r.personal_sum || 0))}</span></label>
      <label class="field"><span>Тип</span>
        <select id="er-op" ${r.exported && !isAdminUser ? 'disabled' : ''}>
          <option value="1" ${r.operation === 1 ? 'selected' : ''}>Приход</option>
          <option value="2" ${r.operation === 2 ? 'selected' : ''}>Возврат</option>
        </select></label>
      <label class="field"><span>ФН</span>
        <input id="er-fn" value="${esc(r.fn)}" ${r.exported && !isAdminUser ? 'disabled' : ''}></label>
      <label class="field"><span>ФД</span>
        <input id="er-fd" value="${esc(r.fd)}" ${r.exported && !isAdminUser ? 'disabled' : ''}></label>
      <label class="field"><span>ФП</span>
        <input id="er-fp" value="${esc(r.fp)}" ${r.exported && !isAdminUser ? 'disabled' : ''}></label>
    </div>
    <div class="form-grid" style="margin-top:10px">
      <label class="field"><span>Магазин</span>
        <input id="er-shop" value="${esc(r.merchant_name || '')}" placeholder="из данных ФНС"></label>
      <label class="field"><span>ИНН магазина</span>
        <input id="er-inn" value="${esc(r.merchant_inn || '')}"></label>
      <label class="field"><span>Адрес магазина</span>
        <input id="er-addr" value="${esc(r.merchant_address || '')}"></label>
      <label class="field"><span>Кассир</span>
        <input id="er-cashier" value="${esc(r.cashier || '')}"></label>
      <label class="field"><span>Сотрудник (подотчётник)</span>
        <input id="er-assignee" value="${esc(r.assignee || '')}" list="er-names">
        <datalist id="er-names">${[...(viewReceipts._names || [])].map(n => `<option value="${esc(n)}">`).join('')}</datalist></label>
      <label class="field"><span>Статья расходов</span>
        <select id="er-cat-pick" ${r.exported && !isAdminUser ? 'disabled' : ''}>
          <option value="">— не указана —</option>
          ${(viewReceipts._catsTop || []).slice(0, 19).map(c =>
            `<option value="${esc(c.name)}" ${r.category === c.name ? 'selected' : ''}>${esc(c.name)} (${c.count})</option>`).join('')}
          <option value="__custom__">Своя…</option>
        </select>
        <input id="er-category" maxlength="100" style="display:none"
          placeholder="своя статья — до 100 символов" value="${esc(r.category || '')}">
        <span class="form-hint" id="er-cat-hint"></span></label>
      <label class="field"><span>Комментарий</span>
        <input id="er-comment" value="${esc(r.comment || '')}"></label>
    </div>
    <div style="margin-top:14px">
      <b style="font-size:13.5px">Позиции чека (${(r.items || []).length})</b>
      ${r.exported && !isAdminUser ? '' : '<button class="btn btn-sm" id="er-add-item" style="margin-left:8px">+ позиция</button>'}
      <div id="er-items" style="margin-top:8px"></div>
    </div>
    <label style="display:flex;gap:10px;align-items:center;margin-top:12px;cursor:pointer">
      <input type="checkbox" id="er-notified" ${r.notified ? 'checked' : ''} style="width:auto">
      <span>🔔 Уведомление от сотрудника</span></label>
    ${extBlockHTML(r.ext) ||
      `<div class="info-callout" style="margin-top:12px">🧾 Полные данные чека ещё не получены.
        <button class="btn btn-sm" id="er-fetch" style="margin-left:8px">🔄 Получить данные из proverkacheka.com</button>
        <div class="form-hint" id="er-fetch-msg" style="margin-top:6px"></div></div>`}
    ${r.ext ? `<div style="margin-top:8px"><button class="btn btn-sm" id="er-refetch"
        title="Запросить свежие данные из proverkacheka.com; пустые поля заполнятся, ваши правки не затрутся">🔄 Обновить данные из источника</button>
        <span class="form-hint" id="er-fetch-msg" style="margin-left:8px"></span></div>` : ''}
    <details class="manual-check" style="margin-top:12px">
      <summary style="cursor:pointer;font-size:13px;color:var(--text-dim)">🌐 Проверить чек вручную на сторонних сервисах</summary>
      <div class="manual-check-links">
        <a href="https://proverkacheka.com/" target="_blank" rel="noopener">proverkacheka.com</a>
        <a href="https://xn--80aaemb2ac0aikd0g.xn--p1ai/" target="_blank" rel="noopener">проверкачека.рф</a>
        <a href="https://proverka-cheka.ru/" target="_blank" rel="noopener">proverka-cheka.ru</a>
        <a href="https://chek-pek.ru/" target="_blank" rel="noopener">chek-pek.ru</a>
      </div>
      <p class="form-hint" style="margin-top:6px">Реквизиты чека: ФН ${esc(r.fn)} · ФД ${esc(r.fd)} · ФП ${esc(r.fp)} · ${fmtSum(r.total_sum)} — скопируйте их на сайте сервиса.</p>
    </details>
    <div class="modal-actions">
      <button class="btn btn-primary" id="er-save">💾 Сохранить</button>
      <button class="btn" id="er-cancel">Отмена</button>
    </div>`);

  // v1.57.3: «Получить/Обновить данные» прямо в карточке чека — доступно
  // всегда (в списке кнопка 📥 скрыта, когда full_data=1, из-за чего чеки,
  // заполненные до появления расширенных полей, не могли обновиться)
  const _fetchData = async (btn) => {
    if (!btn || btn.disabled) return;
    const msg = slot.querySelector('#er-fetch-msg');
    const old = btn.textContent;
    btn.disabled = true;
    btn.textContent = '⏳ Запрашиваю (пауза 2–7 с)…';
    try {
      // v1.57.4: force — принудительный запрос (иначе старые чеки с
      // full_data=1 получали отказ «данные уже получены» без данных)
      const res = await api.post(`/api/v1/receipts/${r.id}/fetch-details?force=1`);
      if (msg) msg.textContent = res.message || 'Готово';
      toast(res.message || 'Данные получены', 'info', 'Данные чека');
      try { await onSaved(); } catch (e) {}          // свежий список
      const fresh = (viewReceipts._rows || []).find(x => x.id === r.id);
      if (fresh) { close(); openEditReceipt(fresh, onSaved); }   // переоткрыть с данными
    } catch (err) {
      if (msg) msg.textContent = err.message || 'Не удалось получить данные';
      toast(err.message || 'Не удалось получить данные', 'err');
      btn.disabled = false;
      btn.textContent = old;
    }
  };
  const _fb = slot.querySelector('#er-fetch');
  if (_fb) _fb.onclick = () => _fetchData(_fb);
  const _rf = slot.querySelector('#er-refetch');
  if (_rf) _rf.onclick = () => _fetchData(_rf);

  // v1.3.0 (баг-фикс): позиции — единый источник DOM; всё введённое читается
  // при сохранении, удаление/добавление работают без потери набранного.
  const itemsBox = slot.querySelector('#er-items');
  const itemRow = (it = { name: '', quantity: 1, price: 0, total: 0 }) => {
    const row = document.createElement('div');
    row.className = 'item-row';
    row.innerHTML = `
      <input class="it-name" value="${esc(it.name)}" placeholder="наименование позиции">
      <input class="it-qty" type="number" step="0.001" min="0" value="${it.quantity}" title="Количество">
      <input class="it-price" type="number" step="0.01" min="0" value="${it.price}" title="Цена">
      <input class="it-total" type="number" step="0.01" min="0" value="${it.total}" title="Сумма">
      <button class="btn btn-sm btn-bad it-del" type="button" title="Удалить позицию">✕</button>`;
    row.querySelector('.it-del').onclick = () => row.remove();
    return row;
  };
  // v1.28.0: статья расходов — выбор из частых или «Своя…» (≤100, счётчик)
  const catPick = slot.querySelector('#er-cat-pick');
  const catInput = slot.querySelector('#er-category');
  const catHint = slot.querySelector('#er-cat-hint');
  const catCounter = () => {
    const len = [...catInput.value].length;
    catHint.textContent = catPick.value === '__custom__'
      ? `введено ${len} из 100 · осталось ${Math.max(0, 100 - len)}` : '';
  };
  if (catPick.value !== '__custom__' && r.category) catInput.value = '';
  if (r.category && !(viewReceipts._catsTop || []).slice(0, 19).some(c => c.name === r.category)) {
    catPick.value = '__custom__';                 // своя статья из сохранённых
  }
  const catSync = () => {
    const custom = catPick.value === '__custom__';
    catInput.style.display = custom ? '' : 'none';
    if (custom) { catCounter(); } else { catHint.textContent = ''; }
  };
  catPick.onchange = () => {
    catSync();
    if (catPick.value === '__custom__') { catInput.focus(); catCounter(); }
  };
  catInput.addEventListener('input', catCounter);
  catSync();

  // v1.8.0: живой пересчёт «к учёту» при вводе личной суммы
  const personalEl = slot.querySelector('#er-personal'), workHint = slot.querySelector('#er-work-hint');
  if (personalEl && workHint) {
    const recalcWork = () => {
      const sum = parseFloat(slot.querySelector('#er-sum').value) || 0;
      const per = parseFloat(personalEl.value) || 0;
      workHint.textContent = 'к учёту: ' + fmtSum(Math.max(0, sum - per));
    };
    personalEl.addEventListener('input', recalcWork);
    slot.querySelector('#er-sum').addEventListener('input', recalcWork);
  }
  (r.items || []).forEach(it => itemsBox.appendChild(itemRow(it)));
  if (!(r.items || []).length) {
    itemsBox.innerHTML = '<p class="form-hint" id="er-no-items">Позиций нет — получите данные из сервиса или добавьте вручную</p>';
  }
  const addBtn = slot.querySelector('#er-add-item');
  if (addBtn) addBtn.onclick = () => {
    itemsBox.querySelector('#er-no-items')?.remove();
    itemsBox.appendChild(itemRow());
    itemsBox.querySelector('.item-row:last-child .it-name').focus();
  };
  const collectItems = () => [...itemsBox.querySelectorAll('.item-row')]
    .map(row => ({
      name: row.querySelector('.it-name').value.trim(),
      quantity: parseFloat(row.querySelector('.it-qty').value) || 1,
      price: parseFloat(row.querySelector('.it-price').value) || 0,
      total: parseFloat(row.querySelector('.it-total').value) || 0,
    }))
    .filter(it => it.name);

  slot.querySelector('#er-cancel').onclick = close;
  slot.querySelector('#er-save').onclick = async () => {
    const body = {
      merchant_name: slot.querySelector('#er-shop').value.trim(),
      // v1.28.0: статья из списка или своя (до 100 символов)
      category: catPick.value === '__custom__'
        ? catInput.value.trim().slice(0, 100)
        : catPick.value,
      merchant_inn: slot.querySelector('#er-inn').value.trim(),
      merchant_address: slot.querySelector('#er-addr').value.trim(),
      cashier: slot.querySelector('#er-cashier').value.trim(),
      assignee: slot.querySelector('#er-assignee').value.trim(),
      personal_sum: parseFloat(slot.querySelector('#er-personal').value) || 0,
      comment: slot.querySelector('#er-comment').value.trim(),
      notified: slot.querySelector('#er-notified').checked,
    };
    const dateEl = slot.querySelector('#er-date'), sumEl = slot.querySelector('#er-sum');
    const opEl = slot.querySelector('#er-op'), fnEl = slot.querySelector('#er-fn');
    const fdEl = slot.querySelector('#er-fd'), fpEl = slot.querySelector('#er-fp');
    const coreLocked = r.exported && !isAdminUser;
    if (!coreLocked) {
      if (dateEl.value) body.receipt_date = new Date(dateEl.value).toISOString();
      body.total_sum = parseFloat(sumEl.value) || 0;
      body.operation = parseInt(opEl.value);
      body.fn = fnEl.value.trim(); body.fd = fdEl.value.trim(); body.fp = fpEl.value.trim();
      body.items = collectItems();
    }
    try {
      await api.patch('/api/v1/receipts/' + r.id, body);
      toast('Чек обновлён', 'ok');
      close(); onSaved && onSaved();
    } catch (e) { toast(e.message, 'err'); }
  };
}

// --- Уведомление от сотрудника (только свой чек) ---
function openNotifyDialog(r, onSaved) {
  const { slot, close } = openModal(`
    <div class="modal-title">🔔 Уведомить бухгалтерию</div>
    <p class="form-hint" style="margin-bottom:12px">Чек ${esc(r.fn)}/${esc(r.fd)}/${esc(r.fp)} на ${fmtSum(r.total_sum)}.
    Сообщите бухгалтеру о замене, возврате товара или другой особенности этого чека.</p>
    <label style="display:flex;gap:10px;align-items:center;margin-bottom:12px;cursor:pointer">
      <input type="checkbox" id="nt-on" ${r.notified ? 'checked' : ''} style="width:auto">
      <span>Требует внимания бухгалтерии</span></label>
    <label class="field"><span>Комментарий к чеку</span>
      <textarea id="nt-comment" rows="3" maxlength="2000" placeholder="например: товар вернули, чек заменяю позже…">${esc(r.comment || '')}</textarea></label>
    <div class="modal-actions">
      <button class="btn btn-primary" id="nt-send">Отправить</button>
      <button class="btn" id="nt-cancel">Отмена</button>
    </div>`);
  slot.querySelector('#nt-cancel').onclick = close;
  slot.querySelector('#nt-send').onclick = async () => {
    try {
      await api.patch('/api/v1/receipts/' + r.id, {
        notified: slot.querySelector('#nt-on').checked,
        comment: slot.querySelector('#nt-comment').value.trim(),
      });
      toast('Уведомление отправлено бухгалтеру', 'ok');
      close(); onSaved && onSaved();
    } catch (e) { toast(e.message, 'err'); }
  };
}


// ==========================================================================
//  v1.2.0: Блок «Что нового» — показывается один раз на каждую версию
// ==========================================================================
// v1.25.0: темы удалены — единственная светлая задана в CSS (:root),
// переключатель из Настроек убран, «вспышки» неправильной темы нет.

// ==========================================================================
//  v1.5.0: Установка приложения на устройство (PWA: Android/iOS/десктоп)
// ==========================================================================
window.addEventListener('beforeinstallprompt', (e) => {
  e.preventDefault();
  window.__ymInstallPrompt = e;
});

function isIOSLike() {
  return /iphone|ipad|ipod/i.test(navigator.userAgent)
    || (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
}

function isWindows() {
  return /windows/i.test(navigator.userAgent);
}

function showInstallDialog() {
  const iOS = isIOSLike();
  const win = isWindows();
  const standalone = window.matchMedia('(display-mode: standalone)').matches
    || window.navigator.standalone === true;
  if (standalone) {
    toast('Приложение уже установлено на этом устройстве', 'ok', '📲');
    return;
  }
  const { close } = openModal(`
    <div class="modal-title">📲 Установить Ямастер Чек</div>
    ${iOS ? `
      <div class="steps" style="font-size:13.5px">
        <div class="step"><div class="step-num"></div><div>В Safari нажмите кнопку <b>«Поделиться»</b>
          <span style="font-size:16px">⎋</span> (внизу на iPhone / справа сверху на iPad)</div></div>
        <div class="step"><div class="step-num"></div><div>Выберите <b>«На экран "Домой"»</b>
          <span style="font-size:16px">➕</span></div></div>
        <div class="step"><div class="step-num"></div><div>Подтвердите — иконка <b>Ямастер Чек</b> появится
          на домашнем экране, приложение откроется на весь экран.</div></div>
      </div>` : `
      ${win ? `
      <p style="font-size:13.5px;color:var(--text-dim);margin-bottom:10px">Установите <b>Ямастер Чек</b>
      как приложение Windows 11 — работа в своём окне без браузера, иконка в «Пуск» и на панели задач.</p>
      <button class="btn btn-primary btn-block" id="pwa-go">⬇ Установить сейчас</button>
      <p class="form-hint" style="margin-top:10px">Если кнопка не сработала (Microsoft Edge): нажмите
      <b>⊕ / «Установить приложение»</b> в адресной строке, либо меню <b>⋯ → Приложения → Установить
      этот сайт как приложение</b>. Приложение появится в «Пуск» и будет запускаться в своём окне.</p>`
      : `
      <p style="font-size:13.5px;color:var(--text-dim);margin-bottom:10px">Установите приложение
      на устройство — быстрый доступ с иконки, работа на весь экран, офлайн-сканирование.</p>
      <button class="btn btn-primary btn-block" id="pwa-go">⬇ Установить сейчас</button>
      <p class="form-hint" style="margin-top:10px">Если кнопка не сработала: меню браузера →
      «Установить приложение» / «Добавить на главный экран».</p>`}`}
    <div class="modal-actions"><button class="btn" id="pwa-close">Понятно</button></div>`);
  const go = document.getElementById('pwa-go');
  if (go) go.onclick = async () => {
    const p = window.__ymInstallPrompt;
    if (!p) return toast('Браузер сам предложит установку — откройте меню браузера', 'info');
    p.prompt();
    const { outcome } = await p.userChoice;
    if (outcome === 'accepted') toast('Приложение устанавливается…', 'ok');
    close();
  };
  document.getElementById('pwa-close').onclick = close;
}

// ==========================================================================
//  v1.4.0: Обновления из приложения (админ)
// ==========================================================================
function checkUpdatesSilently() {
  // v1.50.0: фоновая проверка при старте — можно из кэша (не грузим GitHub)
  api.get('/api/v1/admin/update/check?cache=1', { retries: 1 }).then(r => {
    if (r.ok === false) {
      // v1.6.0: GitHub недоступен с сервера — не молчим, но и не спамим (1 раз за сессию)
      state.githubUnreachable = true;
      if (!state.ghWarnShown) {
        state.ghWarnShown = true;
        toast('Проверка обновлений не удалась: GitHub недоступен с сервера. Инструкция — Настройки → Обновления', 'warn', '🔄 Обновление');
      }
      return;
    }
    state.githubUnreachable = false;
    if (r.update_available) {
      state.updateAvailable = r;
      toast(`Доступно обновление v${r.remote_version} — откройте Настройки → Обновления`, 'info', '🔄 Обновление');
    }
  }).catch(() => { state.githubUnreachable = true; });
}

// v1.6.0: визуальное состояние карточки «Обновления»
function setUpdState(kind, errText, r) {
  const dot = $('#upd-dot'), txt = $('#upd-status-text'), man = $('#upd-manual');
  if (!dot || !txt) return;
  const cls = { checking: 'upd-dot--wait', latest: 'upd-dot--ok',
                available: 'upd-dot--new', error: 'upd-dot--err' }[kind] || '';
  dot.className = 'upd-dot ' + cls;
  txt.textContent = {
    checking: 'Проверяю доступность обновлений…',
    latest: `У вас последняя версия${r && r.current_version ? ' — v' + r.current_version : ''}`,
    available: r && r.remote_version ? `Доступна новая версия — v${r.remote_version}` : 'Доступна новая версия',
    error: 'GitHub недоступен с сервера',
  }[kind] || '';
  if (man) man.classList.toggle('hidden', kind !== 'error');
  const err = $('#upd-err');
  if (err) { err.textContent = errText || ''; err.classList.toggle('hidden', !errText); }
}

function showUpdateDialog(checkInfo) {
  const r = checkInfo;
  const { slot, close } = openModal(`
    <div class="modal-title">🔄 Обновление системы</div>
    <dl class="kv" style="font-size:13.5px">
      <dt>Текущая версия</dt><dd>v${esc(r.current_version)}</dd>
      <dt>Доступна</dt><dd><b style="color:var(--ok)">v${esc(r.remote_version)}</b></dd>
      <dt>Ветка</dt><dd><code class="inline">${esc(r.branch)}</code></dd>
    </dl>
    <details style="margin:10px 0"><summary style="cursor:pointer;font-size:13px;color:var(--text-dim)">📋 Что изменится в новой версии</summary>
      <pre class="codeblock" style="max-height:220px">${esc(r.changelog_excerpt || 'описание уточняется')}</pre></details>
    <div class="info-callout" style="font-size:12.5px">Обновление безопасно для данных: перед установкой автоматически
    создаётся резервная копия базы; проверяется целостность; при сбое — откат на прежнюю версию.
    Устанавливаются только изменения (не весь проект).</div>
    <div class="info-callout" style="font-size:12.5px;margin-top:10px">✅ Пароль не нужен:
    с v1.17.0 приложение перезапускается само (re-exec) — обновление проходит целиком из этого окна.
    Поле пароля ниже можно оставить пустым.</div>
    <label class="field" style="margin-top:10px"><span>🔑 Пароль сервера (sudo) — обычно не требуется</span>
      <input type="password" id="upd-sudo-pw" autocomplete="off" placeholder="оставьте пустым">
      <span class="form-hint">sudo проверяет пароль служебного пользователя приложения, а не ваш —
      поэтому ваш пароль из терминала тут не работал. Теперь перезапуск идёт без пароля.
      Используется только в момент обновления и нигде не сохраняется.</span></label>
    <div class="modal-actions">
      <button class="btn" id="upd-close">Позже</button>
      <button class="btn btn-primary" id="upd-apply">⬇ Обновить до v${esc(r.remote_version)}</button>
    </div>`);
  slot.querySelector('#upd-close').onclick = close;
  slot.querySelector('#upd-apply').onclick = async (e) => {
    e.target.disabled = true;
    try {
      const pwEl = slot.querySelector('#upd-sudo-pw');
      const pw = pwEl && pwEl.value ? pwEl.value : undefined;
      const resp = await api.post('/api/v1/admin/update/apply',
        pw ? { sudo_password: pw } : undefined);
      close();
      if (!resp.updated) return toast(resp.message, 'ok');
      openUpdateProgress();
    } catch (err) { toast(err.message, 'err'); e.target.disabled = false; }
  };
}

// v1.50.0: пошаговое обновление — фазы, время, килобайты, ОДИН итог.
// Единственный источник правды — /update/status (сервер); UI только отображает.
const UPD_PHASES = [
  ['prepare', 'Подготовка'], ['backup', 'Резервная копия базы'],
  ['fetch', 'Связь с GitHub'], ['download', 'Загрузка изменений'],
  ['install', 'Установка файлов'], ['deps', 'Зависимости'],
  ['verify', 'Проверка целостности'], ['restart', 'Перезапуск'],
  ['done', 'Готово'],
];

function updFmtKB(bytes) {
  if (!bytes || bytes < 1024) return bytes ? bytes + ' Б' : '—';
  if (bytes < 1024 * 1024) return Math.round(bytes / 1024) + ' КБ';
  return (bytes / 1024 / 1024).toFixed(1) + ' МБ';
}

function updFmtDur(s) {
  s = Math.max(0, Math.round(s || 0));
  if (s < 60) return s + ' с';
  return Math.floor(s / 60) + ' мин ' + (s % 60) + ' с';
}

function openUpdateProgress() {
  state.updModalOpen = true;
  const { slot, close } = openModal(`
    <div class="modal-title">🔄 Обновление системы</div>
    <style>@keyframes upd-pulse{0%,100%{opacity:1}50%{opacity:.3}}.upd-active{animation:upd-pulse 1.2s infinite}</style>
    <div class="progress-outer"><div class="progress-inner" id="upd-bar" style="width:5%"></div></div>
    <div id="upd-phases" style="margin:10px 0;font-size:13.5px"></div>
    <p class="form-hint" id="upd-step">Запуск…</p>
    <p class="form-hint" id="upd-metrics"><span id="upd-elapsed"></span> <span id="upd-bytes"></span></p>
    <pre class="codeblock" id="upd-log" style="max-height:180px;overflow:auto;font-size:11.5px"></pre>
    <div id="upd-summary" class="hidden" style="margin:10px 0"></div>
    <div class="modal-actions"><button class="btn btn-primary hidden" id="upd-done">Готово</button></div>`,
    { onClose: () => { state.updModalOpen = false; state.updAutoDismissed = autoKey;
                       clearInterval(state.updTimer); clearInterval(updTick); } });
  let finalShown = false;           // v1.50.0: ровно одно финальное уведомление
  let waitingSince = 0;             // с момента «сервис перезапускается»
  let autoKey = 0;                  // v1.52.1: started_at текущего запуска
  const t0 = Date.now();
  const updTick = setInterval(() => {
    const el = slot.querySelector('#upd-elapsed');
    if (el && !el.dataset.done)
      el.textContent = '⏱ прошло: ' + updFmtDur((Date.now() - t0) / 1000);
  }, 1000);
  const phaseRows = (st) => {
    const ph = st.phases || {};
    return UPD_PHASES.map(([key, title]) => {
      const p = ph[key];
      if (!p) return `<div class="form-hint" style="opacity:.45">○ ${title}</div>`;
      const dur = p.finished_at && p.started_at
        ? Math.max(1, Math.round(p.finished_at - p.started_at)) : 0;
      const dtxt = dur ? ` · ${dur} с` : '';
      if (p.status === 'done') return `<div style="color:var(--ok)">✓ ${title}${dtxt}</div>`;
      if (p.status === 'failed') return `<div class="form-error">✗ ${title}${dtxt}</div>`;
      // после успеха активных фаз не бывает: рестарт мог не успеть закрыться
      if (st.success) return `<div style="color:var(--ok)">✓ ${title}${dtxt}</div>`;
      const live = p.started_at
        ? ` · ${Math.max(1, Math.round((Date.now() - p.started_at * 1000) / 1000))} с` : '';
      return `<div style="color:var(--brand)"><span class="upd-active">●</span> ${title}${dtxt}${live}…</div>`;
    }).join('');
  };
  const finishUp = (st) => {
    clearInterval(state.updTimer); clearInterval(updTick);
    state.updModalOpen = false;
    // v1.53.0: перезагрузка страницы нужна, только если сменилась версия
    // интерфейса; пустой bootVersion — перезагружаемся (безопасно)
    const needsReload = !!(st.current_version
                            && (!state.bootVersion || st.current_version !== state.bootVersion));
    const bar = slot.querySelector('#upd-bar');
    if (bar) bar.style.width = st.success ? '100%' : Math.max(5, st.progress || 0) + '%';
    const el = slot.querySelector('#upd-elapsed');
    if (el) { el.dataset.done = '1'; el.textContent = '⏱ заняло: ' + updFmtDur(st.elapsed_s); }
    const stepEl = slot.querySelector('#upd-step');
    if (stepEl) stepEl.textContent = st.success
      ? (st.needs_restart ? 'Файлы обновлены — нужен перезапуск сервиса'
                          : `Обновление установлено: работает v${st.to_version}`)
      : 'Обновление не удалось — система возвращена к прежней версии';
    // итоговый отчёт: что установилось
    const sum = slot.querySelector('#upd-summary');
    if (sum) {
      const rows = ['<dl class="kv" style="font-size:13px">'];
      rows.push(`<dt>Версия</dt><dd>v${esc(st.from_version || '')} → <b>v${esc(st.to_version || '')}</b></dd>`);
      rows.push(st.blocks_changed && st.blocks_changed.length
        ? `<dt>Блоки</dt><dd>${st.blocks_changed.map(b =>
            `${esc(b.name)}: v${esc(b.from || '—')} → v${esc(b.to)}`).join('<br>')}</dd>`
        : '<dt>Блоки</dt><dd>структурных изменений нет</dd>');
      if (st.downloaded_bytes) rows.push(`<dt>Загружено</dt><dd>${updFmtKB(st.downloaded_bytes)}</dd>`);
      rows.push(`<dt>Время</dt><dd>${updFmtDur(st.elapsed_s)}</dd>`);
      if (st.backup_file) rows.push(`<dt>Резервная копия</dt><dd>${esc(st.backup_file)}</dd>`);
      if (typeof st.changed_count === 'number')
        rows.push(`<dt>Изменено файлов</dt><dd>${st.changed_count}</dd>`);
      rows.push(`<dt>Перезагрузка</dt><dd>${needsReload
        ? 'выполнится автоматически' : 'не потребовалась — программа продолжила работу'}</dd>`);
      rows.push('</dl>');
      if (st.rolled_back) rows.push('<div class="info-callout" style="margin-top:8px">При обновлении произошёл сбой — система автоматически вернулась к прежней версии. Данные целы, можно пробовать ещё раз.</div>');
      if (st.error && !st.success) rows.push(`<p class="form-error">${esc(st.error)}</p>`);
      sum.innerHTML = rows.join('');
      sum.classList.remove('hidden');
    }
    // ОДНО финальное уведомление — только теперь, когда известно главное
    if (st.success && !st.needs_restart && needsReload) {
      // v1.53.0: интерфейс сменился — программа перезагружается САМА
      try { sessionStorage.setItem('ymaster-updated', '1'); } catch (e) {}
      if (stepEl) stepEl.textContent = `Обновление установлено: работает v${st.to_version} — обновляю интерфейс…`;
      toast(`Готово: работает v${st.to_version} — страница перезагрузится сама`, 'ok', '🔄 Обновление');
      setTimeout(() => location.reload(), 1800);
    } else if (st.success && !st.needs_restart) {
      // версия интерфейса не менялась — данные обновляем на месте
      try { sessionStorage.setItem('ymaster-updated', '1'); } catch (e) {}
      if (stepEl) stepEl.textContent = `Обновление применено: v${st.to_version} — перезагрузка не потребовалась`;
      toast(`Обновлено: v${st.to_version} — данные обновлены, продолжаем работу`, 'ok', '🔄 Обновление');
      try { refreshBadges(); } catch (e) {}
      try { route(true); } catch (e) {}
    } else if (st.success) {
      toast('Файлы обновлены. Перезапустите сервис: sudo systemctl restart ymaster-check', 'warn', '🔄 Обновление');
    } else if (st.rolled_back) {
      toast('Сбой при обновлении — система откатилась на прежнюю версию, данные целы', 'warn', '🔄 Обновление');
    } else {
      toast(st.error || 'Обновление не удалось', 'err', '🔄 Обновление');
    }
    const log = slot.querySelector('#upd-log');
    if (log) log.textContent = (st.log || []).join('\n');
    const btn = slot.querySelector('#upd-done');
    if (btn) {
      btn.classList.remove('hidden');
      btn.onclick = () => { close(); };   // перезагрузка выполняется автоматически
    }
  };
  clearInterval(state.updTimer);
  state.updTimer = setInterval(async () => {
    let resp;
    try { resp = await api.get('/api/v1/admin/update/status'); }
    catch { return; }
    const st = resp.job || {};
    st.current_version = resp.current_version || '';
    autoKey = st.started_at || autoKey;
    const bar = slot.querySelector('#upd-bar');
    if (bar) bar.style.width = Math.max(5, st.progress || 0) + '%';
    const ph = slot.querySelector('#upd-phases');
    if (ph) ph.innerHTML = phaseRows(st);
    const stepEl = slot.querySelector('#upd-step');
    const curPhase = st.step && st.phases ? st.phases[st.step] : null;
    if (stepEl && st.running) {
      let msg = (curPhase && curPhase.message) || st.step || '…';
      const idle = st.last_activity ? Math.round(Date.now() / 1000 - st.last_activity) : 0;
      if (idle > 15) msg += ` — этап идёт дольше обычного (${idle} с), процесс активен`;
      stepEl.textContent = msg;
    }
    const bytes = slot.querySelector('#upd-bytes');
    if (bytes && st.downloaded_bytes)
      bytes.textContent = `· ⬇ загружено: ${updFmtKB(st.downloaded_bytes)}`;
    const log = slot.querySelector('#upd-log');
    if (log) log.textContent = (st.log || []).join('\n');
    if (!st.finished) return;
    // v1.50.0: успех считается только когда НОВЫЙ процесс реально работает
    const confirmed = !!(st.to_version && st.current_version === st.to_version);
    if (st.success && st.restart_required !== false && !confirmed) {
      // ждём подтверждения рестарта — только если перезапуск был запланирован
      if (!waitingSince) waitingSince = Date.now();
      const stepEl2 = slot.querySelector('#upd-step');
      const wsec = Math.round((Date.now() - waitingSince) / 1000);
      if (stepEl2) stepEl2.textContent = `Установлено — сервис перезапускается… ${wsec} с`;
      if (Date.now() - waitingSince > 180000) {   // рестарт так и не случился
        st.needs_restart = true;
        finishUp(st);
      }
      return;
    }
    if (finalShown) return;
    finalShown = true;
    finishUp(st);
  }, 2000);
}

const WHATS_NEW = {
  '1.57.5': [
    { icon: '📥', title: 'Загрузка данных чека — как в проверенной версии',
      text: 'Вернули блок из v1.25.1: кнопка получения данных отправляет запрос всегда — никаких «данные уже получены». У чека с загруженными данными в списке стоит значок «📥✓», у остальных — кнопка «📥». Квота proverkacheka расходуется по нажатию: старые чеки удобнее обновлять небольшими частями.' },
  ],
  '1.57.4': [
    { icon: '🛠', title: 'Починено: данные чеков снова загружаются',
      text: 'Нашли причину «чеки не загружаются, хотя ключ заполнен»: сервер отказывал чекам с отметкой «полные данные получены», а у всех старых чеков отметка стоит при пустых расширенных полях. Теперь запрос по кнопке в карточке проходит принудительно — данные с proverkacheka.com загружаются и обновляются. Ваши правки по-прежнему не затираются.' },
  ],
  '1.57.3': [
    { icon: '🔄', title: 'Кнопка получения данных — прямо в карточке чека',
      text: 'Исправили тупик: у части чеков кнопка «получить данные» в списке скрывалась (полные данные считались полученными), а блок расширенных данных был пуст — и запросить их было негде. Теперь в карточке чека всегда есть либо «🧾 Полные данные», либо кнопка «Получить данные из proverkacheka.com», плюс «Обновить данные» для повторного запроса. Ваши правки не затираются — заполняются только пустые поля.' },
  ],
  '1.57.2': [
    { icon: '🧾', title: 'Формат чека — по данным настоящего API',
      text: 'Сверили разбор с реальным ответом proverkacheka.com: исправили потерю «Номера заказа» (сервис присылает свойства и объектом, и списком — теперь принимаются оба вида), добавили контрольный знак сообщения и служебные поля. Чек разбирается целиком: позиции с признаками, итоги НДС, касса/смена, налог, место расчётов — всё в карточке чека.' },
  ],
  '1.57.1': [
    { icon: '🧾', title: 'Полные данные чека — теперь в карточке чека',
      text: 'Всё, что присылает proverkacheka.com (место расчётов, касса и смена, налог, свойства заказа, итоги НДС, формат ФФД, оплаты), теперь видно в карточке чека — раскрывающийся блок «Полные данные чека». Запрос к API стал устойчивее: три формата запроса выбираются автоматически, а при ошибке видно точную причину — токен, квота или формат.' },
  ],
  '1.57.0': [
    { icon: '🔌', title: 'Данные чека — только через proverkacheka.com',
      text: 'Оставили один стабильно работающий источник полных данных чека — proverkacheka.com (документацию API сверили, забираем максимум полей: добавились версия формата ФФД, адрес источника и тип оплаты/товара у позиций). Приложение ФНС, Честный Знак и ОФД-ру выведены из системы — сервисы перестали отвечать. Блок «Проверка чеков (ФНС)» с мастер-токеном не тронут: когда придёт разрешение ФНС, источник включится первым автоматически.' },
  ],
  '1.56.2': [
    { icon: '🛠', title: 'Точечная стабилизация сервера',
      text: 'Живой статус чеков теперь корректно переживает закрытие вкладки и обрывы связи — без лишних записей в журнале сервера. Для работы ничего не изменилось.' },
  ],
  '1.56.1': [
    { icon: '🔎', title: 'Мелкая шлифовка вёрстки',
      text: 'У картинок убрали «щель» под ними (изображения стали блочными), текст главной страницы теперь масштабируется вместе с настройкой размера шрифта в браузере — удобно, если хочется крупнее. Поля ввода остались 16px, чтобы телефон не зумил при вводе.' },
  ],
  '1.56.0': [
    { icon: '📱', title: 'Программа удобна на любом экране — от 320px до десктопа',
      text: 'Чеки, компании и отчёты на телефоне прокручиваются внутри своих таблиц — страница больше не разъезжается вбок. Кнопки и пункты меню на сенсорных экранах стали крупнее (от 44px — удобно попадать пальцем), поля ввода — 16px, чтобы телефон не зумил при вводе. Картинки не выходят за границы, меню корректно сообщает о своём состоянии программам чтения с экрана (доступность).' },
  ],
  '1.55.1': [
    { icon: '📱', title: 'Главная: всё видно и на телефоне, и в браузере',
      text: 'Исправили вёрстку главной страницы: она прокручивается целиком — верх лендинга, форма входа и регистрации больше не обрезаются (раньше страница была фиксированной высоты и прятала верх, а форма уезжала за край на телефоне). Поля на мобильном стали 16px — телефон не зумит при вводе, кнопки — во всю ширину. Попутно усилили сохранность данных: копия базы теперь делается и вне каталога приложения, повторный запуск установки обновляет код, а не пересоздаёт его, и база восстанавливается из копии автоматически.' },
  ],
  '1.55.0': [
    { icon: '🧾', title: 'Электронный чек сразу после скана',
      text: 'Отсканировали чек — на странице появляется его электронная копия с позициями и суммой: видно, что сервис работает. Чек можно прислать себе красивым HTML-письмом, а баллы — скопить в кабинете (кабинет открывается за минуту, чеки этого браузера присоединятся сами). Источники данных чека теперь отдают и хранят максимум полей (место расчётов, смена, налог, свойства заказа), «свои шлюзы» выведены из системы, мобильная вёрстка главной исправлена.' },
  ],
  '1.54.0': [
    { icon: '🏠', title: 'Новая главная: скан чека с первого экрана',
      text: 'Кто заходит на chek.ymaster.ru впервые, сразу видит суть: сканируй чеки — получай баллы и кэшбэк, со статистикой базы, ответами на вопросы и кнопкой сканирования, которая открывает камеру сразу. Вход для постоянных пользователей не изменился — логин, пароль или отпечаток, как прежде. После проверки чека данные можно получить на e-mail.' },
  ],
  '1.53.0': [
    { icon: '🔄', title: 'Обновление завершает всё само',
      text: 'Ход обновления виден в реальном времени на каждом шаге: загрузка в процентах и килобайтах, установка зависимостей с живым журналом, таймер каждого этапа. После установки программа сама решает: изменился код — сервис и страница перезагружаются автоматически; менялась только документация — данные обновляются на лету, и работа продолжается без перезагрузки.' },
  ],
  '1.52.1': [
    { icon: '⏱', title: 'Ход обновления виден всегда',
      text: 'Исправлено «зависание» окна обновления: загрузка изменений теперь видна в процентах и килобайтах в реальном времени, а итог обновления фиксируется до перезапуска сервиса — после него окно показывает успешный отчёт, а не ошибку. Если окно закрыть или перезагрузить страницу, «Настройки» вернут его, пока обновление идёт.' },
  ],
  '1.52.0': [
    { icon: '⌨', title: 'Сканер — весь: добавлен резервный ввод реквизитов',
      text: 'В форме сдачи чека появилась кнопка «⌨ Ввести вручную» — на случай, когда QR повреждён или не читается. Реквизиты с чека (дата, сумма, ФН, ФД, ФП) собираются в строку QR и проходят обычную проверку. Теперь в каждом месте сдачи чека есть все способы: камера, фото, перетаскивание, строка QR и ручной ввод.' },
  ],
  '1.51.0': [
    { icon: '📸', title: 'Сканер чека — везде, где сдаётся чек',
      text: 'Тот же отлаженный сканер, что в разделе «Сканирование», теперь на странице «Сдать чек» и в кабинете «Мой Чек-Пул»: живая камера (распознаёт автоматически и игнорирует чужие QR-коды), фото и перетаскивание снимка на форму. Участникам пула больше не нужно никуда переходить — сдать чек можно прямо из кабинета.' },
  ],
  '1.50.0': [
    { icon: '🔄', title: 'Обновление: видно каждый шаг',
      text: 'Процесс обновления стал пошаговым: резервная копия, загрузка (сколько килобайт и за сколько времени), установка файлов, проверка целостности, перезапуск. В конце — отчёт: какая версия установилась и какие блоки программы обновились (версия блока: старая → новая). Сообщение об успехе приходит одно — когда новая версия уже точно работает.' },
  ],
  '1.49.0': [
    { icon: '⬆', title: 'Копию можно вернуть обратно',
      text: 'Скачанную раньше резервную копию теперь можно загрузить в программу: кнопка «⬆ Загрузить копию» в разделе «Резервные копии». Файл проверяется на целостность и попадает в список — если что-то пошло не так, восстановление в пару кликов.' },
  ],
  '1.48.0': [
    { icon: '🧭', title: 'Настройки по порядку + фильтр копий',
      text: 'Настройки перестроены от важного к деталям: сверху данные и восстановление, затем проверки и почта, интеграции, личное. В резервных копиях — фильтры по дате и типу (видны только даты, которые есть в архиве) и кнопка проверки копии перед восстановлением с контролем хеша.' },
  ],
  '1.47.0': [
    { icon: '🗄', title: 'Архивные копии: день, неделя, месяц',
      text: 'Программа теперь сама ведёт архив: каждый день — дневная копия, каждый понедельник — недельная, 1-го числа — месячный архив. В копии вся база (чеки, пользователи, компании), каждая копия проверяется на целостность, у каждой видно, сколько в ней данных. Архивная история не переписывается обновлениями. Исправлена причина, по которой старые копии могли терять последние данные.' },
  ],
  '1.46.1': [
    { icon: '🛟', title: 'Откат рабочих версий — кнопки на месте',
      text: 'В блоке «Последние рабочие версии» появилась кнопка «Откатиться» у каждой прошлой версии, кнопка обновления списка и откат к произвольному коммиту — даже если версии ещё нет в реестре. Добавлены пояснения во всех состояниях и полный пробег по всем блокам программы.' },
  ],
  '1.46.0': [
    { icon: '🛟', title: 'Восстановление базы и откат к рабочим версиям',
      text: 'Резервные копии теперь можно вернуть в программу прямо из настроек — одной кнопкой, со страховой копией и проверкой целостности. Появился блок «Последние рабочие версии»: программа запоминает три последние версии, на которых работала, и к любой можно откатиться, если обновление оказалось проблемным — из приложения или командой sudo bash rollback.sh с сервера, даже если программа не запускается.' },
  ],
  '1.44.2': [
    { icon: '✉', title: 'Почта на порту 465 работает',
      text: 'Исправили отправку писем через серверы, принимающие почту по защищённому соединению с первого байта (порт 465) — в том числе почту Timeweb. Рассылки Чек-Пула и письма кабинета уходят без изменений настроек.' },
  ],
  '1.44.0': [
    { icon: '✉📬', title: 'Почтовый центр: свой ящик, шаблоны и бот рассылок',
      text: 'Новый блок в Настройках: ящик отправителя на своём сервере (например, chek@chek.ymaster.ru) с паролем (хранится зашифрованным), имя отправителя и тексты писем — приветствие и подпись. Бот рассылок сам пишет по расписанию (день недели и время) или по условию: напомнит подтвердить e-mail, вернёт участника, который давно не сдавал чеки, отправит сводку по списку адресов. Письма — красивые (HTML), с отпиской в один клик, журнал отправок хранится 90 дней.' },
  ],
  '1.43.0': [
    { icon: '🔑', title: 'Вход по отпечатку пальца и лицу — без пароля',
      text: 'После первого входа приложение предложит привязать это устройство: дальше вход — одним касанием отпечатка или взгляда в камеру (Windows Hello, Touch ID, Android Biometric; вместо биометрии устройство может попросить PIN — это тоже нормально). Биометрия не покидает устройство: на сервере хранится только цифровой ключ, поэтому способ безопасен и с точки зрения 152-ФЗ. Устройств может быть несколько — список в Настройках → «Быстрый вход», любое можно отвязать. На устройствах без сканера всё остаётся как было.' },
  ],
  '1.42.0': [
    { icon: '🔐', title: 'Иерархия ролей укреплена: меню переключения — только у администратора',
      text: 'Меню профиля в шапке теперь есть только у администратора: бухгалтер и сотрудник видят свою карточку без переключателя ролей, участники Чек-Пула — свой кабинет. В режиме просмотра администратор действует строго с правами выбранной роли: может всё, что может она, и ничего сверх её полномочий — так сохраняется иерархия. В разделе «Пользователи» добавлена памятка по иерархии ролей.' },
  ],
  '1.41.0': [
    { icon: '🧩', title: 'Чек-Пул: просмотр глазами участника + раздел «Участники»',
      text: 'Полная картина ролей: теперь администратор может открыть кабинет Чек-Пула глазами самого участника — тем же способом, что и профили бухгалтера и сотрудника. Новый раздел «Чек-Пул: участники» показывает список (чеки, баллы, риск, статус) с поиском и кнопкой «👁» у каждого активного участника; участники появились и в меню профиля в шапке. Ядро-логин администратора не прерывается, пароль не требуется, включение и выход — в журнале действий. Кабинеты заблокированных участников не открываются.' },
  ],
  '1.40.0': [
    { icon: '👁', title: 'Режим просмотра — кнопка в списке пользователей',
      text: 'Включать просмотр глазами сотрудника стало проще: кнопка «👁» появилась в разделе «Пользователи» — в строке каждого активного бухгалтера и сотрудника. Работает так же, как меню в шапке: без пароля, с жёлтой полосой возврата и записью в журнал действий. Сам режим просмотра появился раньше (v1.18.0) и не изменился.' },
  ],
  '1.39.0': [
    { icon: '🔌', title: 'Чек-Пул: платное API для внешних клиентов',
      text: 'Открыт программный доступ к Чек-Пулу — по принципу «продаём доступ к фильтрам, а не чеки». Партнёр получает ключ (виден один раз, в базе только хэш) и лимиты по тарифу (по умолчанию 60 запросов/час и 5 000/мес, настраиваются при выдаче). Наружу уходят только анонимные агрегаты: сводка пула, чеки по регионам и отраслям, топ продавцов (публичные реквизиты), «сколько чеков под фильтрами» и суммы-агрегаты — сырые чеки, участники и персональные данные не отдаются никогда. API включается администратором и по умолчанию выключен; каждый вызов учитывается в журнале (хранение ≤ 90 дней по 152-ФЗ), выдача и отзыв ключей — в журнале действий.' },
  ],
  '1.38.0': [
    { icon: '🕸', title: 'Чек-Пул: граф связей антифрода и партнёрский кэшбэк с QR',
      text: 'Рост по плану. В панели антифрода появился граф связей: участники, объединённые общим устройством, подсетью или реферальной парой, рисуются кластерами — «фермы аккаунтов» видны сразу; нажатие на участника открывает его карточку. Запущен партнёрский кэшбэк: магазины-партнёры ставят у кассы QR со ссылкой на страницу сдачи чека, участник сдаёт чек партнёра и получает баллы — процент от суммы чека (до 100 за чек), после верификации; в карантине антифрода кэшбэк приостанавливается. Список партнёров и печатные QR — на новой странице «Партнёры и кэшбэк». ML-классификатор позиций — по плану после накопления корпуса (5–10 тыс. позиций); платное API партнёров — в следующем выпуске.' },
  ],
  '1.37.0': [
    { icon: '🏢', title: 'Чек-Пул: подбор чеков для компаний — отчёт из открытой базы',
      text: 'У бухгалтера появился раздел «Подбор из пула»: фильтры по периоду, региону, городу, отрасли, ИНН, сумме и названию магазина или товару; привязка чеков к компании одним нажатием и выгрузка CSV для АО-1 и Excel. Авто-подбор собирает набор чеков под сумму отчёта с точностью ±5%. Привязанный чек чужим компаниям не виден, а участник сохраняет свои баллы. Квота на компанию — 100 чеков в месяц (настраивается администратором; тариф — подписка 5 000 ₽/мес или 50 ₽/чек — решается при запуске). «Свои» сотрудники: если логин сотрудника — корпоративный e-mail и он подтверждён в кабинете пула, его чеки уходят компании автоматически, минуя общий пул.' },
  ],
  '1.36.0': [
    { icon: '🏆', title: 'Чек-Пул: ачивки, лидерборд месяца и вывод баллов',
      text: 'Вовлечение по плану: за реальные чеки — ачивки «50 чеков», «3 отрасли» и «первый чек региона» (вы первопроходец — ваш чек первый из своего региона). В кабинете и публично на странице «Сдать чек» — лидерборд месяца: топ-10 участников и гонка регионов («ваш город на N-м месте»); видны только маскированные имена, город и число чеков, никаких телефонов и адресов. Добавилась цель вывода с прогресс-баром и заявка на вывод прямо в кабинете: минимум 100 баллов; телефон запрашивается только на этом шаге и хранится только хэшем; первый вывод проходит SMS-подтверждение — код заработает, когда подключим шлюз.' },
  ],
  '1.35.0': [
    { icon: '🎁', title: 'Чек-Пул: приглашайте друзей — баллы за их первые шаги',
      text: 'У каждого кабинета появился код приглашения (YM-XXXXXX) и ссылка вида chek.ymaster.ru/#/r/КОД — вкладка «Приглашайте» в кабинете. Баллы начисляются не за регистрацию, а за реальные шаги приглашённого, причём с задержкой против накрутки: подтверждение почты +5, первый чек от 100 ₽ +20, пятый +50, двадцатый +150, пятидесятый +500; плюс 5% с проверенных чеков приглашённого — но не больше 200 баллов в месяц. Лимит — 50 приглашённых на человека, один уровень. Накрутка «сам себя пригласил» бессмысленна: общие устройства и подсети, мгновенные чеки и петли ловит антифрод из Этапа 5 — по таким парам выплаты приостанавливаются до разбора.' },
  ],
  '1.34.0': [
    { icon: '🛡', title: 'Чек-Пул: антифрод из пяти слоёв — карантин вместо банов',
      text: 'Пул защищён от накрутки, как в концепции: устройство (один браузер на несколько аккаунтов), сеть (кластеры подсетей и адреса датацентров), поведение (мгновенная отправка форм, ровные интервалы чеков, поток больше 20 в час), связи между участниками и бизнес-правила (одинаковые суммы подряд). Каждое совпадение — сигнал с весом; риск ≥ 71 включает карантин: чеки сохраняются, но баллы приостанавливаются до разбора. В новом разделе админа «Чек-Пул: антифрод» — лента сигналов, карточка участника (устройства, IP, связи), массовый разбор, снятие карантина и аннулирование баллов. Честные участники не помечаются: пороги с отступом, разобранные как ложные сигналы снижают риск.' },
  ],
  '1.33.0': [
    { icon: '🗺', title: 'Чек-Пул: регионы, отрасли и панель модерации',
      text: 'Каждый чек пула теперь размечается автоматически: регион и город — из адреса места расчёта (если адреса нет — по ИНН продавца через ЕГРЮЛ), отрасль — по сети (Пятёрочка, Аптека 36,6, Лукойл и ещё 50 сетей) или по составу покупок. В Настройках появился раздел «Чек-Пул: модерация»: чеки с ручной проверкой принимаются или отклоняются в два клика (при принятии участнику начисляется балл), есть поиск по ФН/ИНН/магазину, фильтры «без региона/отрасли», ручная разметка, дообогащение старых чеков и выгрузка CSV для Excel. Покрытие видно сразу: цель — 70% чеков с регионом и 60% с отраслью.' },
  ],
  '1.32.0': [
    { icon: '👤', title: 'Кабинет Чек-Пула: регистрация и «сдал чек — забрал чек»',
      text: 'Сдавать чеки по-прежнему можно без регистрации, но теперь у участника есть кабинет: e-mail + пароль либо одноразовая ссылка входа (если администратор настроил почту в Настройках → Чек-Пул). Чеки, сданные без регистрации в этом браузере, автоматически присоединяются к кабинету — ничего не теряется. В кабинете: баланс, «Мои чеки» со статусами и составом (позиции), выгрузка CSV для Excel, печать/PDF, смена пароля и e-mail, удаление аккаунта — персональные данные стираются, баллы сгорают, чеки остаются в пуле обезличенно. Кабинет отдельный от рабочей программы: вход сотрудников компаний не меняется.' },
  ],
  '1.31.0': [
    { icon: '🧾', title: 'Сдать чек в Чек-Пул — прямо на сайте',
      text: 'Приём чеков в открытую базу переехал на сайт — Telegram больше не нужен. На странице «Сдать чек» любой человек вставляет строку QR (или ссылку из приложения ФНС) либо фотографирует QR-код — камера телефона, распознавание прямо в браузере. Регистрация не нужна: баллы копятся в браузере, а «Мои чеки» показывают статус проверки. Согласие с офертой фиксируется в журнале; от ботов — скрытая ловушка и лимит отправок.' },
  ],
  '1.30.0': [
    { icon: '🧩', title: 'Чек-Пул: этап 1 — приём чеков через бота',
      text: 'Начали строить открытую базу чеков (концепция docs/plan.md). Администратор включает приём в Настройках → «🧩 Чек-Пул». Любой человек пересылает боту строку QR — система проверяет чек по официальным источникам (ФНС GetTicket → Честный Знак, без квот), сохраняет позиции и начисляет балл. Дубли баллов не приносят; аномальные суммы (свыше 500 000 ₽) уходят на ручную проверку; лимит 50 чеков/сутки с человека. Ядро приложения не затронуто.' },
  ],
  '1.29.0': [
    { icon: '☎️', title: 'Telegram-бот: работает даже там, где Telegram заблокирован',
      text: 'Разобрались, почему бот молчал после добавления токена: во многих сетях РФ api.telegram.org блокируется — сервер просто не мог достучаться. Теперь в Настройках → Telegram есть прокси (socks5/https) и кнопка «🔍 Диагностика», которая показывает, доступен ли Telegram напрямую и через прокси. Воркер стал надёжнее: новый токен/прокси подхватывает сам, без перезапуска и конфликтов.' },
  ],
  '1.28.0': [
    { icon: '🗂', title: 'Чеки: удобнее фильтры и статьи расходов',
      text: 'Фильтр «Сотрудник» убран — кто добавил и так видно («Кто добавил»). «Статья расходов» в фильтрах стала выпадающим списком статей, которые реально встречаются в работе (до 20). При заполнении чека — выбор из самых частых статей плюс «Своя…»: своя статья до 100 символов со счётчиком «введено X из 100 · осталось Y». Исправлено: повторные клики по чеку больше не открывают новые окна — двойной клик закрывает карточку, а открытый чек подсвечивается в списке.' },
  ],
  '1.27.0': [
    { icon: '🔌', title: 'Коннекторы чеков: GetTicket ФНС и «Приложение ФНС»',
      text: 'Два новых способа получать ПОЛНЫЕ чеки с позициями. 1) Официальное API ФНС теперь не только проверяет чек, но и запрашивает его целиком (GetTicket) — достаточно Мастер-токена. 2) Новый источник «Приложение ФНС»: данные берутся как в мобильном приложении «Проверка чеков» — нужен ИНН и пароль личного кабинета ФНС, токены и квоты не нужны. Цепочка: API ФНС → Приложение ФНС → Честный Знак → ОФД-ру → свои → proverkacheka (последним).' },
  ],
  '1.26.2': [
    { icon: '🧾', title: 'Новая иконка приложения: чек с QR в стиле «мягкий 3D»',
      text: 'Иконка приложения снова про главное — белый чек с настоящим QR-кодом, глянцевой оранжевой галочкой «проверено» и адресом chek.ymaster.ru, теперь в современном тактильном 3D-стиле. Логотип-смайлик «всё сошлось» остался фирменным знаком: в интерфейсе, на бланках и в документах.' },
  ],
  '1.26.1': [
    { icon: '🙂', title: 'Новый фирменный стиль: логотип-смайлик',
      text: 'Обновлены логотип и иконка приложения — чек-смайлик «всё сошлось» с адресом chek.ymaster.ru. Подготовлена фирменная система для будущих задач: знак, горизонтальная и вертикальная версии логотипа, одноцветные варианты для печатей и шаблон фирменного бланка А4 (папка brand/ в составе проекта).' },
  ],
  '1.26.0': [
    { icon: '🔌', title: 'Заполнение чеков: новый источник «Честный Знак»',
      text: 'Добавлен анонимный источник данных (мобильное API ГИС МТ) — без токена и квот, работает так же просто, как proverkacheka: один запрос со строкой QR. Теперь цепочка: API ФНС → Честный Знак → ОФД-ру → свои шлюзы → proverkacheka (последним, чтобы беречь квоту 12–14 запросов в сутки). Источник, ответивший без позиций чека, пропускается.' },
  ],
  '1.25.2': [
    { icon: '📥', title: 'Полные данные: без повторных запросов',
      text: 'Чек, проверенный ФНС и содержащий позиции, теперь считается «с полными данными» — кнопка запроса у него не показывается (только «изменить»). Повторный запрос возможен только если чек действительно не получил данные полностью; массовый запрос пропускает уже полные чеки и сообщает, сколько пропущено.' },
  ],
  '1.25.1': [
    { icon: '🧾', title: 'Страница «Чеки»: список больше не пропадает',
      text: 'Исправлено: после живых событий (новый чек, проверка ФНС) список чеков иногда исчезал до перезагрузки страницы. Теперь данные обновляются на месте — без сброса фильтров и пустого экрана; при сбое сети показывается «Повторить». Фильтр «Сотрудник» и поиск по имени работают в любом регистре (кириллица).' },
  ],
  '1.25.0': [
    { icon: '☀️', title: 'Единая светлая тема — всё читается',
      text: 'Переключатели оформления убраны: приложение всегда в светлом стиле «Ямастер» — белые карточки, текст #333, фирменный оранжевый дозированно. Исправлена читаемость карточки чека, уведомлений и всплывающих подсказок (было тёмное окно с тёмным шрифтом). В настройках остался только «Спокойный час».' },
  ],
  '1.24.1': [
    { icon: '🖨', title: 'Чеки печатаются с фискальным QR-кодом',
      text: 'Исправлено: лист больше не печатается раньше, чем загрузятся QR-коды. Коды загружаются для всех выбранных чеков (раньше — только для 60), с прогрессом; чек без реквизитов получает пометку «фискальный QR не получен».' },
  ],
  '1.24.0': [
    { icon: '🖨', title: 'Кнопка «Напечатать чек»: нарезка на А4 в 3 колонки',
      text: 'Чеки автоматически укладываются на лист А4 в три колонки с уплотнением — минимум пустого места. Длинные чеки переносятся построчно (позиционно) в следующую колонку с пометкой «продолжение чека».' },
    { icon: '🧾', title: 'АО-1: поля страницы и полнота для налоговой',
      text: 'Поля страницы АО-1: левое 20 мм, правое 15 мм, верхнее 10 мм, нижнее 10 мм. В оборотной таблице — полные реквизиты чеков (дата, ФД, ФН/ФП) и суммы «по отчёту/принято к учёту». Добавлены поля РКО и платёжного поручения.' },
  ],
  '1.23.0': [
    { icon: '🔎', title: 'База чеков: фильтры «Кто добавил» и «Данные чека»',
      text: 'Новый выпадающий список «Кто добавил» — только те сотрудники, которые реально добавляли чеки в выбранной компании (с количеством). Фильтр «Данные чека» разделяет чеки, по которым полные данные уже получены, и те, что ещё ждут запроса. У чека с полученными данными кнопка запроса скрыта — осталось только изменение.' },
    { icon: '🧾', title: 'Авансовый отчёт — официальная форма АО-1 как в 1С',
      text: 'Форма приведена к унифицированной АО-1 (Постановление Госкомстата № 55, ОКУД 0302001): лицевая сторона с реквизитами организации из карточки предприятия, расчётом аванса и бухгалтерской записью, оборотная сторона с документами расходов, расписка и «Утверждаю». Реквизиты подставляются из карточки компании. Добавлена кнопка «JSON для 1С» — структура документа в стиле 1С для будущей интеграции.' },
  ],
  '1.22.0': [
    { icon: '⬇', title: 'Свежие данные ЕГРЮЛ/ЕГРИП — скачивание из карточки компании',
      text: 'В карточке компании появилась кнопка «Скачать свежие данные»: программа сначала запрашивает официальную выписку с сайта ФНС (egrul.nalog.ru, PDF), а если она недоступна с сервера — берёт свежую карточку из Checko.ru и формирует печатную выписку. Карточка компании в системе при этом обновляется.' },
    { icon: '🏷', title: 'Сокращённые названия компаний',
      text: 'Если у компании заполнена карточка ЕГРЮЛ/ЕГРИП, в списках и селекторах показывается сокращённое наименование (например «Ямастер» вместо ООО «Ямастер» и т. п.), полное — в подсказке и в карточке.' },
  ],
  '1.21.0': [
    { icon: '📖', title: 'Инструкция по приложению — для каждой роли',
      text: 'В меню слева появилась кнопка «📖 Инструкция»: подробное руководство с картинками — своё для администратора, бухгалтера и сотрудника. Хранится на сервере и обновляется вместе с программой, поэтому всегда актуально. Администратор может почитать инструкцию любой роли.' },
    { icon: '✅', title: 'Обновление — теперь без сюрпризов',
      text: 'Когда программа обновится на сервере, вы увидите аккуратное сообщение «Можно перезагрузиться» с кнопкой — переход на новую версию только по вашему решению, ничего не перезагружается само.' },
    { icon: '👋', title: 'Выход — с подтверждением',
      text: 'При выходе появляется окно «Выйти из аккаунта?» с кнопкой «Остаться» по умолчанию: случайный Enter больше не завершает сеанс.' },
    { icon: '📂', title: 'Блоки настроек сворачиваются',
      text: 'Нажмите на заголовок любого блока в Настройках — содержимое свернётся, а название останется видимым. Состояние запоминается. Также повысили контраст текстов в обеих темах — читается легче.' },
  ],
  '1.20.0': [
    { icon: '🎯', title: 'Умный фокус и тепловая карта сроков',
      text: 'Кнопка «Умный фокус» на дашборде затемняет всё, кроме задач, требующих внимания. Ведомость по подотчётникам стала цветовой картой: зелёный — в срок, оранжевый — до конца срока отчёта меньше 40% времени, красный — просрочка.' },
    { icon: '🎨', title: 'Настройка оформления и «Спокойный час»',
      text: 'В Настройках появился блок «Оформление»: акцентный цвет (оранжевый, индиго, зелень, голубой) и плотность интерфейса (просторная/компактная). Режим «Спокойный час» прячет некритичные уведомления на выбранный срок — ошибки показываются всегда. Пустые списки получили фирменные иллюстрации.' },
  ],
  '1.19.0': [
    { icon: '📜', title: 'Журнал действий — человеческим языком',
      text: 'События теперь читаются как фразы, а не коды: «Мария Смирнова: отсканирован чек на 1 250,00 ₽», а детали каждого события — в кавычках: «ФД 55667, источник: фото». Записи сгруппированы по дням («Сегодня», «Вчера»), у каждого события — значок и цветовой смысл: зелёный — успех, красный — ошибка, оранжевый — внимание.' },
    { icon: '🎨', title: 'Фирменный стиль «Ямастер»',
      text: 'Новая палитра: корпоративный оранжевый, индиго в заголовках, морская зелень успеха и терракота ошибок. Светлая тема теперь по умолчанию — глаза бухгалтеру важнее; тёмная осталась мягкой. Шрифт Inter с табличными цифрами (колонки сумм выровнены), коды и ИНН — JetBrains Mono, рукописные акценты заголовков.' },
  ],
  '1.18.0': [
    { icon: '👁', title: 'Режим просмотра: приложение глазами сотрудника или бухгалтера',
      text: 'Нажмите на свою карточку в правом верхнем углу — появится меню переключения профилей. Выберите бухгалтера или сотрудника, и вы увидите приложение ровно так, как видит его он: свои разделы, свои настройки, свои права. Пароль не запрашивается, ваш административный сеанс не прерывается: вернуться можно кнопкой «Вернуться в администратора» вверху экрана в любой момент. Действия в режиме просмотра выполняются от имени выбранного пользователя — будьте внимательны.' },
  ],
  '1.17.0': [
    { icon: '🔄', title: 'Обновление из приложения — теперь и без пароля',
      text: 'Найдено, почему ваш пароль из терминала не работал в окне обновления: sudo проверяет пароль СЛУЖЕБНОГО пользователя приложения, а не ваш. Теперь приложение перезапускается само (re-exec) — обновление проходит целиком из окна, пароль не нужен вовсе. В настройках появилась готовая sudoers-команда — при желании можно включить и классический перезапуск сервиса.' },
    { icon: '🧹', title: 'Сброс кэша после обновления — автоматически',
      text: 'Клиенты сами узнают о новой версии (даже без открытого окна): кэш PWA удаляется, страница перезагружается на свежем интерфейсе. На всякий случай в Настройках появилась кнопка «Сбросить кэш приложения».' },
    { icon: '🖥', title: 'Блок «Сервер и команды» для администратора',
      text: 'Версия, сборка, аптайм, размер базы, счётчики данных, служебный пользователь — и готовые команды с кнопкой «копировать»: разрешить автоперезапуск, обновить из терминала, статус и логи сервиса.' },
    { icon: '🏢', title: 'Настройки для бухгалтера и сотрудника стали информативнее',
      text: 'Бухгалтер видит свою компанию, ИНН, срок авансовых отчётов и режим проверки ФНС; сотрудник — свои правила и подсказку про Telegram-напоминания. У всех появилась карточка обслуживания устройства.' },
  ],
  '1.16.1': [
    { icon: '🗂', title: 'Checko: интеграция переписана по официальной документации',
      text: 'Найдена причина неработающих карточек ЕГРЮЛ: API Checko отвечает РУССКИМИ ключами (НаимПолн, ОГРН, ЮрАдрес…), а мы ждали английские. Теперь разбирается ровно тот формат, что присылает Checko: полное/краткое наименование, ОКПО, регион, дата регистрации, руководитель, учредители, филиалы, ОКВЭДы, недостоверность и массовость адреса.' },
    { icon: '🧪', title: 'Кнопка «Проверить ключ» в настройках',
      text: 'Один клик делает настоящий запрос в ЕГРЮЛ и показывает, что вернул Checko: наименование организации, статус и остаток запросов на сегодня — ключ либо работает, и вы это видите, либо получаете точную причину ошибки (не тот ключ / лимит / компания не найдена).' },
    { icon: '🚦', title: 'Светофор видит факторы риска ЕГРЮЛ',
      text: 'Недостоверный юридический адрес и «массовость» адреса (10+ организаций по одному адресу) теперь попадают в жёлтые причины риск-оценки контрагента — это официальные маркеры ФНС.' },
  ],
  '1.16.0': [
    { icon: '🔄', title: 'Обновление из приложения — чинит себя само',
      text: 'Найдена причина «обновление не работает»: при установке без git-репозитория применение всегда падало. Теперь приложение само восстанавливает репозиторий (init + fetch с GitHub), preflight честно показывает состояние, а ошибка «ветка не содержит приложения» больше не маскируется под недоступность GitHub.' },
    { icon: '🔔', title: 'Telegram-бот: напоминания и уведомления',
      text: 'Админ вставляет токен от @BotFather в Настройках (хранится зашифрованным). Каждый сотрудник подключается сам: Настройки → Telegram → код → /start КОД. Раз в день в заданное время — напоминание тем, у кого нет чеков за сегодня; уведомления о принятых чеках; /status — чеки дня.' },
    { icon: '🚦', title: 'Светофор контрагента в карточке компании',
      text: '🟢/🟡/🔴 по данным ЕГРЮЛ: ликвидация/банкротство — красный, молодая компания, малый капитал, ликвидация в процессе, нет ИНН — жёлтый с объяснением причин. Защита от расходов по проблемным контрагентам.' },
    { icon: '🏢', title: 'Партнёрские операции для аутсорсеров',
      text: 'Кнопки в списке компаний: «⟳ Обновить ЕГРЮЛ (все)» — массовая актуализация реквизитов клиентов с учётом лимита Checko, и «🧾 АО по всем» — сводный авансовый отчёт по всем компаниям за период.' },
  ],
  '1.15.0': [
    { icon: '🔒', title: 'API-ключи под замком: шифрование в базе',
      text: 'Ключи Checko, ФНС и внешних источников теперь хранятся зашифрованными (AES-CBC + HMAC, Fernet). Файл базы сам по себе ключей не раскрывает — нужен ключ сервера. Старые ключи перешифруются при первом сохранении.' },
    { icon: '🗂', title: 'Форма ключа Checko в Настройках',
      text: 'Админ добавляет ключ в Настройках → блок «ЕГРЮЛ/ЕГРИП — Checko.ru»: вставка, статус с маской, показ по паролю администратора с записью в журнал аудита (защита от подбора — 5 попыток за 5 минут).' },
    { icon: '📋', title: 'Карточка предприятия — все реквизиты ЕГРЮЛ',
      text: 'Из Checko теперь подтягиваются полное и краткое названия, ОПФ, дата регистрации, уставный капитал, ИФНС, руководитель и его должность, контакты, основной и дополнительные ОКВЭД, статус. Пустой ИНН компании заполняется автоматически.' },
    { icon: '🧭', title: 'План уникальности продукта',
      text: 'Составлен план docs/unique_features.md: что сделаем, чтобы «Ямастер Чек» стал №1 у бухгалтеров и коммерческих организаций — светофор контрагентов, партнёрский кабинет, Telegram-напоминания и другое.' },
  ],
  '1.14.0': [
    { icon: '🧾', title: 'Собрать авансовый отчёт за минуту — теперь по всей компании',
      text: 'Диалог отчёта научился работать с периодом любой длины, всеми сотрудниками сразу и выбранными чеками. Один клик — и готовая сводная форма АО с подытогами по каждому подотчётнику, либо классический АО-1 по одному сотруднику.' },
    { icon: '⬇', title: 'CSV авансового отчёта для Excel/1С',
      text: 'Собранный отчёт скачивается одной кнопкой: дата, сотрудник, продавец, ИНН, статья, ФН/ФД/ФП, статус ФНС и сумма — с итоговой строкой. Excel-совместимый формат с кириллицей.' },
    { icon: '🛡', title: 'Контроль качества отчёта',
      text: 'Перед печатью система предупредит о чеках со статусом «Недействителен» в выборке и умеет отбирать только подтверждённые ФНС. Чеков больше 1000? Подскажет сузить период.' },
    { icon: '🏢', title: 'Авансовый отчёт из карточки компании',
      text: 'В карточке компании появилась кнопка «Авансовый отчёт» — период и компания подставляются сами.' },
  ],
  '1.13.0': [
    { icon: '🗑', title: 'Удаление компаний с переносом данных',
      text: 'Компания-дубль или уехавший клиент? Теперь компанию можно удалить: чеки перенести в другую компанию (флаг «выгружено в 1С» сбросится — переезд = повторная выгрузка) либо стереть безвозвратно. Сотрудники переезжают вместе с чеками или открепляются — решаете вы.' },
    { icon: '🛡', title: 'Защита от компаний-дублей',
      text: '«ООО «Ямастер»», ООО "ямастер" и «ООО — ЯМАСТЕР» теперь распознаются как одна и та же компания: при создании система подсказывает о похожей. ИНН стал ключевым атрибутом — проверяются контрольные цифры, два разных ЮЛ с одним ИНН не сохранятся.' },
    { icon: '🗂', title: 'Карточка компании из ЕГРЮЛ (Checko.ru)',
      text: 'В карточке компании — полное название из ЕГРЮЛ, ОГРН, КПП, адрес, руководитель, ОКВЭД и статус. Кнопка «ЕГРЮЛ» по ИНН заполняет данные автоматически (сервис Checko, ключ вставляется один раз в Настройках; бесплатный тариф — 100 запросов в день).' },
    { icon: '⬇', title: 'CSV всех чеков компании одной кнопкой',
      text: 'Из карточки компании можно скачать CSV сразу по всем её чекам — не нужно ничего выбирать вручную.' },
  ],
  '1.12.3': [
    ['👤 На чеках видно, кто добавил и для какой компании', 'в списке чеков, в карточке чека и в сводке выбранных чеков показывается автор (кто отсканировал/прислал) и компания; в CSV-выгрузке — новые колонки «Кто добавил» и «Компания».'],
    ['🎉 Окно «Что нового» — аккуратно на любом экране', 'исправлена вёрстка на телефоне: новости показываются карточками, без разъезжающейся таблицы.'],
  ],
  '1.12.2': [
    ['📐 Вёрстка выровнена на всех устройствах', 'исправлена шапка после добавления селектора компаний: заголовок больше не переносится и не «прыгает», селектор компактный на телефоне, фильтр группы — на всю строку. CSS и JS теперь загружаются с маркером версии — после обновления оформление всегда соответствует коду.'],
  ],
  '1.12.1': [
    ['🛠 Восстановлен запуск приложения в браузере', 'в версии 1.12.0 ошибка вёрстки кода (страница «Пользователи») не давала приложению открыться. Исправлено; добавлен автоматический страж целостности кода — такое не повторится.'],
    ['🏢 Компании из «памятки» — по названию, с авторемонтом', 'компанией считается НАЗВАНИЕ после тире («Иванова — ООО «Альфа-Трейд»» → «ООО «Альфа-Трейд»»); компании, созданные из полного текста памятки, переименованы и слиты автоматически — коллеги одной организации снова в одном пространстве.'],
  ],
  '1.12.0': [
    ['🏢 Компании определяются по «памятке» — существующие данные уже распределены', 'каждый различающийся текст «памятки» приглашения (он же «Организация» сотрудника) стал компанией: приглашения, сотрудники и их чеки перепривязаны автоматически — при первом запуске новой версии.'],
    ['✉️ В приглашении компания — первое поле', 'выберите существующую или введите новую — создастся сама; группа доступа определяется компанией. На странице «Пользователи» — фильтр по компании.'],
  ],
  '1.11.0': [
    ['🏢 Приложение стало платформой: ведите бухгалтерию нескольких компаний', 'администратор создаёт неограниченное число компаний (ООО, ИП), в каждой — свои бухгалтеры и сотрудники со своими чеками. Компании видят только своё пространство; вы видите всё.'],
    ['🔓 Фильтр «Все компании / одна компания» в шапке', 'выберите компанию — дашборд и чеки показывают только её данные; карточка «По компаниям» на дашборде показывает сводку по всем сразу.'],
    ['🔁 Чеки можно перемещать между компаниями', 'отметьте чеки в базе → «Переместить в компанию»: чек переезжает целиком, при желании — сразу с новым подотчётным лицом.'],
    ['✉️ Приглашения с привязкой к компании', 'при создании ссылки выберите компанию — новый сотрудник сразу попадёт в её пространство.'],
  ],
  '1.10.2': [
    ['📱 Меню: выбор пункта работает гарантированно', 'открытое меню — самый верхний слой экрана: ничто не перехватывает нажатие; выбор пункта выполняется самим приложением, без зависимости от поведения браузера. Если пункт не выбирался — после обновления сервера полностью закройте приложение и откройте снова.'],
  ],
  '1.10.1': [
    ['🐛 В приложении теперь выбирается пункт меню', 'исправлена ошибка слоёв: фон-затемнение перекрывал меню на телефоне — нажатие по пункту лишь закрывало меню. Теперь пункт открывает раздел, и меню сворачивается само; «чёлка» и жест-бар iPhone/Android больше не перекрывают меню в установленном приложении.'],
  ],
  '1.10.0': [
    ['📱 Меню и экран: удобно на любом устройстве', 'меню сворачивается всегда — фон, ✕, Esc или свайп; интерфейс адаптируется к планшету и горизонтальному экрану, поворот телефона больше не заблокирован.'],
    ['🧾 Чеки в PDF — как кассовые', 'в печатной версии каждого чека — настоящий фискальный QR-код, как выдаёт касса: проверяется любым приложением проверки чеков.'],
    ['📊 Авансовый отчёт АО-1 за минуту', 'бухгалтер и администратор: форма АО-1 заполняется по чекам сотрудника за месяц, при необходимости правится и печатается («Сохранить как PDF»).'],
    ['🪟 Windows 11 — своё окно', 'установите приложение из Edge («Установить приложение») — работает в отдельном окне, без браузера, как обычная программа.'],
    ['🔑 Обновление с паролем сервера', 'если серверу нужен пароль sudo — поле прямо в диалоге обновления; пароль нигде не хранится и используется только в момент обновления.'],
  ],
  '1.9.1': [
    ['👤 Обновление «как суперпользователь» — прямо из приложения', 'администратор = суперпользователь: перед обновлением карточка показывает готовность (GitHub / право на перезапуск / копия БД). Обновление не начнётся, если что-то не готово — никаких полу-состояний.'],
    ['📢 Все пользователи получают версию автоматически', 'при обновлении сервер предупреждает всех подключённых — страница перезагружается сама, и у каждого открывается новый код (кэш кода больше не задерживает новую версию).'],
  ],
  '1.9.0': [
    ['🏗 Хэш сборки в карточке «Обновления»', 'строка «Сборка» показывает короткий коммит установленной версии — после обновления сразу видно, что код реально применился (сравните с хэшем на GitHub).'],
    ['🧪 Релиз-контроль', 'все зависимости сверяются с requirements.txt автоматическим тестом-стражем; проверка целостности обновления идёт интерпретатором приложения; deploy.sh проверяет живость сайта после рестарта.'],
    ['▣ QR-код приглашения', 'из 1.8.2: пригласите сотрудника QR-кодом — камера телефона, и страница регистрации открылась.'],
  ],
  '1.8.3': [
    ['🛡 Обновления — ещё надёжнее', 'проверка целостности теперь выполняется интерпретатором окружения приложения (venv) — исключён ложный откат исправного обновления; весь конвейер покрыт сквозными тестами.'],
  ],
  '1.8.2': [
    ['▣ QR-код приглашения', 'рядом со ссылкой-приглашением — кнопка «QR-код»: сотрудник просто наводит камеру телефона и попадает на страницу регистрации. QR можно показать с экрана или скачать PNG и отправить.'],
  ],
  '1.8.1': [
    ['🔄 Обновление из приложения теперь применяет себя само', 'раньше файлы обновлялись, но сервис не мог перезапуститься без root — версия оставалась старой. Теперь приложение перезапускает себя само (sudoers-правило ставит deploy.sh) и корректно показывает финал.'],
  ],
  '1.8.0': [
    ['✨ Мерцание устранено насовсем', 'дашборд и список чеков больше не перерисовываются, когда данные не изменились; статус подключения не мигает при фоновых реконнектах; исправлен кэш Service Worker (из-за 404 он вовсе не устанавливался).'],
    ['👥 Личные суммы в чеке', 'в редакторе чека поле «Личные, ₽ (не для учёта)»: сумма к учёту считается сама — разделение личных и рабочих покупок без бухгалтерской справки.'],
    ['⏳ Срок авансового отчёта под контролем', 'настраиваемый срок сдачи (приказ руководителя, п. 6.3 Указания ЦБ 3210-У); в «Базе чеков» бейджи «сдать до / просрочен», у бухгалтера — всегда перед глазами.'],
    ['📋 Ведомость подотчётников за месяц', 'кнопка на дашборде: кто сколько принёс, суммы к учёту, просрочки + выгрузка CSV.'],
  ],
  '1.7.0': [
    ['🖨 Печать чеков в PDF', 'отметьте чеки → «Печать PDF»: раскрой листов A4 на колонки, чеки реального размера 80 мм, длинные автоматически продолжаются в соседней колонке — в диалоге печати выберите «Сохранить как PDF».'],
    ['🎨 Темы: светлая, тёмная и «как на устройстве»', 'переключатель в Настройках; авто-режим следует системной настройке телефона/компьютера.'],
    ['⏰ SSL-сертификат под присмотром', 'deploy.sh сам включает таймер продления и обновляет сертификат, когда до конца срока меньше 25 дней; срок виден в итоговом отчёте.'],
    ['✨ Меньше мерцания', 'индикатор «Живое подключение» больше не мигает, тема применяется без вспышки при загрузке.'],
  ],
  '1.6.0': [
    ['🔄 Обновления работают даже при блокировках', 'проверка идёт по трём независимым каналам GitHub (api → raw → git); при сбое видна точная причина и кнопка-инструкция ручного обновления одной командой.'],
    ['🪶 Лёгкий клиент — меньше нагрузки', 'ответы сжимаются (gzip), статика кэшируется на неделю, в скрытой вкладке приложение почти не общается с сервером, а события приходят одной пачкой вместо частых перерисовок.'],
    ['🎛 Источник обновлений в интерфейсе', 'репозиторий и ветку теперь видно и можно поменять прямо в карточке «Обновления».'],
    ['📈 Тренды на дашборде', 'динамика чеков «неделя к неделе» (▲/▼) прямо в карточке показателя.'],
  ],
  '1.5.0': [
    ['📲 Установка на устройство', 'кнопка «Установить приложение»: иконка на домашнем экране Android/iPhone/iPad/ПК, полноэкранный режим (для iPhone — пошаговая инструкция).'],
    ['💾 Архивные бэкапы', 'автоматически: ежедневные (7 шт.), перед каждым обновлением (5) и АРХИВ МЕСЯЦА (12 месяцев) + создание и скачивание копий из Настроек.'],
    ['🎨 Вёрстка маппинга', 'правила — аккуратные карточки с подписями, счётчик правил, корректная раскладка на телефоне.'],
    ['⏰ Ежедневная копия БД', 'приложение само делает копию базы раз в сутки — надёжность без участия человека.'],
  ],
  '1.4.0': [
    ['🔄 Обновления из приложения', 'проверка при запуске и в Настройках, установка в один клик — без терминала: копия БД → только изменения → проверка целостности → рестарт, при сбое авто-откат. Данные и настройки сохраняются.'],
    ['⏳ «Требуют внимания»', 'на дашборде чеки, которые висят необработанными дольше 3 дней — срок отчёта по подотчётным суммам (риск НДФЛ).'],
    ['🏷 Статьи расходов', 'категория на чеке (Канцелярия, ГСМ…), подсказки, фильтр и колонка в CSV — разделение личных и рабочих покупок.'],
    ['🧾 НДС за месяц', 'сумма НДС по всем чекам месяца — прямо на дашборде.'],
    ['👥 Сводка по подотчётникам', 'кто сколько чеков и на какую сумму принёс.'],
    ['🔑 Больше прав админа', 'смена роли пользователя, временный пароль в 1 клик (сотрудник сменит сам), восстановление из архива.'],
  ],
  '1.2.0': [
    ['📥 Данные чека из сервисов', 'полная информация (магазин, ИНН, все позиции) — из API ФНС, proverkacheka.com или своего источника. Паузы 2–7 с и ротация источников — без блокировок.'],
    ['✏️ Редактирование чеков', 'бухгалтер и администратор могут изменить любые поля чека и позиции — по одному или массово; изменения попадают в 1С и CSV.'],
    ['👤 «От кого прислал»', 'чеки от сотрудников автоматически помечаются отправителем.'],
    ['🔔 Уведомления сотрудников', 'флажок «уведомить бухгалтерию» и комментарий к своему чеку.'],
    ['📱 Стабильная связь', 'исправлены «офлайн» и мерцание на телефоне.'],
  ],
  '1.3.0': [
    ['🏦 Новый источник: ОФД-ру «QR Cash»', 'официальное API ofd.ru по базе ФНС — ещё один вариант получения полного чека (tokenSecret в Настройках).'],
    ['🔗 Несколько своих источников', 'теперь можно подключить до 10 своих сервисов проверки (списком, у каждого своё «остывание»).'],
    ['🌐 Ручная проверка в 1 клик', 'в редакторе чека — кнопки открытия сервисов: proverkacheka.com, проверкачека.рф, proverka-cheka.ru, chek-pek.ru.'],
    ['🎨 Качество вёрстки', 'доработаны таблицы (липкие заголовки), модалки и сетки на телефоне, фокус с клавиатуры, безопасные отступы iOS.'],
    ['🔢 Версия теперь всегда видна и честна', 'единая версия в шапке, футере, API и кэше — обновляется из одного места.'],
  ],
};
function showWhatsNew() {
  const v = state.appVersion;
  if (!v || !WHATS_NEW[v]) return;                       // для этой версии новостей нет
  try {
    if (localStorage.getItem('ymaster_seen_version') === v) return;
    localStorage.setItem('ymaster_seen_version', v);
  } catch (e) { return; }
  // v1.12.3: вертикальные карточки — аккуратно на любом экране
  // (раньше была сетка .kv «auto 1fr»: длинные заголовки раздували колонку
  // и ломали вёрстку на телефоне)
  const rows = WHATS_NEW[v].map(([title, text]) =>
    `<div class="wn-item"><div class="wn-title">${title}</div><div class="wn-text">${text}</div></div>`).join('');
  const { slot, close } = openModal(`
    <div class="modal-title">🎉 Ямастер Чек v${esc(v)} — что нового</div>
    <div class="whats-new">${rows}</div>
    <div class="modal-actions"><button class="btn btn-primary" id="wn-ok">Понятно, работаем</button></div>`);
  slot.querySelector('#wn-ok').onclick = close;
}

// Отправка отсканированной строки на сервер (+ офлайн-режим)
async function handleScannedText(text, source) {
  const parsed = parseQrClient(text);
  if (!parsed) {
    toast('QR-код прочитан, но это не чек (нет реквизитов ФН/ФД/ФП)', 'warn');
    return;
  }
  if (navigator.onLine && state.wsOk) {
    try {
      const r = await api.post('/api/v1/receipts/scan',
                               { qr_data: text, source, company_id: scanCompanyId() });
      pushRecent(r.receipt);
      if (!r.duplicate) beep();
      return;
    } catch (e) {
      if (e.status !== 0) { toast(e.message, 'err', 'Сервер отклонил скан'); return; }
    }
  }
  // Офлайн: в очередь
  const n = offlineQueue.push({ qr_data: text, source });
  toast(`Нет связи — скан сохранён локально (в очереди: ${n})`, 'warn', 'Офлайн-режим');
  refreshBadges();
}

function beep() {
  try {
    const ctx = new (window.AudioContext || window.webkitAudioContext)();
    const osc = ctx.createOscillator();
    const gain = ctx.createGain();
    osc.connect(gain); gain.connect(ctx.destination);
    osc.frequency.value = 880; osc.type = 'sine';
    gain.gain.setValueAtTime(0.08, ctx.currentTime);
    gain.gain.exponentialRampToValueAtTime(0.0001, ctx.currentTime + 0.25);
    osc.start(); osc.stop(ctx.currentTime + 0.26);
    setTimeout(() => ctx.close(), 400);
  } catch { /* без звука */ }
}

async function flushOfflineQueue(manual = false) {
  let n = offlineQueue.size();
  if (!n) { if (manual) toast('Очередь пуста — всё синхронизировано', 'info'); return; }
  let sent = 0;
  while (offlineQueue.size() > 0) {
    const item = offlineQueue.all()[0];
    try {
      await api.post('/api/v1/receipts/scan', { qr_data: item.qr_data,
                                                source: item.source || 'web', company_id: scanCompanyId() });
      offlineQueue.shift();
      sent++;
    } catch (e) {
      if (e.status === 422) { offlineQueue.shift(); continue; }
      break;
    }
  }
  if (sent) toast(`Синхронизировано сканов: ${sent}`, 'ok', 'Офлайн-очередь');
  refreshBadges();
  if (state.view === 'scan') route(true);
}

async function handleFiles(files) {
  for (const file of files) {
    if (!file.type.startsWith('image/')) { toast(`${file.name}: не изображение`, 'warn'); continue; }
    try {
      const { valid, raws } = await decodeImageFile(file);
      if (valid.length === 0 && raws.length === 0) {
        await serverScanImage(file);
      } else if (valid.length) {
        for (const p of valid) {
          const qr = rebuildQrString(p);
          await handleScannedText(qr, 'image');
        }
      } else {
        await serverScanImage(file);
      }
    } catch (e) {
      toast(`${file.name}: ${e.message}`, 'err');
    }
  }
}

function rebuildQrString(p) {
  const sum = p.sum ? parseFloat(p.sum) : 0;
  const t = p.date ? p.date.replace(/(\d{2})\.(\d{2})\.(\d{4}) (\d{2}):(\d{2})/, '$3$2$1T$4$5') : '';
  return `t=${t}&s=${sum.toFixed(2)}&fn=${p.fn}&i=${p.fd}&fp=${p.fp}&n=${p.op}`;
}

async function serverScanImage(file) {
  const fd = new FormData();
  fd.append('file', file);
  try {
    const r = await api.post('/api/v1/receipts/scan/image', fd);
    pushRecent(r.receipt);
    toast(r.message, 'ok', 'Сервер распознал QR');
  } catch (e) {
    if (e.status === 300) {
      const variants = (e.data && e.data.detail && e.data.detail.variants) || [];
      pickVariantDialog(variants);
    } else {
      toast(e.message, 'err', file.name);
    }
  }
}

function pickVariantDialog(variants) {
  const { slot } = openModal(`
    <div class="modal-title">🔎 Найдено несколько чеков</div>
    <p style="color:var(--text-dim);margin-bottom:14px">На изображении распознано несколько разных QR-кодов. Выберите, какие добавить:</p>
    <div id="variants-box"></div>
    <div class="modal-actions"><button class="btn" data-close>Закрыть</button>
    <button class="btn btn-primary" id="btn-add-selected">Добавить выбранные</button></div>`);
  slot.querySelector('#variants-box').innerHTML = variants.map((v, i) => `
    <label class="recent-scan-item" style="cursor:pointer">
      <input type="checkbox" class="variant-check" data-i="${i}" checked style="width:auto">
      <div class="rs-main">
        <div class="rs-title">Сумма ${v.total_sum || '—'} ₽</div>
        <div class="rs-sub">ФН ${v.fn} · ФД ${v.fd} · ФП ${v.fp}</div>
      </div>
    </label>`).join('');
  slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
  slot.querySelector('#btn-add-selected').onclick = async () => {
    const idxs = [...slot.querySelectorAll('.variant-check:checked')].map(c => +c.dataset.i);
    $('#modal-root').classList.add('hidden');
    for (const i of idxs) {
      await handleScannedText(variants[i].qr_data, 'image');
    }
  };
}

function pasteDialog() {
  const { slot } = openModal(`
    <div class="modal-title">📋 Вставка QR-текста</div>
    <p class="form-hint" style="margin-bottom:12px">Вставьте строку из QR-кода чека, например:<br>
    <code class="inline">t=20250901T1200&amp;s=1500.00&amp;fn=9999078902001234&amp;i=12345&amp;fp=1234567890&amp;n=1</code></p>
    <label class="field"><span>Строка QR-кода</span>
      <textarea id="paste-text" rows="4" placeholder="t=...&s=...&fn=...&i=...&fp=...&n=1"></textarea></label>
    <div class="modal-actions">
      <button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" id="btn-paste-send">Отправить</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
  slot.querySelector('#btn-paste-send').onclick = async () => {
    const text = slot.querySelector('#paste-text').value.trim();
    if (!text) return;
    $('#modal-root').classList.add('hidden');
    await handleScannedText(text, 'web');
  };
  setTimeout(() => slot.querySelector('#paste-text').focus(), 50);
}

function manualDialog() {
  const { slot } = openModal(`
    <div class="modal-title">⌨ Ручной ввод реквизитов</div>
    <p class="form-hint" style="margin-bottom:12px">Резервный режим — когда QR повреждён. Реквизиты указаны на самом чеке.</p>
    <div class="form-grid">
      <label class="field full"><span>Дата и время чека</span>
        <input id="m-date" placeholder="22.09.2025 14:30" required></label>
      <label class="field"><span>Сумма, ₽</span>
        <input id="m-sum" type="number" step="0.01" min="0.01" placeholder="1500.00" required></label>
      <label class="field"><span>Признак расчёта</span>
        <select id="m-op"><option value="1">Приход</option><option value="2">Возврат прихода</option></select></label>
      <label class="field"><span>ФН (8–20 цифр)</span>
        <input id="m-fn" placeholder="9999078902001234" required></label>
      <label class="field"><span>ФД</span>
        <input id="m-fd" placeholder="12345" required></label>
      <label class="field full"><span>ФП / ФПД</span>
        <input id="m-fp" placeholder="1234567890" required></label>
    </div>
    <div class="modal-actions">
      <button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" id="btn-manual-send">Добавить чек</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
  slot.querySelector('#btn-manual-send').onclick = async () => {
    const payload = {
      date_time: slot.querySelector('#m-date').value.trim(),
      total_sum: parseFloat(slot.querySelector('#m-sum').value),
      fn: slot.querySelector('#m-fn').value.trim(),
      fd: slot.querySelector('#m-fd').value.trim(),
      fp: slot.querySelector('#m-fp').value.trim(),
      operation: +slot.querySelector('#m-op').value,
    };
    try {
      payload.company_id = scanCompanyId();   // v1.11.0
      const r = await api.post('/api/v1/receipts/manual', payload);
      $('#modal-root').classList.add('hidden');
      pushRecent(r.receipt);
      toast(r.message, r.duplicate ? 'warn' : 'ok');
      refreshBadges();
    } catch (e) {
      toast(e.message, 'err', 'Проверьте реквизиты');
    }
  };
}

// ==========================================================================
//  ЭКРАН: Чеки
// ==========================================================================
// v1.8.0: бейдж контроля срока авансового отчёта (п. 6.3 Указания ЦБ 3210-У)
function advanceBadge(r) {
  const dl = viewReceipts._deadline;
  if (!dl || !r.receipt_date || r.exported) return '';
  const DAY = 86400000;
  const due = new Date(r.receipt_date).getTime() + dl * DAY;
  const left = Math.ceil((due - Date.now()) / DAY);
  const dueStr = new Date(due).toLocaleDateString('ru-RU');
  if (left < 0) return `<span class="dl-badge bad" title="Срок сдачи авансового отчёта истёк">просрочен: ${-left} дн</span>`;
  if (left <= 2) return `<span class="dl-badge warn" title="Скоро истечёт срок сдачи">сдать до ${dueStr}</span>`;
  return `<span class="dl-badge ok" title="В пределах срока сдачи">сдать до ${dueStr}</span>`;
}

// v1.25.1: заполнить селектор «Кто добавил» из списка (общий для вида)
function fillCreators(sel, list, keepValue) {
  if (!sel) return;
  sel.innerHTML = '<option value="">все добавившие</option>' +
    (list || []).map(u => `<option value="${esc(u.id)}">${esc(u.name)} (${u.count})</option>`).join('');
  sel.value = keepValue || '';
}

// v1.28.0: заполнить селект «Статья расходов» используемыми статьями
function fillCats(sel, cats, keepValue) {
  if (!sel) return;
  sel.innerHTML = `<option value="">все статьи</option>` +
    (cats || []).map(c => `<option value="${esc(c.name)}">${esc(c.name)} (${c.count})</option>`).join('');
  if (keepValue && !(cats || []).some(c => c.name === keepValue)) {
    const o = document.createElement('option');
    o.value = keepValue; o.textContent = keepValue;
    sel.appendChild(o);
  }
  sel.value = keepValue || '';
}

async function viewReceipts(container) {
  const acc = isAccountant();
  // v1.8.0: срок сдачи авансового отчёта (настройка приказа руководителя)
  if (acc && !viewReceipts._deadline) {
    api.get('/api/v1/settings/app')
      .then(r => { viewReceipts._deadline = r.advance_deadline_days || 10;
                   viewReceipts._sig = null; load(); })
      .catch(() => {});
  }
  container.innerHTML = `
    <div class="glass card">
      <div class="filter-bar">
        <label class="field"><span>Поиск (ФН/ФД/ФП/сотрудник)</span>
          <input id="f-q" placeholder="номер или имя…"></label>
        <label class="field"><span>Статус</span>
          <select id="f-status"><option value="">все</option>
            <option value="new">Новые</option><option value="verifying">Проверяются</option>
            <option value="verified">Проверены</option><option value="failed">Отклонены</option></select></label>
        <label class="field"><span>Проверка ФНС</span>
          <select id="f-fns"><option value="">любая</option>
            <option value="valid">Действителен</option><option value="invalid">Недействителен</option>
            <option value="not_found">Не найден</option><option value="unknown">Не проверен</option></select></label>
        ${acc ? `
        <label class="field"><span>Выгрузка в 1С</span>
          <select id="f-exp"><option value="">все</option>
            <option value="false">Ожидают выгрузки</option><option value="true">Выгружены</option></select></label>
        <label class="field"><span>Кто добавил</span>
          <select id="f-creator"><option value="">все добавившие</option></select></label>
        <label class="field"><span>Данные чека</span>
          <select id="f-full"><option value="">все</option>
            <option value="true">📥 проверены + полные данные</option>
            <option value="false">⏳ данных не хватает — можно запросить</option></select></label>
        <label class="field"><span>Статья расходов</span>
          <select id="f-category"><option value="">все статьи</option></select></label>
        <label class="field"><span>Уведомления 🔔</span>
          <select id="f-notified"><option value="">все</option>
            <option value="true">только уведомления</option></select></label>` : ''}
        <label class="field"><span>С даты</span><input type="date" id="f-from"></label>
        <label class="field"><span>По дату</span><input type="date" id="f-to"></label>
        <button class="btn" id="btn-filter">Найти</button>
      </div>
      ${acc ? `
      <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px">
        <button class="btn btn-sm btn-ok" id="btn-bulk-verify">✓ Проверить в ФНС (выбранные)</button>
        <button class="btn btn-sm" id="btn-bulk-verify-all">✓✓ Проверить все новые</button>
        <button class="btn btn-sm" id="btn-bulk-assign">👤 Назначить сотрудника</button>
        ${isAdmin() ? '<button class="btn btn-sm" id="btn-bulk-move">🏢 Переместить в компанию</button>' : ''}
        <button class="btn btn-sm" id="btn-bulk-fetch" title="Получить полные данные выбранных чеков из сервисов (ФНС/proverkacheka). Паузы 2–7 с — без блокировок">📥 Данные сервисов</button>
        <button class="btn btn-sm btn-primary" id="btn-bulk-export">⬇ Выгрузить в 1С (выбранные)</button>
        <button class="btn btn-sm" id="btn-csv">📊 CSV-сводка</button>
        <button class="btn btn-sm" id="btn-print-pdf">🖨 Напечатать чек</button>
        <button class="btn btn-sm" id="btn-ao1">🧾 Собрать авансовый отчёт</button>
        <button class="btn btn-sm btn-bad" id="btn-bulk-delete">🗑 Удалить выбранные</button>
        <span class="form-hint" style="align-self:center" id="sel-info">не выбрано</span>
      </div>` : `
      <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:10px">
        <button class="btn btn-sm" id="btn-print-pdf">🖨 Напечатать чек (выбранные)</button>
        <span class="form-hint" style="align-self:center" id="sel-info-user">не выбрано</span>
      </div>
      <p class="form-hint" style="margin-bottom:12px">Режим пользователя: видны только ваши чеки.
      Проверка в ФНС и выгрузка в 1С выполняются бухгалтером.</p>`}
      <div class="table-wrap" id="receipts-table"><div class="skeleton" style="height:300px"></div></div>
      <div class="pagination" id="receipts-pager"></div>
    </div>`;

  viewReceipts._mounted = false;   // v1.25.1: список пока не отрисован
  // v1.25.1: применённые фильтры переживают перестроение вида (WS/навигация)
  const filters = Object.assign({
    q: '', status: '', fns_status: '', exported: '', assignee: '',
    creator: '', full_data: '',
    category: '', notified: '', date_from: '', date_to: '', page: 1,
  }, viewReceipts._filters || {});
  filters.assignee = '';   // v1.28.0: фильтр «Сотрудник» убран (есть «Кто добавил»)
  let pageInfo = { total: 0, total_sum: 0, page_size: 50 };
  // вернуть значения в поля формы — чтобы видно было, что фильтр применён
  [['#f-q', 'q'], ['#f-status', 'status'], ['#f-fns', 'fns_status'],
   ['#f-exp', 'exported'], ['#f-creator', 'creator'],
   ['#f-full', 'full_data'], ['#f-category', 'category'],
   ['#f-notified', 'notified'], ['#f-from', 'date_from'], ['#f-to', 'date_to']]
    .forEach(([sel, key]) => {
      const el = $(sel);
      if (el && filters[key]) el.value = filters[key];
    });

  async function load() {
    try {
    const p = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); });
    if (companyIdParam()) p.set('company_id', companyIdParam());   // v1.11.0
    const data = await api.get('/api/v1/receipts?' + p.toString());
    if (!Array.isArray(data.items)) data.items = [];   // v1.25.1: страховка формы ответа
    pageInfo = data;
    // v1.8.0: данные не изменились → не перерисовываем (без мерцания).
    // v1.25.1: НО если после перестроения вида список ещё не отрисован
    // (скелетон) — рисуем обязательно: раньше список «пропадал» до F5
    const rcptSig = JSON.stringify(data);
    if (viewReceipts._sig === rcptSig && viewReceipts._mounted) {
      updateSelInfo(); return;
    }
    viewReceipts._sig = rcptSig;
    viewReceipts._rows = data.items;
    // datalist имён сотрудников — из текущих строк + ранее введённые
    const names = new Set(viewReceipts._names || []);
    data.items.forEach(x => { if (x.assignee) names.add(x.assignee); });
    viewReceipts._names = [...names];
    const cats = new Set(viewReceipts._cats || []);
    data.items.forEach(x => { if (x.category) cats.add(x.category); });
    viewReceipts._cats = [...cats];
    // v1.23.0/v1.25.1: селектор «Кто добавил» — кэшируется, восстанавливается
    // после перестроения вида, сбрасывается при смене компании
    if (acc) {
      const cCompany = companyIdParam() || '';
      if (viewReceipts._creatorsCompany !== cCompany) {
        viewReceipts._creatorsCompany = cCompany;
        viewReceipts._creatorsLoaded = false;
        viewReceipts._creators = null;
      }
      const selC = $('#f-creator');
      if (selC && selC.options.length <= 1 && (viewReceipts._creators || []).length) {
        fillCreators(selC, viewReceipts._creators, viewReceipts._creatorKeep || '');
      }
      if (!viewReceipts._creatorsLoaded) {
        viewReceipts._creatorsLoaded = true;
        api.get('/api/v1/receipts/creators' + (companyIdParam() ? '?company_id=' + companyIdParam() : ''))
          .then(list => {
            viewReceipts._creators = Array.isArray(list) ? list : [];
            const sel = $('#f-creator');
            if (sel) fillCreators(sel, viewReceipts._creators, viewReceipts._creatorKeep || '');
          }).catch(() => { viewReceipts._creatorsLoaded = false; });
      }
      // v1.28.0: статьи расходов — топ используемых (кэш по компании)
      const catsCompany = companyIdParam() || '';
      if (viewReceipts._catsCompany !== catsCompany) {
        viewReceipts._catsCompany = catsCompany;
        viewReceipts._catsLoaded = false;
        viewReceipts._catsTop = null;
      }
      const selCats = $('#f-category');
      if (selCats && selCats.options.length <= 1 && (viewReceipts._catsTop || []).length) {
        fillCats(selCats, viewReceipts._catsTop, viewReceipts._catKeep || '');
      }
      if (!viewReceipts._catsLoaded) {
        viewReceipts._catsLoaded = true;
        api.get('/api/v1/receipts/categories' + (companyIdParam() ? '?company_id=' + companyIdParam() : ''))
          .then(list => {
            viewReceipts._catsTop = Array.isArray(list) ? list : [];
            const sel = $('#f-category');
            if (sel) fillCats(sel, viewReceipts._catsTop, viewReceipts._catKeep || '');
          }).catch(() => { viewReceipts._catsLoaded = false; });
      }
    }
    const el = $('#receipts-table');
    viewReceipts._mounted = true;    // v1.25.1: список отрисован — мерцание отключаем
    if (!data.items.length) {
      el.innerHTML = emptyState('🧾', 'Чеки не найдены. Отсканируйте первый на вкладке «Сканирование»');
    } else {
      el.innerHTML = `<table class="data"><thead><tr>
        <th style="width:34px"><input type="checkbox" id="sel-all" style="width:auto" title="Выбрать все на странице"></th>
        <th>Дата чека</th><th>Сумма</th><th>ФН</th><th>ФД</th><th>ФП</th>
        ${acc ? '<th>Сотрудник</th>' : ''}
        <th>Статус</th><th>ФНС</th><th>1С</th><th style="width:86px"></th></tr></thead>
        <tbody>${data.items.map(r => receiptRow(r)).join('')}</tbody></table>`;
      $$('tbody tr', el).forEach(tr => {
        tr.onclick = (e) => {
          if (e.target.tagName === 'INPUT' || e.target.tagName === 'A') return;
          receiptDrawer(tr.dataset.id);
        };
      });
      // v1.28.0: открытая карточка чека — подсветка строки восстанавливается
      if (receiptDrawer._openId) {
        const openTr = el.querySelector(`tr[data-id="${receiptDrawer._openId}"]`);
        if (openTr) openTr.classList.add('row-open');
      }
      $$('input.row-sel', el).forEach(cb => {
        cb.onchange = () => {
          cb.checked ? state.receiptsSelected.add(cb.dataset.id) : state.receiptsSelected.delete(cb.dataset.id);
          cb.closest('tr').classList.toggle('selected', cb.checked);
          updateSelInfo();
        };
      });
      const selAll = $('#sel-all');
      if (selAll) selAll.onchange = () => {
        $$('input.row-sel', el).forEach(cb => {
          cb.checked = selAll.checked;
          cb.checked ? state.receiptsSelected.add(cb.dataset.id) : state.receiptsSelected.delete(cb.dataset.id);
          cb.closest('tr').classList.toggle('selected', cb.checked);
        });
        updateSelInfo();
      };
    }
    const pager = $('#receipts-pager');
    const pages = Math.max(1, Math.ceil(data.total / data.page_size));
    pager.innerHTML = `
      <span>Стр. ${data.page} из ${pages} · всего ${fmtInt(data.total)} на ${fmtSum(data.total_sum)}</span>
      <button class="btn btn-sm" id="pg-prev" ${data.page <= 1 ? 'disabled' : ''}>←</button>
      <button class="btn btn-sm" id="pg-next" ${data.page >= pages ? 'disabled' : ''}>→</button>`;
    $('#pg-prev').onclick = () => { filters.page--; viewReceipts._filters = { ...filters }; load(); };
    $('#pg-next').onclick = () => { filters.page++; viewReceipts._filters = { ...filters }; load(); };
    updateSelInfo();
    } catch (e) {
      // v1.25.1: ошибка загрузки — внятное состояние с кнопкой «Повторить»
      // вместо вечного скелетона («пропавший» список)
      viewReceipts._mounted = false;   // списка на экране больше нет
      const el = $('#receipts-table');
      if (el) el.innerHTML = `<div class="empty-state"><span class="big-ico">📡</span>
        <b>Не удалось загрузить чеки</b>
        <span class="form-hint">${esc(e.message || 'нет связи с сервером')}</span>
        <button class="btn btn-primary" id="rc-retry" style="margin-top:10px">Повторить</button></div>`;
      const rb = $('#rc-retry');
      if (rb) rb.onclick = () => load();
    }
  }
  viewReceipts._refresh = load;   // v1.25.1: живое обновление списка без route()

  function receiptRow(r) {
    const canDel = isAdmin() || (!r.exported && r.created_by_id === state.me.id);
    const isOwner = r.created_by_id === state.me.id;
    const notifiedMark = r.notified ? ' <span title="Сотрудник уведомляет бухгалтерию">🔔</span>' : '';
    const detailsMark = r.details_source ? `<span class="form-hint" title="Источник данных: ${esc(r.details_source)}">${r.details_source === 'fns_api' ? 'ФНС' : r.details_source === 'proverkacheka' ? 'ПК' : r.details_source === 'custom' ? 'свой' : '✎'}</span>` : '';
    // v1.23.0: полные данные получены → запрос не нужен, только изменение
    // v1.25.2: полные данные есть → только «изменить»; запрос показывается,
    // только если чек ещё НЕ получил данные полностью
    const actions = acc
      ? `<td style="white-space:nowrap">
           ${r.full_data
             ? '<span class="form-hint" title="Полные данные получены; «Обновить данные» — в карточке чека (✏️)">📥✓</span>'
             : `<button class="btn btn-sm r-fetch" data-act="fetch" data-id="${r.id}"
                title="Получить полные данные чека из сервиса проверки">📥</button>`}
           <button class="btn btn-sm r-edit" data-act="edit" data-id="${r.id}" title="Изменить чек и позиции">✏️</button>
         </td>`
      : (isOwner ? `<td><button class="btn btn-sm r-notify" data-act="notify" data-id="${r.id}"
            title="Уведомить бухгалтерию (замена, возврат, комментарий)">🔔${r.notified ? '✓' : ''}</button></td>` : '<td></td>');
    return `<tr data-id="${r.id}">
      <td><input type="checkbox" class="row-sel" data-id="${r.id}" style="width:auto"
        ${state.receiptsSelected.has(r.id) ? 'checked' : ''}></td>
      <td class="cell-date">${fmtDate(r.receipt_date)}${notifiedMark}
        ${acc ? advanceBadge(r) : ''}</td>
      <td class="cell-sum">${fmtSum(r.total_sum)}${acc && r.personal_sum > 0
        ? `<div class="form-hint">к учёту: ${fmtSum((r.total_sum || 0) - r.personal_sum)}</div>` : ''}</td>
      <td class="cell-mono">${r.fn}</td><td class="cell-mono">${r.fd}</td><td class="cell-mono">${r.fp}</td>
      ${acc ? `<td>${r.assignee ? esc(r.assignee) : '<span class="form-hint">—</span>'}${r.notified ? ' <span title="Уведомление сотрудника">🔔</span>' : ''}${companyChip(r.company_id)}
        <div class="rcpt-added" title="Кто добавил чек">＋ ${esc(r.created_by_name || r.created_by || '—')}</div></td>` : ''}
      <td>${chip(r.status)}</td>
      <td>${chip(r.fns_status)} ${detailsMark}</td>
      <td>${r.exported ? '<span class="chip exported"><span class="dot"></span>да</span>' : '<span class="chip unknown"><span class="dot"></span>нет</span>'}</td>
      ${actions}
    </tr>`;
  }

  // v1.7.0: счётчик выбора в пользовательском тулбаре
  function updateUserSelInfo() {
    const el = $('#sel-info-user');
    if (el) el.textContent = state.receiptsSelected.size
      ? `выбрано: ${state.receiptsSelected.size}` : 'не выбрано';
  }
  function updateSelInfo() {
    updateUserSelInfo();
    // v1.10.0: авансовый отчёт АО-1 (бухгалтер/админ)
    const bao = $('#btn-ao1');
    if (bao && !bao.dataset.bound) {
      bao.dataset.bound = '1';
      bao.onclick = () => openAO1Modal();
    }

    // v1.11.0: перемещение выбранных чеков в другую компанию (админ)
    const bmove = $('#btn-bulk-move');
    if (bmove && !bmove.dataset.bound) {
      bmove.dataset.bound = '1';
      bmove.onclick = () => openMoveDialog();
    }

    // v1.7.0: печать выбранных чеков PDF (кнопка у бухгалтера и пользователя)
    const bpdf = $('#btn-print-pdf');
    if (bpdf && !bpdf.dataset.bound) {
      bpdf.dataset.bound = '1';
      bpdf.onclick = () => {
        const ids = [...state.receiptsSelected];
        if (!ids.length) return toast('Отметьте чеки галочками — распечатаю выбранные', 'info', '🖨');
        printReceiptsPDF(ids);
      };
    }

    const el = $('#sel-info');
    if (!el) return;
    if (!state.receiptsSelected.size) { el.textContent = 'не выбрано'; return; }
    // v1.12.3: кто добавил выбранные чеки и какие они компании
    const sel = (viewReceipts._rows || []).filter(r => state.receiptsSelected.has(r.id));
    const comps = [...new Set(sel.map(r => r.company_name).filter(Boolean))];
    const authors = [...new Set(sel.map(r => r.created_by_name).filter(Boolean))];
    el.textContent = `выбрано: ${state.receiptsSelected.size}`
      + (comps.length ? ` · компании: ${comps.join(', ')}` : '')
      + (authors.length ? ` · добавили: ${authors.join(', ')}` : '');
  }

  $('#btn-filter').onclick = () => {
    filters.q = $('#f-q').value.trim();
    filters.status = $('#f-status').value;
    filters.fns_status = $('#f-fns').value;
    if (acc) {
      filters.exported = $('#f-exp').value;
      filters.creator = $('#f-creator') ? $('#f-creator').value : '';
      viewReceipts._creatorKeep = filters.creator;
      filters.full_data = $('#f-full') ? $('#f-full').value : '';
      // v1.28.0: «Статья расходов» — выпадающий список используемых статей
      filters.category = $('#f-category') ? $('#f-category').value : '';
      viewReceipts._catKeep = filters.category;
      filters.notified = $('#f-notified') ? $('#f-notified').value : '';
    }
    filters.date_from = $('#f-from').value;
    filters.date_to = $('#f-to').value;
    filters.page = 1;
    viewReceipts._filters = { ...filters };   // v1.25.1: переживают перестроение вида
    load();
  };
  $('#f-q').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-filter').click(); });

  if (acc) {
    $('#btn-bulk-verify').onclick = async () => {
      if (!state.receiptsSelected.size) return toast('Выберите чеки галочками', 'warn');
      const r = await api.post('/api/v1/receipts/verify', { receipt_ids: [...state.receiptsSelected] });
      toast(r.message, 'info', 'Проверка ФНС');
    };
    $('#btn-bulk-verify-all').onclick = async () => {
      const r = await api.post('/api/v1/receipts/verify', { receipt_ids: [] });
      toast(r.message, 'info', 'Проверка ФНС');
    };
    $('#btn-bulk-assign').onclick = () => bulkAssignDialog();
    $('#btn-bulk-fetch').onclick = async () => {
      if (!state.receiptsSelected.size) return toast('Выберите чеки галочками', 'warn');
      const r = await api.post('/api/v1/receipts/fetch-details',
                               { receipt_ids: [...state.receiptsSelected] });
      toast(r.message, 'info', 'Получение данных');
    };
    $('#btn-bulk-export').onclick = async () => {
      if (!state.receiptsSelected.size) return toast('Выберите чеки галочками', 'warn');
      try {
        const { blob, filename } = await api.download('/api/v1/receipts/export', {
          receipt_ids: [...state.receiptsSelected], format: 'json',
        });
        downloadBlob(blob, filename);
        toast(`Файл ${filename} сформирован, чеки помечены как выгруженные`, 'ok', 'Экспорт в 1С');
        load();
      } catch (e) { toast(e.message, 'err'); }
    };
    $('#btn-csv').onclick = async () => {
      try {
        const ids = [...state.receiptsSelected];
        const { blob, filename } = await api.download('/api/v1/receipts/export-csv', { receipt_ids: ids });
        downloadBlob(blob, filename);
        toast(`CSV ${filename} скачан (Excel-совместимый)`, 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
    $('#btn-bulk-delete').onclick = async () => {
      if (!state.receiptsSelected.size) return toast('Выберите чеки галочками', 'warn');
      if (!confirm(`Удалить чеков: ${state.receiptsSelected.size}?`)) return;
      for (const id of state.receiptsSelected) {
        try { await api.del('/api/v1/receipts/' + id); } catch (e) { toast(e.message, 'err'); }
      }
      state.receiptsSelected.clear();
      toast('Удаление выполнено', 'ok');
      load();
    };
  }

  // --- v1.2.0: действия в строках чеков (делегирование) ---
  const tbody = $('#receipts-table');
  if (tbody) {
    tbody.onclick = async (e) => {
      const btn = e.target.closest('button[data-act]');
      if (!btn) return;
      const id = btn.dataset.id;
      const row = (viewReceipts._rows || []).find(x => x.id === id);
      if (btn.dataset.act === 'fetch') {
        btn.disabled = true; btn.textContent = '⏳';
        try {
          const r = await api.post(`/api/v1/receipts/${id}/fetch-details`);
          toast(r.message, 'info', 'Получение данных');
        } catch (err) { toast(err.message, 'err'); }
        setTimeout(() => { btn.disabled = false; btn.textContent = '📥'; }, 4000);
      } else if (btn.dataset.act === 'edit') {
        if (row) openEditReceipt(row, () => load());
      } else if (btn.dataset.act === 'notify') {
        if (row) openNotifyDialog(row, () => load());
      }
    };
  }

  function bulkAssignDialog() {
    if (!state.receiptsSelected.size) return toast('Выберите чеки галочками', 'warn');
    const { slot } = openModal(`
      <div class="modal-title">👤 Назначить сотрудника</div>
      <p class="form-hint" style="margin-bottom:12px">Сотрудник (подотчётное лицо) попадёт в авансовый
      отчёт при выгрузке в 1С. Выбрано чеков: <b>${state.receiptsSelected.size}</b></p>
      <label class="field"><span>ФИО сотрудника</span>
        <input id="ba-name" placeholder="Иванова Анна Петровна" list="ba-list">
        <datalist id="ba-list">
          ${[...new Set([...(viewReceipts._names || [])])].map(n => `<option value="${esc(n)}">`).join('')}
        </datalist></label>
      <div class="modal-actions">
        <button class="btn" data-close>Отмена</button>
        <button class="btn btn-primary" id="ba-save">Назначить</button>
      </div>`);
    slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
    slot.querySelector('#ba-save').onclick = async () => {
      const name = slot.querySelector('#ba-name').value.trim();
      if (!name) return toast('Введите ФИО сотрудника', 'warn');
      try {
        const r = await api.post('/api/v1/receipts/bulk-assign', {
          receipt_ids: [...state.receiptsSelected], assignee: name,
        });
        $('#modal-root').classList.add('hidden');
        toast(r.message, 'ok');
        viewReceipts._names = [...(viewReceipts._names || []), name];
        load();
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  await load();
}

// ==========================================================================
//  v1.7.0: Печать чеков PDF — раскрой листов A4 под чеки 80 мм
//  Чеки печатаются реального размера (как из кассового аппарата), длинные
//  разрезаются по строкам позиций и продолжаются в соседней колонке.
//  Геометрия — в app/static/js/printpack.js (чистые функции, тесты node).
// ==========================================================================
function rcptDate(v) {
  try { const d = new Date(v); return d.toLocaleDateString('ru-RU') + ' ' + d.toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' }); }
  catch { return String(v || ''); }
}

function rcptBlocks(r, qrUrl, qrFail) {
  const op = Number(r.operation) === 2 ? 'ВОЗВРАТ ПРИХОДА' : 'ПРИХОД';
  const rule = '<div class="rcpt-rule"></div>';
  const blocks = [];
  blocks.push(`<div class="rcpt-head">${esc(r.merchant_name || 'КАССОВЫЙ ЧЕК')}</div>`);
  if (r.merchant_inn || r.merchant_address) {
    blocks.push(`<div class="rcpt-sub">${r.merchant_inn ? 'ИНН ' + esc(r.merchant_inn) : ''}${r.merchant_inn && r.merchant_address ? ' · ' : ''}${esc(r.merchant_address || '')}</div>`);
  }
  blocks.push(rule);
  blocks.push(`<div style="text-align:center"><b>${op}</b> · ${rcptDate(r.receipt_date)}</div>`);
  blocks.push(rule);
  const items = r.items || [];
  if (items.length) {
    for (const it of items) {
      const qty = Number(it.quantity) || 0;
      const price = Number(it.price) || 0;
      const sum = Number(it.sum ?? qty * price) || 0;
      blocks.push(`<div class="r-item"><div class="r-name">${esc(it.name || 'товар')}</div>
        <div class="r-line"><span>${qty} × ${price.toFixed(2)}</span><span>${sum.toFixed(2)}</span></div></div>`);
    }
  } else {
    blocks.push('<div class="rcpt-part">(позиции чека не загружены — получите данные сервисов)</div>');
  }
  blocks.push(rule);
  blocks.push(`<div class="r-line" style="font-weight:700"><span>ИТОГ:</span><span>${Number(r.total_sum || 0).toFixed(2)} ₽</span></div>`);
  if (r.cash_sum || r.ecash_sum) {
    blocks.push(`<div class="r-line rcpt-sub"><span>${r.cash_sum ? 'наличные: ' + Number(r.cash_sum).toFixed(2) : ''}</span><span>${r.ecash_sum ? 'безнал: ' + Number(r.ecash_sum).toFixed(2) : ''}</span></div>`);
  }
  blocks.push(rule);
  const fnsMap = { valid: '✓ проверен ФНС', invalid: '✗ НЕ действителен', not_found: '? не найден в ФНС', unknown: 'не проверялся' };
  blocks.push(`<div class="rcpt-foot">ФН ${esc(r.fn || '—')} · ФД ${esc(r.fd || '—')} · ФП ${esc(r.fp || '—')}<br>${fnsMap[r.fns_status] || ''}<br>Ямастер Чек · ymaster.ru</div>`);
  // v1.10.0: фискальный QR — как на настоящем кассовом чеке
  if (qrUrl) {
    blocks.push(`<div style="text-align:center;margin-top:1.5mm"><img src="${qrUrl}" alt="Фискальный QR чека" style="width:26mm;height:26mm;margin:0 auto"></div>`);
  } else if (qrFail && r.fn) {
    // v1.24.1: код не получен (нет реквизитов/ошибка) — место под код остаётся помечено
    blocks.push('<div class="rcpt-part" style="margin-top:1.5mm">(фискальный QR не получен)</div>');
  }
  return blocks;
}

// v1.24.1: чеки печатаются С фискальным QR-кодом — дожидаемся загрузки
// всех картинок раскроя (PNG с сервера через object URL) до вызова
// диалога печати, иначе браузер печатает лист раньше, чем отрисуются коды.
function whenImagesReady(root) {
  const imgs = Array.from(root.querySelectorAll('img'));
  return Promise.all(imgs.map(im => new Promise(res => {
    if (im.complete && im.naturalWidth) return res();
    const done = () => { im.onload = null; im.onerror = null; res(); };
    im.onload = done; im.onerror = done;
    setTimeout(done, 4000);            // страховка: не зависаем дольше 4 с
  })));
}

// v1.24.0: @page нельзя задать селектором — стиль печати подменяется
// перед window.print(): чеки — поля 0 (раскрой сам позиционирует куски),
// АО-1 — лево 20 мм, право 15 мм, верх 10 мм, низ 10 мм.
function setPrintPageMargin(css, ao1) {
  let el = document.getElementById('ym-page-style');
  if (!el) {
    el = document.createElement('style');
    el.id = 'ym-page-style';
    document.head.appendChild(el);
  }
  el.textContent = css;
  document.body.classList.toggle('print-ao1', !!ao1);
}

async function printReceiptsPDF(ids) {
  const { close } = openModal(`<div class="modal-title">🖨 Напечатать чек</div>
    <p class="form-hint" id="pp-status">Готовлю раскрой листов A4: загружаю фискальные QR-коды…</p><div class="spinner"></div>`);
  try {
    const data = await api.get('/api/v1/receipts?ids=' + ids.join(','));
    if (!data.items.length) { close(); return toast('Чеки не найдены', 'err'); }
    const MM = 25.4 / 96;                                   // CSS px → мм
    let root = document.getElementById('print-root');
    if (root) root.remove();
    root = document.createElement('div');
    root.id = 'print-root';
    document.body.appendChild(root);
    const meas = document.createElement('div');
    meas.style.cssText = `width:${RCPT_W}mm;position:absolute;left:0;top:0;`; // v1.24.0: меряем в проектной ширине 80 мм
    root.appendChild(meas);

    // 1) Фискальные QR-коды чеков (PNG с сервера) — чеки печатаются С КОДОМ:
    //    качаем для всех выбранных чеков (v1.24.1: без лимита 60), с прогрессом
    const qrUrls = {}, qrFail = {};
    const status = document.getElementById('pp-status');
    const qrList = data.items.slice(0, 300);           // страховка от гигантских выборок
    for (let i = 0; i < qrList.length; i++) {
      const r = qrList[i];
      if (status) status.textContent = `Загружаю фискальные QR: ${i + 1} из ${qrList.length}…`;
      try {
        const { blob } = await api.download(`/api/v1/receipts/${r.id}/qr.png`);
        qrUrls[r.id] = URL.createObjectURL(blob);
      } catch { qrFail[r.id] = true; }                 // чек без реквизитов — пометка на листе
    }

    // 2) Рендерим чеки и режем длинные по строкам позиций
    const pieces = [];                                      // { html, h }
    for (const r of data.items) {
      const blocks = rcptBlocks(r, qrUrls[r.id], qrFail[r.id]);   // v1.24.1: с QR / пометкой
      const full = document.createElement('div');
      full.className = 'rcpt';
      full.innerHTML = blocks.join('');
      meas.appendChild(full);
      let hFull = full.getBoundingClientRect().height * MM;
      hFull *= SCALE;                                   // v1.24.0: эффективная высота на листе
      if (hFull <= COL_H) {
        pieces.push({ html: full.outerHTML, h: hFull });
        meas.removeChild(full);
        continue;
      }
      // длинный: режем по блокам, части продолжаются в соседней колонке
      const nodes = [...full.children];
      const hs = nodes.map(n => n.getBoundingClientRect().height * MM * SCALE); // v1.24.0: позиционная нарезка в масштабе листа
      meas.removeChild(full);
      const parts = splitBlocks(hs);
      for (let pi = 0; pi < parts.length; pi++) {
        const [a, b] = parts[pi];
        const part = document.createElement('div');
        part.className = 'rcpt';
        let html = pi > 0
          ? `<div class="rcpt-part">— продолжение чека ФД ${esc(r.fd || '')} —</div>` : '';
        html += nodes.slice(a, b).map(n => n.outerHTML).join('');
        if (pi < parts.length - 1) {
          html += `<div class="rcpt-part">↓ продолжение — часть ${pi + 2} из ${parts.length} ↓</div>`;
        }
        part.innerHTML = html;
        meas.appendChild(part);
        pieces.push({ html: part.outerHTML, h: part.getBoundingClientRect().height * MM });
        meas.removeChild(part);
      }
    }

    // 2) Раскрой по страницам A4 (каждый кусок ЦЕЛИКОМ на странице)
    setPrintPageMargin('@page { size: A4 portrait; margin: 0; }', false); // v1.24.0
    const pages = packCut(pieces.map(p => p.h));

    // 3) Собираем страницы печати
    root.innerHTML = pages.map(page =>
      `<div class="print-page">${page.map(pl =>
        `<div class="print-piece" style="left:${columnX(pl.col)}mm;top:${pl.y}mm">${pieces[pl.piece].html}</div>`
      ).join('')}</div>`).join('');
    await whenImagesReady(root);          // v1.24.1: QR должны быть в листе, не в «пустоте»
    close();
    const withQr = Object.keys(qrUrls).length;
    toast(`Раскрой готов: ${pages.length} стр. × 3 колонки · ${data.items.length} чек · QR на ${withQr}. В диалоге печати выберите «Сохранить как PDF»`, 'ok', '🖨');
    window.print();
    window.addEventListener('afterprint', () => {
      const el = document.getElementById('print-root');
      if (el) el.remove();
    }, { once: true });
  } catch (e) {
    close();
    toast(e.message, 'err');
  }
}

// ==========================================================================
//  v1.10.0: АВАНСОВЫЙ ОТЧЁТ АО-1 (бухгалтер/админ).
//  Шаблон — редактируемый; при формировании заполняется автоматически
//  по чекам сотрудника за период. Вывод: страница A4 → «Сохранить как PDF».
//  Форма по структуре соответствует классическому АО-1 (Указание ЦБ 3210-У:
//  подотчётное лицо, назначение аванса, приложенные чеки, итоги, подписи).
// ==========================================================================
const AO_DOC_NAMES = { 1: 'приход', 2: 'возврат прихода' };

async function openAO1Modal(opts = {}) {
  // v1.14.0: «Собрать авансовый отчёт» — период любой, сотрудник или ВСЕ
  // сотрудники компании, выбранные чеки, CSV и печать (АО-1 / сводная форма).
  // opts: { companyId } — запуск из карточки компании (админ).
  const now = new Date();
  const ym = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  const mFrom = `${ym}-01`, mTo = `${ym}-${new Date(now.getFullYear(), now.getMonth() + 1, 0).getDate()}`;
  const names = [...(viewReceipts._names || [])];
  const isAdmin = state.me?.role === 'admin';
  const selCnt = state.receiptsSelected.size;
  const comps = (state.companies || []).filter(c => c.is_active);
  const preset = opts.companyId || '';

  const { slot } = openModal(`
    <div class="modal-title">🧾 Собрать авансовый отчёт</div>
    <p class="form-hint" style="margin-bottom:10px">Чекы собираются автоматически:
    выберите период и сотрудников — получите готовую форму АО с итогами,
    проверкой статусов ФНС и выгрузкой в CSV/Excel.</p>
    <div class="form-grid">
      <label class="field"><span>Организация</span>
        <input id="ao-org" value="${esc(state.me?.organization || 'ООО «Ямастер»')}"></label>
      <label class="field"><span>Номер документа</span>
        <input id="ao-num" value="АО-${ym}-${String(Math.floor(Math.random() * 90) + 10)}"></label>
      <label class="field"><span>Дата составления</span>
        <input id="ao-date" type="date" value="${now.toISOString().slice(0, 10)}"></label>
      <label class="field"><span>Период: с</span>
        <input id="ao-from" type="date" value="${mFrom}"></label>
      <label class="field"><span>Период: по</span>
        <input id="ao-to" type="date" value="${mTo}"></label>
      ${isAdmin ? `<label class="field"><span>Компания</span>
        <select id="ao-company"><option value="">— все компании —</option>
          ${comps.map(c => `<option value="${c.id}" ${c.id === preset ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
        </select></label>` : ''}
      <label class="field"><span>Подотчётное лицо (пусто — все)</span>
        <input id="ao-assignee" list="ao-names" placeholder="Иванов Иван">
        <datalist id="ao-names">${names.map(n => `<option value="${esc(n)}">`).join('')}</datalist></label>
      <label class="field"><span>Должность подотчётного</span>
        <input id="ao-post" placeholder="менеджер"></label>
      <label class="field"><span>Структурное подразделение</span>
        <input id="ao-dept" placeholder="администрация"></label>
      <label class="field"><span>Назначение аванса</span>
        <input id="ao-purpose" value="На хозяйственные расходы"></label>
      <label class="field"><span>Получено из кассы, ₽</span>
        <input id="ao-cash" type="number" min="0" step="0.01" value="0"></label>
      <label class="field"><span>Получено на карту, ₽</span>
        <input id="ao-card" type="number" min="0" step="0.01" value="0"></label>
      <label class="field"><span>РКО (№ и дата, выдача из кассы)</span>
        <input id="ao-rko" placeholder="№ 12 от 05.10.2026"></label>
      <label class="field"><span>Платёжное поручение (№ и дата)</span>
        <input id="ao-pay" placeholder="№ 45 от 05.10.2026"></label>
      <label class="field"><span>Счёт Дт (аванс)</span>
        <input id="ao-dt" value="71.01"></label>
      <label class="field"><span>Счёт Кт (выдача)</span>
        <input id="ao-kt" value="50.01"></label>
      <label class="field"><span>Счёт учёта расходов</span>
        <input id="ao-acc" value="44.01"></label>
      <label class="field"><span>Бухгалтер (ФИО для подписи)</span>
        <input id="ao-buh" value="${esc(state.me?.full_name || '')}"></label>
      <label class="field"><span>Руководитель (ФИО для «Утверждаю»)</span>
        <input id="ao-head" placeholder="Иванов И.И."></label>
    </div>
    <div style="display:flex;gap:16px;flex-wrap:wrap;margin:8px 0 4px">
      ${selCnt ? `<label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="ao-onlysel" style="width:auto">
        только выбранные чеки (${selCnt})</label>` : ''}
      <label style="display:flex;gap:6px;align-items:center"><input type="checkbox" id="ao-onlyvalid" style="width:auto">
        только подтверждённые ФНС</label>
    </div>
    <div class="modal-actions">
      <button class="btn" data-close>Отмена</button>
      <button class="btn btn-primary" id="ao-make">🧾 Собрать отчёт</button>
    </div>`);
  slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
  slot.querySelector('#ao-make').onclick = async (e) => {
    const btn = e.target;
    const dFrom = slot.querySelector('#ao-from').value;
    const dTo = slot.querySelector('#ao-to').value;
    if (!dFrom || !dTo) return toast('Укажите период: с … по …', 'warn');
    btn.disabled = true; btn.textContent = '…собираю';
    try {
      const body = {
        date_from: dFrom, date_to: dTo,
        assignee: slot.querySelector('#ao-assignee').value.trim() || null,
        only_valid: slot.querySelector('#ao-onlyvalid').checked,
      };
      const cSel = slot.querySelector('#ao-company');
      if (cSel && cSel.value) body.company_id = cSel.value;
      if (slot.querySelector('#ao-onlysel')?.checked) body.receipt_ids = [...state.receiptsSelected];
      const rep = await api.post('/api/v1/receipts/advance-report', body);
      if (!rep.rows.length) {
        btn.disabled = false; btn.textContent = '🧾 Собрать отчёт';
        return toast('За период чеков нет — измените фильтры', 'warn');
      }
      aoShowPreview(slot, rep, {
        'ao-org': slot.querySelector('#ao-org').value,
        'ao-num': slot.querySelector('#ao-num').value,
        'ao-date': slot.querySelector('#ao-date').value,
        'ao-post': slot.querySelector('#ao-post').value,
        'ao-purpose': slot.querySelector('#ao-purpose').value,
        'ao-buh': slot.querySelector('#ao-buh').value,
        'ao-dept': slot.querySelector('#ao-dept') ? slot.querySelector('#ao-dept').value : '',
        'ao-head': slot.querySelector('#ao-head') ? slot.querySelector('#ao-head').value : '',
        'ao-cash': slot.querySelector('#ao-cash') ? slot.querySelector('#ao-cash').value || '0' : '0',
        'ao-card': slot.querySelector('#ao-card') ? slot.querySelector('#ao-card').value || '0' : '0',
        'ao-dt': slot.querySelector('#ao-dt') ? slot.querySelector('#ao-dt').value.trim() : '71.01',
        'ao-kt': slot.querySelector('#ao-kt') ? slot.querySelector('#ao-kt').value.trim() : '50.01',
        'ao-acc': slot.querySelector('#ao-acc') ? slot.querySelector('#ao-acc').value.trim() : '44.01',
        'ao-rko': slot.querySelector('#ao-rko') ? slot.querySelector('#ao-rko').value.trim() : '',   // v1.24.0
        'ao-pay': slot.querySelector('#ao-pay') ? slot.querySelector('#ao-pay').value.trim() : '',   // v1.24.0
      });
    } catch (err) {
      toast(err.message, 'err');
      btn.disabled = false; btn.textContent = '🧾 Собрать отчёт';
    }
  };
}

// --- v1.14.0: предпросмотр собранного отчёта -------------------------------
function aoShowPreview(slot, rep, meta) {
  aoShowPreview._meta = meta;   // v1.23.0: поля формы для JSON
  const sum = (x) => (Math.round(x * 100) / 100).toFixed(2);
  const many = rep.by_assignee.length > 1 ||
    (rep.by_assignee.length === 1 && rep.by_assignee[0].name === '—');
  slot.innerHTML = `
    <div class="modal-title">🧾 Отчёт собран — ${rep.total.count} чек(ов) на ${sum(rep.total.sum)} ₽</div>
    ${rep.invalid_count ? `<div class="info-callout" style="margin-bottom:8px">⚠ В отчёте
      <b>${rep.invalid_count}</b> чек(ов) со статусом «Недействителен» —
      исключите их из учёта или перепроверьте.</div>` : ''}
    ${rep.truncated ? `<div class="info-callout" style="margin-bottom:8px">⚠ Показаны первые
      1000 чеков — сузьте период.</div>` : ''}
    <div class="form-hint">Период: ${esc(rep.period.from)} — ${esc(rep.period.to)} ·
      ${esc(rep.company ? rep.company.name : 'все компании')}</div>
    <table style="width:100%;border-collapse:collapse;margin:10px 0;font-size:13px">
      <thead><tr style="text-align:left;color:var(--text-faint)">
        <th style="padding:4px">Подотчётник</th><th>Чеков</th><th>Сумма, ₽</th></tr></thead>
      <tbody>${rep.by_assignee.map(a => `<tr>
        <td style="padding:4px">${esc(a.name)}</td><td>${a.count}</td>
        <td>${sum(a.sum)}</td></tr>`).join('')}
        <tr><td style="padding:4px"><b>ИТОГО</b></td><td><b>${rep.total.count}</b></td>
        <td><b>${sum(rep.total.sum)}</b></td></tr></tbody>
    </table>
    <div class="modal-actions" style="justify-content:space-between;flex-wrap:wrap">
      <button class="btn" id="ao-back">← Изменить параметры</button>
      <div style="display:flex;gap:8px;flex-wrap:wrap">
        <button class="btn" id="ao-csv">⬇ CSV (Excel)</button>
        <button class="btn" id="ao-json" title="Структура документа в стиле 1С — для интеграции">⬇ JSON для 1С</button>
        <button class="btn btn-primary" id="ao-print">🖨 Печать / PDF</button>
      </div>
    </div>`;
  slot.querySelector('#ao-back').onclick = () => { $('#modal-root').classList.add('hidden'); openAO1Modal(); };
  slot.querySelector('#ao-csv').onclick = () => aoCsvDownload(rep);
  // v1.23.0: JSON в стиле 1С (интеграция готова, поля «как в 1С»)
  slot.querySelector('#ao-json').onclick = () => {
    const meta2 = aoShowPreview._meta || {};
    const j = ao1Json(rep, meta2, Number(meta2['ao-cash'] || 0), Number(meta2['ao-card'] || 0));
    const blob = new Blob([JSON.stringify(j, null, 2)], { type: 'application/json;charset=utf-8' });
    downloadBlob(blob, `avansovy-otchet-1c-${rep.period.from}_${rep.period.to}.json`);
    toast('JSON для 1С скачан — структура готова к загрузке', 'ok', '⬇ 1С');
  };
  slot.querySelector('#ao-print').onclick = () => {
    if (many) aoMultiPrint(rep, meta); else ao1Print(slot, rep, { ...meta, assignee: rep.by_assignee[0].name, month: rep.period.from.slice(0, 7) });
  };
}

// --- v1.14.0: CSV авансового отчёта (Excel-совместимый, с BOM) -------------
function aoCsvDownload(rep) {
  const sum = (x) => (Math.round(x * 100) / 100).toFixed(2).replace('.', ',');
  const dt = (s) => s ? String(s).replace('T', ' ').slice(0, 16) : '';
  const fnsNames = { valid: 'Действителен', invalid: 'Недействителен',
                     not_found: 'Не найден', unknown: 'Не проверен' };
  const ops = { 1: 'Приход', 2: 'Возврат' };
  const q = (v) => '"' + String(v == null ? '' : v).replace(/"/g, '""') + '"';
  const lines = [['№', 'Дата и время', 'Подотчётник', 'Продавец', 'ИНН продавца',
                  'Статья расходов', 'ФН', 'ФД', 'ФП', 'Операция', 'Статус ФНС', 'Сумма, ₽']
                 .join(';')];
  rep.rows.forEach((r, i) => {
    lines.push([i + 1, q(dt(r.receipt_date)), q(r.assignee), q(r.merchant_name || 'Товары (по чеку)'),
      q(r.merchant_inn), q(r.category || '—'), q(r.fn), q(r.fd), q(r.fp),
      q(ops[r.operation] || ''), q(fnsNames[r.fns_status] || ''), sum(r.total_sum)].join(';'));
  });
  lines.push(['', '', '', '', '', '', '', '', '', '', 'ИТОГО', sum(rep.total.sum)].join(';'));
  const blob = new Blob(['\ufeff' + lines.join('\r\n')], { type: 'text/csv;charset=utf-8' });
  downloadBlob(blob, `avansovy-otchet-${rep.period.from}_${rep.period.to}.csv`);
  toast(`CSV отчёта скачан: ${rep.total.count} чек(ов)`, 'ok', '⬇');
}

// --- v1.14.0: сводная печать (несколько подотчётников) ----------------------
function aoMultiPrint(rep, meta) {
  const sum = (x) => (Math.round(x * 100) / 100).toFixed(2);
  const ops = { 1: ' (возврат)' };
  let rows = '';
  let n = 0;
  for (const a of rep.by_assignee) {
    const group = rep.rows.filter(r => r.assignee === a.name);
    for (const r of group) {
      n++;
      rows += `<tr>
        <td class="ao-c">${n}</td>
        <td class="ao-c">${fmtDate(r.receipt_date)}</td>
        <td>${esc(r.assignee)}</td>
        <td>${esc(r.merchant_name || 'Товары (по чеку)')}</td>
        <td class="ao-c">${esc(r.category || '—')}</td>
        <td class="ao-c">ФД ${esc(r.fd || '—')}${ops[r.operation] || ''}</td>
        <td class="ao-r">${sum(r.total_sum)}</td></tr>`;
    }
    rows += `<tr class="ao-sub"><td colspan="6">Подытог — ${esc(a.name)}
      (${a.count} чек.)</td><td class="ao-r">${sum(a.sum)}</td></tr>`;
  }
  let root = document.getElementById('print-root');
  if (root) root.remove();
  root = document.createElement('div');
  root.id = 'print-root';
  root.innerHTML = `
    <div class="print-page ao-page">
      <div class="ao-head">
        <div class="ao-org"><b>${esc(meta['ao-org'] || '')}</b></div>
        <div class="ao-docnum">Сводный авансовый отчёт · Приложение №&nbsp;${rep.total.count} · документов<br>
          <b>${esc(meta['ao-num'] || '')}</b> от <b>${esc(meta['ao-date'] || '')}</b></div>
      </div>
      <h2 class="ao-title">АВАНСОВЫЙ ОТЧЁТ (сводный)</h2>
      <table class="ao-meta">
        <tr><td>Организация:</td><td><b>${esc(rep.company ? rep.company.name : '—')}</b></td>
            <td>ИНН:</td><td>${esc(rep.company?.inn || '—')}</td></tr>
        <tr><td>Период:</td><td colspan="3">${esc(rep.period.from)} — ${esc(rep.period.to)}</td></tr>
        <tr><td>Назначение аванса:</td><td colspan="3">${esc(meta['ao-purpose'] || '')}</td></tr>
      </table>
      <table class="ao-table">
        <thead><tr><th>№</th><th>Дата чека</th><th>Подотчётник</th>
          <th>Наименование (продавец)</th><th>Статья</th><th>Документ</th><th>Сумма, ₽</th></tr></thead>
        <tbody>${rows}</tbody>
        <tfoot><tr><td colspan="6" class="ao-r"><b>ИТОГО</b></td>
          <td class="ao-r"><b>${sum(rep.total.sum)}</b></td></tr></tfoot>
      </table>
      <p class="ao-note">Приложено кассовых чеков — <b>${rep.total.count}</b> шт. на сумму
        <b>${sum(rep.total.sum)} ₽</b>. Подотчётных лиц: <b>${rep.by_assignee.length}</b>.</p>
      <table class="ao-sign">
        <tr><td>Проверил(а) бухгалтер</td><td class="ao-line">${esc(meta['ao-buh'] || '')}</td>
            <td>Подпись</td><td class="ao-line"></td></tr>
      </table>
      <p class="ao-foot">Сформировано в «Ямастер Чек» · ymaster.ru · ${new Date().toLocaleString('ru-RU')}</p>
    </div>`;
  document.body.appendChild(root);
  toast(`Сводный АО: ${rep.total.count} чек(ов), ${rep.by_assignee.length} подотчётник(ов) — «Сохранить как PDF»`, 'ok', '🧾');
  window.print();
}

// ==========================================================================
// v1.23.0: ПЕЧАТЬ АО-1 по унифицированной форме (Постановление Госкомстата
// РФ от 01.08.2001 № 55, ОКУД 0302001): лицевая сторона (реквизиты
// организации — из КАРТОЧКИ ПРЕДПРИЯТИЯ, расчёт аванса, бухгалтерская
// запись, утверждение, расписка) + оборотная сторона (документы расходов).
// Данные структурированы в стиле 1С — готово к интеграции (JSON для 1С).
// ==========================================================================
function ao1Money(n) { return (Math.round((Number(n) || 0) * 100) / 100).toFixed(2); }

function ao1Json(rep, meta, cash, card) {
  const total = rep.rows.reduce((a, r) => a + (r.total_sum || 0), 0);
  const org = (rep.requisites && rep.requisites['Организация']) || {};
  const spent = total;
  const got = (Number(cash) || 0) + (Number(card) || 0);
  const rest = Math.max(0, Math.round((got - spent) * 100) / 100);
  const over = Math.max(0, Math.round((spent - got) * 100) / 100);
  return {
    АвансовыйОтчет: {
      НомерДокумента: meta['ao-num'] || '',
      ДатаДокумента: meta['ao-date'] || '',
      ОтчетныйПериод: meta.month || (rep.period ? rep.period.from : ''),
      Организация: {
        НаименованиеПолное: org['НаименованиеПолное'] || meta['ao-org'] || '',
        НаименованиеСокращенное: org['НаименованиеСокращенное'] || '',
        ИНН: org['ИНН'] || '', КПП: org['КПП'] || '',
        ОГРН: org['ОГРН'] || '', ОКПО: org['ОКПО'] || '',
        Адрес: org['Адрес'] || '', Телефон: org['Телефон'] || '',
        Руководитель: org['Руководитель'] || '',
      },
      СтруктурноеПодразделение: meta['ao-dept'] || '',
      ПодотчетноеЛицо: rep.person || { ФИО: meta.assignee || '' },
      НазначениеАванса: meta['ao-purpose'] || '',
      Суммы: {
        ПолученоИзКассы: ao1Money(cash), ПолученоНаКарту: ao1Money(card),
        ИтогоПолучено: ao1Money(got), Израсходовано: ao1Money(spent),
        Остаток: ao1Money(rest), Перерасход: ao1Money(over),
        СчетДт: meta['ao-dt'] || '71.01', СчетКт: meta['ao-kt'] || '50.01',
        РКО: meta['ao-rko'] || '',                       // v1.24.0: № и дата РКО (выдача из кассы)
        ПлатёжноеПоручение: meta['ao-pay'] || '',        // v1.24.0: № и дата п/п (зачисление на карту)
      },
      Документы: rep.rows.map((r, i) => ({
        НомерСтроки: i + 1,
        ДатаДокумента: r.receipt_date ? String(r.receipt_date).slice(0, 10) : '',
        НомерДокумента: 'ФД №' + (r.fd || '—'),
        НаименованиеДокумента: 'Кассовый чек',
        НаименованиеРасхода: (r.merchant_name || 'Товары (по чеку)') +
          (r.category ? ' — ' + r.category : ''),
        Продавец: r.merchant_name || '', ИННПродавца: r.merchant_inn || '',
        ФН: r.fn || '', ФД: r.fd || '', ФП: r.fp || '',
        Сумма: ao1Money(r.total_sum),
        СтатьяРасходов: r.category || '',
        СчетУчета: meta['ao-acc'] || '44.01',
      })),
      Итого: { Сумма: ao1Money(total), КоличествоДокументов: rep.rows.length,
               Листов: Math.max(1, Math.ceil(rep.rows.length / 18)) },
      ГлавныйБухгалтер: meta['ao-buh'] || '',
      Руководитель: meta['ao-head'] || '',
      Источник: 'Ямастер Чек v' + (state.appVersion || ''),
    },
  };
}

function ao1Print(f, rep, meta) {
  const items = rep.rows;
  const sum = ao1Money;
  const total = items.reduce((a, r) => a + (r.total_sum || 0), 0);
  const cash = Number(meta['ao-cash'] || 0), card = Number(meta['ao-card'] || 0);
  const got = cash + card;
  const rest = Math.max(0, Math.round((got - total) * 100) / 100);
  const over = Math.max(0, Math.round((total - got) * 100) / 100);
  const org = (rep.requisites && rep.requisites['Организация']) || {};
  const person = rep.person || {};
  const fio = person['ФИО'] || meta.assignee || '—';
  const dash = '—';
  const dtRu = (s) => s ? fmtDate(s) : dash;
  // v1.24.0: оборот — по отчёту/принято к учёту = СУММЫ, дебет = счёт учёта;
  // реквизиты чека полные (дата, ФД, ФН/ФП) — обязательны для налоговой
  const rowsBack = items.map((r, i) => `<tr>
      <td class="ao1-c">${i + 1}</td>
      <td class="ao1-c">${dtRu(r.receipt_date)}</td>
      <td class="ao1-c">ФД №${esc(r.fd || dash)}</td>
      <td class="ao1-c">ФН ${esc(r.fn || dash)}<br>ФП ${esc(r.fp || dash)}</td>
      <td>Кассовый чек${r.merchant_name ? ': ' + esc(r.merchant_name) : ''}${r.category ? ' — ' + esc(r.category) : ''}</td>
      <td class="ao1-r">${sum(r.total_sum)}</td>
      <td class="ao1-r">${sum(r.total_sum)}</td>
      <td class="ao1-r">${sum(r.total_sum)}</td>
      <td class="ao1-c">${esc(meta['ao-acc'] || '44.01')}</td>
    </tr>`).join('');
  let root = document.getElementById('print-root');
  if (root) root.remove();
  root = document.createElement('div');
  root.id = 'print-root';
  // v1.24.0: поля страницы АО-1 — левое 20 мм, правое 15 мм, верх 10 мм, низ 10 мм
  setPrintPageMargin('@page { size: A4 portrait; margin: 10mm 15mm 10mm 20mm; }', true);
  root.innerHTML = `
    <div class="print-page ao1-page">
      <table class="ao1-codes"><tr>
        <td></td><td class="ao1-lbl">Форма по ОКУД</td><td class="ao1-code">0302001</td></tr>
        <tr><td class="ao1-org"><b>${esc(org['НаименованиеПолное'] || meta['ao-org'] || '')}</b>
              <div class="ao1-under">наименование организации</div></td>
            <td class="ao1-lbl">по ОКПО</td><td class="ao1-code">${esc(org['ОКПО'] || dash)}</td></tr>
      </table>
      <table class="ao1-nums">
        <tr><td class="ao1-cell">Структурное подразделение<br><b>${esc(meta['ao-dept'] || dash)}</b></td>
            <td class="ao1-cell">Номер документа<br><b>${esc(meta['ao-num'] || dash)}</b></td>
            <td class="ao1-cell">Дата составления<br><b>${esc(meta['ao-date'] || dash)}</b></td>
            <td class="ao1-cell">Отчётный период<br><b>${esc(meta.month || '')}</b></td></tr>
      </table>
      <div class="ao1-approve">УТВЕРЖДАЮ<br>
        Руководитель ${esc(org['Руководитель'] || meta['ao-head'] || '')}
        <span class="ao1-sig">подпись</span>
        <span class="ao1-sig">расшифровка подписи</span>
        «___» ____________ 20___ г.</div>
      <h2 class="ao1-title">АВАНСОВЫЙ ОТЧЁТ</h2>
      <table class="ao1-meta">
        <tr><td>Подотчётное лицо</td><td colspan="3"><b>${esc(fio)}</b>
              <div class="ao1-under">фамилия, инициалы</div></td>
            <td>Табельный номер</td><td>${esc(person['ТабельныйНомер'] || dash)}</td></tr>
        <tr><td>Профессия (должность)</td><td colspan="3">${esc(person['Должность'] || meta['ao-post'] || dash)}</td>
            <td>Подразделение</td><td>${esc(meta['ao-dept'] || dash)}</td></tr>
        <tr><td>Назначение аванса</td><td colspan="5">${esc(meta['ao-purpose'] || '')}</td></tr>
      </table>
      <table class="ao1-calc">
        <thead><tr><th rowspan="2">Наименование показателя</th><th rowspan="2">Сумма, руб. коп.</th>
          <th colspan="4">Бухгалтерская запись</th></tr>
          <tr><th>Дебет<br>счёт</th><th>сумма</th><th>Кредит<br>счёт</th><th>сумма</th></tr></thead>
        <tbody>
          <tr><td>Предыдущий аванс — остаток</td><td class="ao1-r">${dash}</td><td colspan="4" class="ao1-c">${dash}</td></tr>
          <tr><td>Предыдущий аванс — перерасход</td><td class="ao1-r">${dash}</td><td colspan="4" class="ao1-c">${dash}</td></tr>
          <tr><td>Получен аванс — 1. из кассы${meta['ao-rko'] ? ` (РКО ${esc(meta['ao-rko'])})` : ''}</td><td class="ao1-r">${sum(cash)}</td>
              <td class="ao1-c">${esc(meta['ao-dt'] || '71.01')}</td><td class="ao1-r">${sum(cash)}</td>
              <td class="ao1-c">${esc(meta['ao-kt'] || '50.01')}</td><td class="ao1-r">${sum(cash)}</td></tr>
          <tr><td>&nbsp;&nbsp;&nbsp;2. на банковскую карту${meta['ao-pay'] ? ` (п/п ${esc(meta['ao-pay'])})` : ''}</td><td class="ao1-r">${sum(card)}</td>
              <td class="ao1-c">${esc(meta['ao-dt'] || '71.01')}</td><td class="ao1-r">${sum(card)}</td>
              <td class="ao1-c">51</td><td class="ao1-r">${sum(card)}</td></tr>
          <tr class="ao1-total"><td>Итого получено</td><td class="ao1-r">${sum(got)}</td><td colspan="4"></td></tr>
          <tr><td>Израсходовано</td><td class="ao1-r">${sum(total)}</td>
              <td class="ao1-c">${esc(meta['ao-acc'] || '44.01')}</td><td class="ao1-r">${sum(total)}</td>
              <td class="ao1-c">${esc(meta['ao-dt'] || '71.01')}</td><td class="ao1-r">${sum(total)}</td></tr>
          <tr><td>Остаток</td><td class="ao1-r">${rest ? sum(rest) : dash}</td><td colspan="4" class="ao1-c">${rest ? '' : dash}</td></tr>
          <tr><td>Перерасход</td><td class="ao1-r">${over ? sum(over) : dash}</td><td colspan="4" class="ao1-c">${over ? '' : dash}</td></tr>
        </tbody>
      </table>
      <p class="ao1-line">Приложение <b>${items.length}</b> документов на
        <b>${Math.max(1, Math.ceil(items.length / 18))}</b> лист(ах)</p>
      <p class="ao1-line">Отчёт проверен. К утверждению в сумме
        <b>${sum(total)}</b> руб. (${ruMoney(total)}).</p>
      <table class="ao1-signs">
        <tr><td>Главный бухгалтер</td><td class="ao1-sigline"></td><td class="ao1-sig">подпись</td>
            <td class="ao1-sigline">${esc(meta['ao-buh'] || '')}</td><td class="ao1-sig">расшифровка</td></tr>
        <tr><td>Бухгалтер (принял отчёт)</td><td class="ao1-sigline"></td><td class="ao1-sig">подпись</td>
            <td class="ao1-sigline">${esc(meta['ao-buh'] || '')}</td><td class="ao1-sig">расшифровка</td></tr>
      </table>
      <p class="ao1-line">${rest ? `Остаток внесён в кассу в сумме <b>${sum(rest)}</b> руб.` :
        (over ? `Перерасход выдан в сумме <b>${sum(over)}</b> руб.` : 'Остаток/перерасход отсутствуют.')}</p>
      <div class="ao1-receipt">
        <b>Расписка.</b> Принят к проверке от <b>${esc(fio)}</b> авансовый отчёт
        № <b>${esc((meta['ao-num'] || dash).split('-').pop())}</b> от <b>${esc(meta['ao-date'] || dash)}</b>
        на сумму <b>${sum(total)}</b> руб., документов — <b>${items.length}</b> на
        <b>${Math.max(1, Math.ceil(items.length / 18))}</b> листах.
        <div class="ao1-sigrow">Бухгалтер <span class="ao1-sigline"></span> подпись <span class="ao1-sigline"></span></div>
      </div>
      <p class="ao-foot">Сформировано в «Ямастер Чек» · ymaster.ru · ${new Date().toLocaleString('ru-RU')} · лицевая сторона</p>
    </div>
    <div class="print-page ao1-page ao1-back">
      <h3 class="ao1-backtitle">Оборотная сторона формы № АО-1</h3>
      <table class="ao1-back-table">
        <thead><tr><th rowspan="2">№ п/п</th><th colspan="3">Документ, подтверждающий расходы</th>
          <th rowspan="2">Наименование документа (расхода)</th>
          <th rowspan="2">Сумма по чеку, руб. коп.</th>
          <th rowspan="2">По отчёту</th><th rowspan="2">Принято к учёту</th>
          <th rowspan="2">Дебет счёта</th></tr>
          <tr><th>дата</th><th>номер (ФД)</th><th>ФН / ФП</th></tr></thead>
        <tbody>${rowsBack}</tbody>
        <tfoot><tr><td colspan="5" class="ao1-r"><b>ИТОГО</b></td>
          <td class="ao1-r"><b>${sum(total)}</b></td>
          <td class="ao1-r"><b>${sum(total)}</b></td>
          <td class="ao1-r"><b>${sum(total)}</b></td><td></td></tr></tfoot>
      </table>
      <table class="ao1-signs" style="margin-top:26px">
        <tr><td>Отчёт составил(а), подотчётное лицо</td><td class="ao1-sigline"></td>
            <td class="ao1-sig">подпись</td><td class="ao1-sigline">${esc(fio)}</td><td class="ao1-sig">расшифровка</td></tr>
        <tr><td class="ao1-sig" colspan="5">Дата составления: <b>${esc(meta['ao-date'] || dash)}</b></td></tr>
      </table>
      <p class="ao-foot">Сформировано в «Ямастер Чек» · ymaster.ru · оборотная сторона</p>
    </div>`;
  document.body.appendChild(root);
  toast(`АО-1 (официальная форма): ${items.length} докум. на ${sum(total)} ₽ — в диалоге печати «Сохранить как PDF»`, 'ok', '🧾');
  window.print();
}

function ruMoney(n) {
  n = Math.round((Number(n) || 0) * 100) / 100;
  const rub = Math.floor(n), kop = Math.round((n - rub) * 100);
  const ones = ['ноль', 'один', 'два', 'три', 'четыре', 'пять', 'шесть', 'семь', 'восемь', 'девять',
    'десять', 'одиннадцать', 'двенадцать', 'тринадцать', 'четырнадцать', 'пятнадцать', 'шестнадцать',
    'семнадцать', 'восемнадцать', 'девятнадцать'];
  const tens = ['', '', 'двадцать', 'тридцать', 'сорок', 'пятьдесят', 'шестьдесят', 'семьдесят', 'восемьдесят', 'девяносто'];
  const hun = ['', 'сто', 'двести', 'триста', 'четыреста', 'пятьсот', 'шестьсот', 'семьсот', 'восемьсот', 'девятьсот'];
  const tri = (x, forms) => {
    let s = '';
    const h = Math.floor(x / 100), t = Math.floor((x % 100) / 10), o = x % 10;
    if (h) s += hun[h] + ' ';
    if (t >= 2) { s += tens[t] + ' '; s += ones[o] + ' '; }
    else if (x % 100) s += ones[x % 100] + ' ';
    const last = x % 100;
    if (last >= 11 && last <= 19) s += forms[2];
    else if (o === 1) s += forms[0];
    else if (o >= 2 && o <= 4) s += forms[1];
    else s += forms[2];
    return (s + ' ').replace(/\s+/g, ' ').trim();
  };
  const mil = Math.floor(rub / 1e6), th = Math.floor((rub % 1e6) / 1e3), rest = rub % 1e3;
  let out = [];
  if (mil) out.push(tri(mil, ['миллион', 'миллиона', 'миллионов']));
  if (th) out.push(tri(th, ['тысяча', 'тысячи', 'тысяч']));
  if (rest) out.push(tri(rest, ['рубль', 'рубля', 'рублей']));
  if (!rub) out.push('ноль рублей');
  return out.join(' ').replace('один тысяча', 'одна тысяча').replace('два тысячи', 'две тысячи') +
    ' ' + String(kop).padStart(2, '0') + ' коп.';
}

// ==========================================================================
//  v1.8.0: Ведомость подотчётников за месяц (бухгалтер) — таблица + CSV
// ==========================================================================
async function openStatementModal() {
  const now = new Date();
  const ym = `${now.getFullYear()}-${String(now.getMonth() + 1).padStart(2, '0')}`;
  const { slot, close } = openModal(`
    <div class="modal-title">📋 Ведомость подотчётников</div>
    <div style="display:flex;gap:10px;align-items:end;flex-wrap:wrap;margin-bottom:12px">
      <label class="field"><span>Месяц</span><input type="month" id="st-month" value="${ym}"></label>
      <button class="btn btn-sm btn-primary" id="st-load">Показать</button>
      <button class="btn btn-sm" id="st-csv">⬇ CSV</button>
    </div>
    <div id="st-body"><div class="skeleton" style="height:120px"></div></div>
    <p class="form-hint" style="margin-top:8px">«К учёту» = сумма чека минус личные покупки.
    «Просрочено» — чеки, по которым истёк срок сдачи авансового отчёта
    (Настройки → Общие; п. 6.3 Указания ЦБ 3210-У).</p>`);
  let lastRows = [];
  const load = async () => {
    const body = slot.querySelector('#st-body');
    const mv = slot.querySelector('#st-month').value;
    if (!mv) return;
    const [y, m] = mv.split('-').map(Number);
    const from = `${y}-${String(m).padStart(2, '0')}-01`;
    const to = `${y}-${String(m).padStart(2, '0')}-${new Date(y, m, 0).getDate()}`;
    body.innerHTML = '<div class="skeleton" style="height:120px"></div>';
    try {
      const data = await api.get(`/api/v1/receipts?date_from=${from}&date_to=${to}&page_size=200`);
      const dl = viewReceipts._deadline || 10;
      const by = {};
      data.items.forEach(r => {
        const k = r.assignee || '— не назначен —';
        const b = by[k] = by[k] || { count: 0, sum: 0, work: 0, late: 0 };
        b.count++;
        b.sum += r.total_sum || 0;
        b.work += (r.total_sum || 0) - (r.personal_sum || 0);
        if (!r.exported && r.receipt_date
            && Date.now() - new Date(r.receipt_date).getTime() > dl * 86400000) b.late++;
      });
      const keys = Object.keys(by).sort((a, b) => by[b].sum - by[a].sum);
      lastRows = keys.map(k => ({ name: k, ...by[k] }));
      body.innerHTML = lastRows.length
        ? `<table class="data" style="min-width:0"><thead><tr><th>Сотрудник</th><th>Чеков</th>
           <th>Сумма</th><th>К учёту</th><th>Просрочено</th></tr></thead><tbody>
           ${lastRows.map(x => `<tr><td>${esc(x.name)}</td><td>${x.count}</td>
             <td class="cell-sum">${fmtSum(x.sum)}</td><td class="cell-sum">${fmtSum(x.work)}</td>
             <td>${x.late ? `<span class="dl-badge bad">${x.late}</span>` : '—'}</td></tr>`).join('')}
           </tbody><tfoot><tr><th>Итого</th><th>${lastRows.reduce((a, x) => a + x.count, 0)}</th>
           <th class="cell-sum">${fmtSum(lastRows.reduce((a, x) => a + x.sum, 0))}</th>
           <th class="cell-sum">${fmtSum(lastRows.reduce((a, x) => a + x.work, 0))}</th>
           <th>${lastRows.reduce((a, x) => a + x.late, 0)}</th></tr></tfoot></table>`
        : '<p class="form-hint">За этот месяц чеков нет</p>';
    } catch (e) { body.innerHTML = `<p class="form-error">${esc(e.message)}</p>`; }
  };
  slot.querySelector('#st-load').onclick = load;
  load();
  slot.querySelector('#st-csv').onclick = () => {
    if (!lastRows.length) return toast('Нет данных для CSV', 'info');
    const rows = [['Сотрудник', 'Чеков', 'Сумма', 'К учёту', 'Просрочено'],
      ...lastRows.map(x => [x.name, x.count, x.sum.toFixed(2), x.work.toFixed(2), x.late])];
    const csv = '\ufeff' + rows.map(r => r.join(';')).join('\n');
    downloadBlob(new Blob([csv], { type: 'text/csv;charset=utf-8' }),
      `vedomost-${slot.querySelector('#st-month').value}.csv`);
  };
}

function downloadBlob(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}

// --- Drawer: карточка чека --------------------------------------------------
async function receiptDrawer(id) {
  // v1.28.0: один чек — одна карточка. Повторные клики по тому же чеку
  // НЕ открывают вторую карточку; двойной клик — закрывает; строка
  // открытого чека подсвечивается в списке.
  const nowTs = Date.now();
  const lastClick = receiptDrawer._lastClick || { id: '', ts: 0 };
  receiptDrawer._lastClick = { id, ts: nowTs };
  if (receiptDrawer._openId === id && receiptDrawer._close) {
    if (lastClick.id === id && nowTs - lastClick.ts < 500) receiptDrawer._close();
    return;                                   // уже открыто — не плодим окна
  }
  if (receiptDrawer._close) receiptDrawer._close();   // открыта другая — заменим
  const r = await api.get('/api/v1/receipts/' + id);
  const acc = isAccountant();
  const canDel = isAdmin() || (!r.exported && r.created_by_id === state.me.id);
  const drawer = document.createElement('div');
  drawer.className = 'drawer';
  drawer.innerHTML = `
    <div class="drawer-head">
      <b>Чек ${fmtSum(r.total_sum)}</b>
      ${chip(r.status)} ${chip(r.fns_status)}
      <button class="btn-icon modal-close" style="margin-left:auto">✕</button>
    </div>
    <div class="drawer-body">
      <div class="qr-box" title="Нажмите, чтобы скопировать">${esc(r.qr_data)}</div>
      <dl class="kv">
        <dt>Дата чека</dt><dd>${fmtDate(r.receipt_date)}${isAccountant() ? ' ' + advanceBadge(r) : ''}</dd>
        <dt>ФН</dt><dd class="cell-mono">${r.fn}</dd>
        <dt>ФД</dt><dd class="cell-mono">${r.fd}</dd>
        <dt>ФП</dt><dd class="cell-mono">${r.fp}</dd>
        <dt>Признак расчёта</dt><dd>${r.operation === 2 ? 'Возврат прихода' : 'Приход'}</dd>
        <dt>Источник</dt><dd>${esc(r.source)}</dd>
        <dt>Кто добавил</dt><dd>${esc(r.created_by_name || r.created_by || '—')}${r.created_by_name && r.created_by ? ` <span class="form-hint">(${esc(r.created_by)})</span>` : ''}</dd>
        ${r.company_name ? `<dt>Компания</dt><dd>${esc(r.company_name)}${companyChip(r.company_id)}</dd>` : ''}
        <dt>Принят в систему</dt><dd>${fmtDate(r.created_at)}</dd>
        <dt>Выгружен в 1С</dt><dd>${r.exported ? 'да · ' + fmtDate(r.exported_at) : 'нет'}</dd>
        <dt>Ответ ФНС</dt><dd>${esc(r.fns_message || '—')}</dd>
      </dl>
      ${acc ? `
      <div class="card-title">Для бухгалтерии</div>
      <div class="form-grid" style="margin-bottom:16px">
        <label class="field"><span>Сотрудник (подотчётник)</span>
          <input id="d-assignee" value="${esc(r.assignee || '')}" placeholder="Иванов А.А."></label>
        <label class="field"><span>Комментарий</span>
          <input id="d-comment" value="${esc(r.comment || '')}" placeholder="например: канцтовары для офиса"></label>
      </div>` : ''}
      <div class="card-title">Позиции чека (${r.items.length})</div>
      ${r.items.length ? `<table class="data" style="min-width:0"><thead><tr><th>Наименование</th><th>Кол.</th><th>Цена</th><th>Сумма</th></tr></thead>
        <tbody>${r.items.map(it => `<tr style="cursor:default">
          <td>${esc(it.name)}</td><td>${it.quantity}</td>
          <td class="cell-sum">${fmtSum(it.price)}</td><td class="cell-sum">${fmtSum(it.total)}</td></tr>`).join('')}</tbody></table>`
        : emptyState('📦', 'Позиции недоступны (из QR их получить нельзя — приходят от ФНС/ОФД)')}
      <div class="modal-actions" style="justify-content:flex-start;flex-wrap:wrap">
        ${acc ? '<button class="btn btn-primary btn-sm" id="d-save">💾 Сохранить</button>' : ''}
        ${acc ? '<button class="btn btn-ok btn-sm" id="d-verify">✓ Проверить в ФНС</button>' : ''}
        ${canDel ? '<button class="btn btn-bad btn-sm" id="d-delete">🗑 Удалить</button>' : ''}
      </div>
    </div>`;
  document.body.appendChild(drawer);
  requestAnimationFrame(() => drawer.classList.add('open'));
  const openRow = document.querySelector(`#receipts-table tr[data-id="${id}"]`);
  if (openRow) openRow.classList.add('row-open');
  const close = () => {
    drawer.classList.remove('open');
    setTimeout(() => drawer.remove(), 300);
    if (receiptDrawer._openId === id) {
      receiptDrawer._openId = null;
      receiptDrawer._close = null;
      document.querySelectorAll('#receipts-table tr.row-open')
        .forEach(tr => tr.classList.remove('row-open'));
    }
  };
  receiptDrawer._openId = id;
  receiptDrawer._close = close;
  drawer.querySelector('.modal-close').onclick = close;
  drawer.querySelector('.qr-box').onclick = () => {
    navigator.clipboard && navigator.clipboard.writeText(r.qr_data);
    toast('Строка QR скопирована', 'info');
  };
  const save = drawer.querySelector('#d-save');
  if (save) save.onclick = async () => {
    try {
      await api.patch('/api/v1/receipts/' + id, {
        assignee: drawer.querySelector('#d-assignee').value.trim() || null,
        comment: drawer.querySelector('#d-comment').value.trim() || null,
      });
      toast('Сохранено', 'ok');
      close();
    } catch (e) { toast(e.message, 'err'); }
  };
  const verify = drawer.querySelector('#d-verify');
  if (verify) verify.onclick = async () => {
    await api.post(`/api/v1/receipts/${id}/verify`, {});
    toast('Чек отправлен на проверку', 'info');
    close();
  };
  const del = drawer.querySelector('#d-delete');
  if (del) del.onclick = async () => {
    if (!confirm('Удалить этот чек?')) return;
    await api.del('/api/v1/receipts/' + id);
    toast('Чек удалён', 'ok');
    close();
  };
}

// ==========================================================================
//  ЭКРАН: Выгрузка в 1С
// ==========================================================================
async function viewExport(container) {
  let onecSettings = null;
  try { onecSettings = await api.get('/api/v1/settings/onec'); } catch { /* не админ */ }
  const stats = await api.get('/api/v1/dashboard/stats?days=90');

  container.innerHTML = `
    <div class="grid cards-2-even">
      <div class="glass card">
        <div class="card-title">Экспорт пакета чеков</div>
        <div class="info-callout">Выгружаются <b>проверенные чеки</b>. Сотрудник (подотчётник)
        попадает в поле «Контрагент» документа 1С — авансовый отчёт заполняется сам.
        Формат — EnterpriseData (JSON/XML) для 1С:БП 3.0, ERP 2, УТ 11.
        Дубли в 1С отсекаются по ключу <b>ФН+ФД+ФП</b>.</div>
        <label class="field" style="margin-bottom:12px"><span>Документ 1С</span>
          <select id="exp-target">
            <option value="ПоступлениеТоваровУслуг">Поступление товаров и услуг</option>
            <option value="АвансовыйОтчет">Авансовый отчёт</option>
            <option value="ПриходныйКассовыйОрдер">Приходный кассовый ордер</option>
          </select></label>
        <label class="field" style="margin-bottom:16px"><span>Формат</span>
          <select id="exp-format">
            <option value="json">EnterpriseData JSON</option>
            <option value="xml">EnterpriseData XML</option>
          </select></label>
        <div style="display:flex;gap:10px;flex-wrap:wrap">
          <button class="btn btn-primary" id="btn-export-all">⬇ Выгрузить ожидающие (${stats.pending_export})</button>
          <button class="btn" id="btn-export-verified">⬇ Все проверенные</button>
        </div>
        <p class="form-hint" style="margin-top:12px">После скачивания чеки помечаются как выгруженные.
        Повторная загрузка в 1С не создаст дублей: ключ ФН+ФД+ФП.</p>
      </div>

      <div class="glass card">
        <div class="card-title">Push-режим (1С забирает сама)</div>
        <div class="steps">
          <div class="step"><span class="step-num"></span><div>
            В 1С создайте HTTP-соединение к этому серверу: <code class="inline" id="pull-url">/onec/v1/receipts/pull</code></div></div>
          <div class="step"><span class="step-num"></span><div>
            Заголовок авторизации: <code class="inline">X-API-Token: …</code>
            ${onecSettings ? '<button class="btn btn-sm" id="btn-reveal" style="margin-left:6px">показать токен</button>' : '(токен показывает администратор)'}</div></div>
          <div class="step"><span class="step-num"></span><div>
            Регламентным заданием выполняйте выборку и подтверждение: <code class="inline">/onec/v1/receipts/ack</code></div></div>
          <div class="step"><span class="step-num"></span><div>
            Готовый код для 1С — в документации: <code class="inline">docs/INTEGRATION_1C.md</code></div></div>
        </div>
        <div id="token-zone" style="margin-top:12px"></div>
        <div class="info-callout" style="margin-top:14px;margin-bottom:0">
          Статистика: выгружено <b>${stats.exported}</b> · ожидают <b>${stats.pending_export}</b> чеков.
        </div>
      </div>
    </div>`;

  $('#btn-export-all').onclick = () => doExport({ receipt_ids: [] });
  $('#btn-export-verified').onclick = async () => {
    const data = await api.get('/api/v1/receipts?status=verified&exported=false&page_size=200');
    const ids = data.items.map(r => r.id);
    if (!ids.length) return toast('Нет проверенных чеков, ожидающих выгрузки', 'warn');
    doExport({ receipt_ids: ids });
  };
  async function doExport(body) {
    try {
      body.format = $('#exp-format').value;
      body.target_object = $('#exp-target').value;
      const { blob, filename } = await api.download('/api/v1/receipts/export', body);
      downloadBlob(blob, filename);
      toast(`${filename} — чеки помечены как выгруженные`, 'ok', 'Экспорт в 1С');
      route(true);
    } catch (e) { toast(e.message, 'err'); }
  }
  const reveal = $('#btn-reveal');
  if (reveal) reveal.onclick = async () => {
    try {
      const s = await api.get('/api/v1/settings/onec/reveal');
      $('#token-zone').innerHTML =
        `<div class="token-line"><input readonly value="${esc(s.api_token)}" id="token-input">
         <button class="btn btn-sm" id="btn-copy-token">копировать</button></div>`;
      $('#btn-copy-token').onclick = () => {
        navigator.clipboard && navigator.clipboard.writeText(s.api_token);
        toast('Токен скопирован', 'ok');
      };
    } catch (e) { toast(e.message, 'err'); }
  };
}

// ==========================================================================
//  ЭКРАН: Маппинг
// ==========================================================================
async function viewMapping(container) {
  const [mapping, catalog] = await Promise.all([
    api.get('/api/v1/settings/mapping'),
    api.get('/api/v1/settings/mapping/catalog'),
  ]);
  const admin = isAdmin();

  container.innerHTML = `
    <div class="info-callout">Маппинг определяет, <b>какие поля чека и в какие реквизиты 1С</b> попадут
    при выгрузке. Правила применяются и в файловом экспорте, и в Push-режиме.
    ${admin ? '' : 'Просмотр — изменения вносит администратор.'}</div>
    <div class="glass card">
      <div class="card-title">Правила преобразования <span class="spacer"></span>
        <span class="form-hint" id="m-count"></span>
        ${admin ? '<button class="btn btn-sm" id="m-add">+ Добавить правило</button>' : ''}</div>
      <div id="mapping-rows"></div>
      <div class="modal-actions" style="justify-content:flex-start">
        ${admin ? '<button class="btn btn-primary" id="m-save">💾 Сохранить маппинг</button>' : ''}
      </div>
    </div>
    <div class="grid cards-2-even" style="margin-top:16px">
      <div class="glass card"><div class="card-title">Поля чека (источники)</div>
        <ul class="catalog-list" style="list-style:none">${catalog.source_fields.map(f =>
          `<li><span>${f.name}</span><code>${f.code}</code></li>`).join('')}</ul></div>
      <div class="glass card"><div class="card-title">Документы и преобразования</div>
        <ul class="catalog-list" style="list-style:none">${catalog.target_objects.map(o =>
          `<li><span>${o.name}</span><code>${o.code}</code></li>`).join('')}
          <li style="border:none"></li>
          ${catalog.transforms.map(t => `<li><span>${t.name}</span><code>${t.code}</code></li>`).join('')}</ul></div>
    </div>`;

  const rowsEl = $('#mapping-rows');

  function addRow(m = { source_field: 'total_sum', target_object: 'ПоступлениеТоваровУслуг',
                        target_field: '', transform: 'direct', transform_param: '', is_active: true }) {
    // v1.5.0: правило = карточка-строка с подписями; на телефоне — стек
    const div = document.createElement('div');
    div.className = 'map-row';
    div.innerHTML = `
      <label class="map-cell map-src"><span>Поле чека</span>
        <select class="mr-src">${catalog.source_fields.map(f => `<option value="${f.code}" ${f.code === m.source_field ? 'selected' : ''}>${f.name}</option>`).join('')}</select></label>
      <label class="map-cell"><span>Документ 1С</span>
        <select class="mr-obj">${catalog.target_objects.map(o => `<option value="${o.code}" ${o.code === m.target_object ? 'selected' : ''}>${o.name}</option>`).join('')}</select></label>
      <label class="map-cell"><span>Реквизит 1С</span>
        <input class="mr-field" placeholder="СуммаДокумента" value="${esc(m.target_field)}"></label>
      <label class="map-cell"><span>Преобразование</span>
        <select class="mr-tr">${catalog.transforms.map(t => `<option value="${t.code}" ${t.code === m.transform ? 'selected' : ''}>${t.name}</option>`).join('')}</select></label>
      <label class="map-cell map-check" title="Правило включено">
        <span>Вкл.</span><input type="checkbox" class="mr-active" ${m.is_active ? 'checked' : ''} style="width:auto"></label>
      <div class="map-actions">
        <button class="btn btn-sm btn-bad mr-del" type="button" title="Удалить правило">🗑</button></div>`;
    if (!admin) div.querySelectorAll('input,select').forEach(el => el.disabled = true);
    div.querySelector('.mr-del').onclick = () => { div.remove(); updateCount(); };
    div.addEventListener('input', updateCount);
    rowsEl.appendChild(div);
  }
  function updateCount() {
    const el = $('#m-count');
    if (!el) return;
    const rows = $$('.map-row');
    const valid = rows.filter(d => d.querySelector('.mr-field')?.value.trim()).length;
    el.textContent = rows.length ? `правил: ${rows.length} · с реквизитом: ${valid}` : '';
  }
  mapping.items.forEach(addRow);
  if (!mapping.items.length) addRow();
  updateCount();
  if (admin) {
    $('#m-add').onclick = () => addRow();
    $('#m-save').onclick = async () => {
      const items = $$('.mapping-row').map(div => ({
        source_field: div.querySelector('.mr-src').value,
        target_object: div.querySelector('.mr-obj').value,
        target_field: div.querySelector('.mr-field').value.trim(),
        transform: div.querySelector('.mr-tr').value,
        transform_param: '',
        is_active: div.querySelector('.mr-active').checked,
      })).filter(i => i.target_field);
      try {
        const r = await api.put('/api/v1/settings/mapping', { items });
        toast(`Маппинг сохранён: правил — ${r.items.length}`, 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
  }
}

// ==========================================================================
//  ЭКРАН: Пользователи и приглашения (только администратор)
// ==========================================================================
async function viewUsers(container) {
  const [users, invites] = await Promise.all([
    api.get('/api/v1/users'),
    api.get('/api/v1/invites'),
  ]);
  if (isAdmin() && !state.companies.length) {
    try { state.companies = await api.get('/api/v1/companies'); } catch (e) {}
  }
  // v1.12.0: фильтр «группа-компания» (сохраняется между перерисовками)
  if (viewUsers._companyFilter == null) viewUsers._companyFilter = 'all';
  const cf = viewUsers._companyFilter;
  const byCompany = (arr) => cf === 'all' ? arr
    : arr.filter(x => x.company_id === cf);
  const usersV = byCompany(users), invitesV = byCompany(invites);

  container.innerHTML = `
    <div class="info-callout">Регистрация — <b>только по приглашениям</b>: создайте ссылку с ролью
    и передайте сотруднику. Администратор в системе всегда <b>один</b> — права передаются
    кнопкой «Сделать администратором» у бухгалтера.
    <div style="margin-top:6px">Иерархия ролей: <b>Администратор</b> (всё) →
    <b>Бухгалтер</b> (расширенный доступ: выгрузка в 1С, маппинг, подбор из пула,
    правка чеков компании) → <b>Сотрудник</b> (свои чеки; правка — только уведомление
    и комментарий). Отдельный контур — <b>участники Чек-Пула</b> (раздел
    «Чек-Пул: участники»). Просмотр любой роли — только у администратора:
    кнопка «👁» или меню профиля в шапке.</div></div>
    ${isAdmin() && state.companies.length ? `
    <div class="filter-bar" style="margin-bottom:14px">
      <label class="field"><span>Группа-компания</span>
        <select id="p-company-filter">
          <option value="all" ${cf === 'all' ? 'selected' : ''}>Все компании</option>
          ${state.companies.map(c => `<option value="${c.id}" ${cf === c.id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
        </select></label>
      <span class="form-hint" style="align-self:end">сотрудники и приглашения выбранной группы</span>
    </div>` : ''}

    <div class="glass card" style="margin-bottom:16px">
      <div class="card-title">Приглашения <span class="spacer"></span>
        <button class="btn btn-sm btn-primary" id="i-add">+ Создать приглашение</button></div>
      <div class="table-wrap"><table class="data"><thead><tr>
        <th>Компания (группа)</th><th>Роль</th><th>Компания</th><th>Использовано</th><th>Действует до</th>
        <th>Статус</th><th>Ссылка</th><th></th></tr></thead><tbody>
        ${invitesV.length ? invitesV.map(i => `<tr style="cursor:default">
          <td><b>${esc(i.company_name || '—')}</b>${i.note ? `<div class="form-hint">${esc(i.note)}</div>` : ''}</td>
          <td>${roleChip(i.role)}</td>
          <td>${esc(i.company_name || '—')}</td>
          <td>${i.used_count} / ${i.max_uses}</td>
          <td class="cell-date">${i.expires_at ? fmtDate(i.expires_at) : '∞'}</td>
          <td>${i.valid ? '<span class="chip verified"><span class="dot"></span>активно</span>' : '<span class="chip failed"><span class="dot"></span>' + (i.revoked ? 'отозвано' : 'исчерпано') + '</span>'}</td>
          <td style="white-space:nowrap">${i.valid
            ? `<button class="btn btn-sm i-link" data-token="${esc(i.token)}">🔗 копировать</button>
               <button class="btn btn-sm i-qr" data-id="${i.id}" data-token="${esc(i.token)}" title="Показать QR-код">▣ QR</button>` : '—'}</td>
          <td>${i.valid ? `<button class="btn btn-sm btn-bad i-revoke" data-id="${i.id}">Отозвать</button>` : ''}</td>
        </tr>`).join('') : `<tr style="cursor:default"><td colspan="8">${emptyState('✉️', 'Приглашений для этой группы нет')}</td></tr>`}
      </tbody></table></div>
    </div>

    <div class="glass card">
      <div class="card-title">Пользователи системы <span class="spacer"></span>
        <button class="btn btn-sm" id="u-add">+ Создать вручную</button></div>
      <div class="table-wrap"><table class="data"><thead><tr>
        <th>Логин</th><th>ФИО</th><th>Роль</th><th>Компания</th><th>Статус</th>
        <th>Последний вход</th><th></th></tr></thead><tbody>
        ${usersV.map(u => `<tr data-id="${u.id}" style="cursor:default">
          <td><b>${esc(u.username)}</b>${u.must_change_password ? ' <span class="chip unknown mono">врем. пароль</span>' : ''}</td>
          <td>${esc(u.full_name)}</td>
          <td>${roleChip(u.role)}</td>
          <td>${esc(u.company_name || '—')}</td>
          <td>${u.is_active ? '<span class="chip verified"><span class="dot"></span>активен</span>' : '<span class="chip failed"><span class="dot"></span>отключён</span>'}</td>
          <td class="cell-date">${fmtDate(u.last_login_at)}</td>
          <td style="white-space:nowrap">
            ${u.role === 'accountant' && u.is_active ? `<button class="btn btn-sm u-admin" data-id="${u.id}" data-name="${esc(u.username)}" title="Передать права администратора">⬆ Админом</button>` : ''}
            ${u.role !== 'admin' && u.is_active ? `<button class="btn btn-sm u-role" data-id="${u.id}" data-role="accountant" data-name="${esc(u.username)}" title="Сменить роль (бухгалтер ↔ пользователь)">↕ Роль</button>` : ''}
            ${u.role !== 'admin' && u.is_active ? `<button class="btn btn-sm u-reset" data-id="${u.id}" data-name="${esc(u.username)}" title="Выдать временный пароль">🔑</button>` : ''}
            ${u.role !== 'admin' && u.is_active ? `<button class="btn btn-sm u-view" data-id="${u.id}" data-name="${esc(u.full_name || u.username)}" title="Посмотреть приложение глазами сотрудника (режим просмотра)">👁</button>` : ''}
            ${!u.is_active ? `<button class="btn btn-sm u-unarchive" data-id="${u.id}" data-name="${esc(u.username)}" title="Восстановить из архива">♻</button>` : ''}
            <button class="btn btn-sm u-edit" title="Изменить данные">✎</button>
          </td></tr>`).join('')}
      </tbody></table></div>
    </div>`;

  function roleChip(role) {
    return role === 'admin'
      ? '<span class="chip exported"><span class="dot"></span>Администратор</span>'
      : (role === 'accountant'
        ? '<span class="chip new"><span class="dot"></span>Бухгалтер</span>'
        : '<span class="chip unknown"><span class="dot"></span>Пользователь</span>');
  }

  // --- Приглашения ---
  const pcf = $('#p-company-filter');
  if (pcf) pcf.onchange = () => { viewUsers._companyFilter = pcf.value; route(true); };
  $('#i-add').onclick = () => inviteDialog();
  $$('.i-link').forEach(btn => btn.onclick = () => {
    const url = `${location.origin}/#/register/${btn.dataset.token}`;
    navigator.clipboard && navigator.clipboard.writeText(url);
    toast('Ссылка-приглашение скопирована — отправьте сотруднику', 'ok', 'Ссылка готова');
  });
  // v1.8.2: QR из списка приглашений
  $$('.i-qr').forEach(btn => btn.onclick = () =>
    openInviteQr({ id: btn.dataset.id }, `${location.origin}/#/register/${btn.dataset.token}`));
  $$('.i-revoke').forEach(btn => btn.onclick = async () => {
    if (!confirm('Отозвать приглашение? Ссылка перестанет работать.')) return;
    await api.post(`/api/v1/invites/${btn.dataset.id}/revoke`, {});
    toast('Приглашение отозвано', 'ok');
    route(true);
  });

  // v1.8.2: модал QR-кода приглашения — показать/скачать PNG
  async function openInviteQr(inv, url) {
    const { slot } = openModal(`
      <div class="modal-title">▣ QR-код приглашения</div>
      <div class="invite-qr-box"><div class="skeleton" style="height:220px;width:220px"></div></div>
      <p class="form-hint" style="margin-top:10px">Пусть сотрудник наведёт камеру телефона
      (сканер QR — камера или «Google Объектив») — откроется страница регистрации,
      роль присвоится автоматически. Ссылка: <span class="mono" style="font-size:11px">${esc(url || '')}</span></p>
      <div class="modal-actions">
        <button class="btn" data-close>Закрыть</button>
        <button class="btn btn-primary" id="iv-qr-dl">⬇ Скачать PNG</button>
      </div>`);
    slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
    const box = slot.querySelector('.invite-qr-box');
    try {
      const { blob, filename } = await api.download(`/api/v1/invites/${inv.id}/qr`);
      const objUrl = URL.createObjectURL(blob);
      box.innerHTML = `<img src="${objUrl}" alt="QR-код приглашения" width="220" height="220">`;
      slot.querySelector('#iv-qr-dl').onclick = () => downloadBlob(blob, filename);
    } catch (e) {
      box.innerHTML = `<p class="form-error">${esc(e.message)}</p>`;
    }
  }


  // --- Пользователи ---
  $('#u-add').onclick = () => userDialog();
  $$('.u-edit').forEach(btn => btn.onclick = () => {
    const u = users.find(x => x.id === btn.closest('tr').dataset.id);
    userDialog(u);
  });
  // v1.4.0: расширенные действия администратора
  $$('.u-role').forEach(btn => btn.onclick = async () => {
    const to = btn.dataset.role === 'accountant' ? 'user' : 'accountant';
    if (!confirm(`Сделать ${btn.dataset.name} ${to === 'accountant' ? 'БУХГАЛТЕРОМ (расширенный доступ)' : 'ПОЛЬЗОВАТЕЛЕМ (только свои чеки)'}?`)) return;
    try { const r = await api.patch(`/api/v1/admin/users/${btn.dataset.id}/role`, { role: to }); toast(r.message, 'ok'); route(true); }
    catch (e) { toast(e.message, 'err'); }
  });
  $$('.u-reset').forEach(btn => btn.onclick = async () => {
    if (!confirm(`Сбросить пароль ${btn.dataset.name}? Сотрудник получит временный пароль и сменит его при входе.`)) return;
    try {
      const r = await api.post(`/api/v1/admin/users/${btn.dataset.id}/reset-password`);
      openModal(`<div class="modal-title">🔑 Временный пароль для ${esc(btn.dataset.name)}</div>
        <p class="form-hint" style="margin-bottom:10px">Передайте его сотруднику — при входе система потребует сменить пароль.</p>
        <div class="qr-box" style="font-size:16px;justify-content:center">${esc(r.temp_password)}</div>
        <div class="modal-actions"><button class="btn btn-primary" id="tp-ok">Передал</button></div>`).slot.querySelector('#tp-ok').onclick = function(){ this.closest('.modal-root').classList.add('hidden'); };
      route(true);
    } catch (e) { toast(e.message, 'err'); }
  });
  // v1.40.0: режим просмотра прямо из списка пользователей
  $$('.u-view').forEach(btn => btn.onclick = () =>
    startViewAs(btn.dataset.id, btn.dataset.name));
  $$('.u-unarchive').forEach(btn => btn.onclick = async () => {
    try { const r = await api.post(`/api/v1/admin/users/${btn.dataset.id}/unarchive`); toast(r.message, 'ok'); route(true); }
    catch (e) { toast(e.message, 'err'); }
  });

  $$('.u-admin').forEach(btn => btn.onclick = async () => {
    if (!confirm(`Передать права администратора пользователю ${btn.dataset.name}?\n\n` +
      'Вы станете бухгалтером. Администратор в системе всегда один.')) return;
    try {
      const r = await api.post(`/api/v1/users/${btn.dataset.id}/promote-admin`, {});
      toast(r.message, 'ok', 'Права переданы');
      setTimeout(() => location.reload(), 1200);
    } catch (e) { toast(e.message, 'err'); }
  });

  function userDialog(u = null) {
    const { slot } = openModal(`
      <div class="modal-title">${u ? '✎ Пользователь: ' + esc(u.username) : '+ Новый пользователь'}</div>
      <div class="form-grid">
        <label class="field"><span>Логин</span><input id="u-username" value="${esc(u?.username || '')}" ${u ? 'disabled' : ''}></label>
        <label class="field"><span>Роль</span>
          <select id="u-role" ${u && u.role === 'admin' ? 'disabled' : ''}>
            <option value="user" ${u?.role === 'user' ? 'selected' : ''}>Пользователь</option>
            <option value="accountant" ${u?.role === 'accountant' ? 'selected' : ''}>Бухгалтер</option>
            ${u && u.role === 'admin' ? '<option value="admin" selected>Администратор</option>' : ''}
          </select></label>
        <label class="field"><span>ФИО</span><input id="u-fullname" value="${esc(u?.full_name || '')}"></label>
        <label class="field"><span>Организация</span><input id="u-org" value="${esc(u?.organization || '')}"></label>
        <label class="field full"><span>${u ? 'Новый пароль (пусто — не менять)' : 'Пароль'}</span>
          <input id="u-pass" type="password" placeholder="${u ? '••••••' : 'минимум 6 символов'}"></label>
        ${u ? `<label class="field"><span>Активен</span>
          <select id="u-active"><option value="1" ${u.is_active ? 'selected' : ''}>Да</option>
          <option value="0" ${!u.is_active ? 'selected' : ''}>Нет</option></select></label>` : ''}
        ${isAdmin() && state.companies.length ? `<label class="field full"><span>Компания (пространство)</span>
          <select id="u-company"><option value="">— без компании —</option>
          ${state.companies.map(c => `<option value="${c.id}" ${(u ? (u.company_id || '') : state.companyFilter) === c.id ? 'selected' : ''}>${esc(c.name)}</option>`).join('')}
          </select></label>` : ''}
      </div>
      <div class="modal-actions">
        ${u && u.id !== state.me.id ? '<button class="btn btn-bad" id="u-delete">Архивировать</button>' : ''}
        <button class="btn" data-close>Отмена</button>
        <button class="btn btn-primary" id="u-save">Сохранить</button>
      </div>`);
    slot.querySelector('[data-close]').onclick = () => $('#modal-root').classList.add('hidden');
    slot.querySelector('#u-save').onclick = async () => {
      try {
        if (u) {
          const patch = {
            full_name: slot.querySelector('#u-fullname').value,
            organization: slot.querySelector('#u-org').value,
          };
          const pass = slot.querySelector('#u-pass').value;
          if (pass) patch.password = pass;
          const active = slot.querySelector('#u-active');
          if (active) patch.is_active = active.value === '1';
          const uc = slot.querySelector('#u-company');
          if (uc) patch.company_id = uc.value || null;
          await api.patch('/api/v1/users/' + u.id, patch);
        } else {
          const ucNew = slot.querySelector('#u-company');
          await api.post('/api/v1/users', {
            username: slot.querySelector('#u-username').value.trim(),
            password: slot.querySelector('#u-pass').value,
            ...(ucNew && ucNew.value ? { company_id: ucNew.value } : {}),
            full_name: slot.querySelector('#u-fullname').value,
            organization: slot.querySelector('#u-org').value,
            role: slot.querySelector('#u-role').value,
          });
        }
        $('#modal-root').classList.add('hidden');
        toast('Пользователь сохранён', 'ok');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
    };
    const del = slot.querySelector('#u-delete');
    if (del) del.onclick = async () => {
      if (!confirm(`Архивировать пользователя ${u.username}?`)) return;
      await api.del('/api/v1/users/' + u.id);
      $('#modal-root').classList.add('hidden');
      toast('Пользователь архивирован', 'ok');
      route(true);
    };
  }
}


// --------------------------------------------------------------------------
//  v1.17.0: жёсткий сброс приложения после обновления сервера
// --------------------------------------------------------------------------
// ==========================================================================
// v1.21.0: БАННЕР «ПРОГРАММА ОБНОВИЛАСЬ НА СЕРВЕРЕ» — перезагрузка по кнопке
// ==========================================================================
function showUpdateBanner(newVer) {
  if (state.updModalOpen) return;   // v1.50.0: в окне обновления всё видно
  let b = document.getElementById('update-banner');
  if (b) {      // уже показан — обновляем версию
    const span = b.querySelector('.ub-ver');
    if (span && newVer) span.textContent = newVer;
    return;
  }
  b = document.createElement('div');
  b.id = 'update-banner';
  b.className = 'update-banner';
  b.setAttribute('role', 'status');
  b.innerHTML =
    `<span class="ub-text">✅ Программа обновилась на сервере` +
    (newVer ? ` — <b>v<span class="ub-ver">${esc(newVer)}</span></b>` : '') +
    `. Можно перезагрузиться, чтобы увидеть новую версию.</span>` +
    ` <button class="btn btn-sm btn-primary" id="ub-reload">🔄 Перезагрузиться</button>` +
    ` <button class="btn btn-sm" id="ub-later">Позже</button>`;
  document.body.appendChild(b);
  $('#ub-reload', b).onclick = () => hardReset(false);
  $('#ub-later', b).onclick = () => b.remove();
}

async function hardReset(silent = false, autoReload = true) {
  try {
    if ('caches' in window) {
      const keys = await caches.keys();
      await Promise.all(keys.filter(k => k.startsWith('ymaster-check-'))
        .map(k => caches.delete(k)));
    }
    if (navigator.serviceWorker && navigator.serviceWorker.getRegistrations) {
      const regs = await navigator.serviceWorker.getRegistrations();
      await Promise.all(regs.map(r => r.update().catch(() => {})));
    }
  } catch (e) { /* кэш не критичен */ }
  try { sessionStorage.clear(); } catch (e) {}
  if (!silent) toast('Кэш приложения сброшен — загружаю новую версию…', 'ok', '🧹');
  // v1.21.0: при уведомлении об обновлении перезагрузка — только по кнопке
  if (autoReload) setTimeout(() => location.reload(), 400);
}

// v1.17.0: сторож версии — если сервер стал новее, клиент сам сбрасывает кэш
setInterval(async () => {
  if (document.hidden) return;
  try {
    const r = await api.get('/api/v1/about', { retries: 1 });
    if (state.appVersion && r.version && r.version !== state.appVersion) {
      state.appVersion = r.version;
      hardReset(true, false).catch(() => {});        // кэш — тихо
      showUpdateBanner(r.version);            // перезагрузка — решает человек
    }
  } catch (e) { /* сервер недоступен — молча */ }
}, 60000);
document.addEventListener('visibilitychange', async () => {
  if (document.hidden) return;
  try {
    const r = await api.get('/api/v1/about', { retries: 1 });
    if (state.appVersion && r.version && r.version !== state.appVersion) {
      state.appVersion = r.version;
      hardReset(true, false).catch(() => {});
      showUpdateBanner(r.version);
    }
  } catch (e) {}
});

// v1.17.0: копирование команд в буфер (блок «Сервер и команды»)
async function copyCmd(txt, btn) {
  try {
    await navigator.clipboard.writeText(txt);
  } catch (e) {
    const ta = document.createElement('textarea');
    ta.value = txt; ta.style.position = 'fixed'; ta.style.opacity = '0';
    document.body.appendChild(ta); ta.select();
    try { document.execCommand('copy'); } catch (e2) {}
    ta.remove();
  }
  if (btn) { const o = btn.textContent; btn.textContent = '✓ скопировано';
    setTimeout(() => { btn.textContent = o; }, 1500); }
}

// ==========================================================================
//  ЭКРАН: Журнал (v1.19.0 — человеческий формат: события словами, детали
//  в «кавычках», группировка по дням, табличные цифры времени)
// ==========================================================================
const AUDIT_KIND = {
  login: ['ok', '🔑'], login_failed: ['bad', '🚫'], register: ['info', '👤'],
  password_changed: ['info', '🔐'],
  receipt_created: ['ok', '🧾'], receipt_duplicate: ['warn', '🧾'],
  receipt_updated: ['info', '✏️'], receipt_deleted: ['bad', '🗑'],
  verify_queued: ['info', '🛡'], receipts_exported: ['ok', '📤'],
  receipts_exported_csv: ['info', '📤'], receipts_assigned: ['info', '👤'],
  receipts_moved: ['info', '📦'], receipts_transferred: ['info', '📦'],
  external_fetch: ['info', '🌐'],
  invite_created: ['info', '✉️'], invite_qr: ['info', '🔢'],
  invite_revoked: ['warn', '✉️'],
  user_created: ['ok', '👤'], user_updated: ['info', '👤'],
  user_role_changed: ['warn', '👤'], user_password_reset: ['warn', '🔑'],
  user_archived: ['warn', '📦'], user_unarchived: ['ok', '📦'],
  user_moved: ['info', '🏢'], admin_transferred: ['warn', '🛡'],
  company_created: ['ok', '🏢'], company_updated: ['info', '🏢'],
  company_deleted: ['bad', '🏢'], company_card_refreshed: ['info', '🏛'],
  company_cards_bulk_refreshed: ['info', '🏛'],
  impersonate_start: ['warn', '👁'], impersonate_stop: ['ok', '👁'],
  fns_settings_updated: ['info', '⚙️'], onec_token_updated: ['info', '🔗'],
  onec_pull: ['ok', '🔗'], onec_ack: ['ok', '🔗'],
  mapping_updated: ['info', '⚙️'], app_settings_updated: ['info', '⚙️'],
  external_settings_updated: ['info', '⚙️'],
  telegram_settings_saved: ['info', '✈️'], demo_data_loaded: ['info', '🧪'],
  checko_key_saved: ['info', '🔐'], checko_key_tested: ['info', '🔐'],
  checko_key_revealed: ['warn', '🔐'], checko_key_reveal_blocked: ['bad', '🔐'],
  checko_key_reveal_failed: ['bad', '🔐'],
  backup_created: ['ok', '💾'], backup_downloaded: ['info', '💾'],
  update_check: ['info', '🔄'], update_check_failed: ['bad', '🔄'],
  update_apply: ['warn', '🔄'], update_repo_changed: ['info', '🔄'],
};

function auditDayLabel(ts) {
  const d = new Date(ts), now = new Date();
  const key = (x) => `${x.getFullYear()}-${x.getMonth()}-${x.getDate()}`;
  if (key(d) === key(now)) return 'Сегодня';
  const y = new Date(now); y.setDate(now.getDate() - 1);
  if (key(d) === key(y)) return 'Вчера';
  return d.toLocaleDateString('ru-RU', {
    day: 'numeric', month: 'long',
    year: d.getFullYear() !== now.getFullYear() ? 'numeric' : undefined,
  });
}

async function viewAudit(container) {
  let rows = [];
  try { rows = await api.get('/api/v1/dashboard/recent?limit=60'); } catch {}
  const fmtTime = (ts) =>
    new Date(ts).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' });
  // Группировка по дням: «Сегодня» / «Вчера» / «12 октября»
  const groups = [];
  rows.forEach(f => {
    const lbl = auditDayLabel(f.created_at);
    const g = groups.length ? groups[groups.length - 1] : null;
    if (g && g.label === lbl) g.items.push(f);
    else groups.push({ label: lbl, items: [f] });
  });
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title font-accent">Журнал действий</div>
      <p class="form-hint" style="margin:-2px 0 14px">Последние 60 событий — что происходило,
      обычными словами. Детали каждого события — в кавычках.</p>
      ${groups.map(g => `
        <div class="audit-day tnum">${esc(g.label)}</div>
        ${g.items.map(f => {
          const [kind, ico] = AUDIT_KIND[f.action] || ['', '📌'];
          const who = f.username ? esc(f.username) : 'система';
          const human = f.human || feedActionText(f);
          return `
          <div class="audit-row kind-${kind}">
            <span class="audit-ico" aria-hidden="true">${ico}</span>
            <div class="audit-main">
              <div class="audit-text"><b>${who}</b> ${esc(human)}</div>
              ${f.comment ? `<div class="audit-comment">«${esc(f.comment)}»</div>` : ''}
            </div>
            <span class="audit-time tnum">${fmtTime(f.created_at)}</span>
          </div>`; }).join('')}
      `).join('')}
      ${rows.length ? '' : emptyState('📜', 'Журнал пуст')}
    </div>`;
}

// ==========================================================================
//  ЭКРАН: Настройки
// ==========================================================================
async function viewSettings(container) {
  let fns = null, onec = null, appSet = null, ext = null, checkoSet = null, tgSet = null, poolSet = null, smtpSet = null, me = null;
  try { me = await api.get('/api/v1/auth/me'); } catch {}
  try { if (isAdmin()) { fns = await api.get('/api/v1/settings/fns'); onec = await api.get('/api/v1/settings/onec'); appSet = await api.get('/api/v1/settings/app'); ext = await api.get('/api/v1/settings/external'); checkoSet = await api.get('/api/v1/settings/checko'); tgSet = await api.get('/api/v1/settings/telegram'); poolSet = await api.get('/api/v1/pool-admin/overview'); smtpSet = await api.get('/api/v1/pool-admin/smtp'); } }
  catch { /* ignore */ }
  const about = await api.get('/api/v1/about');
  let appSum = null, sysInfo = null;
  try { appSum = await api.get('/api/v1/settings/app/summary'); } catch {}
  if (isAdmin()) { try { sysInfo = await api.get('/api/v1/admin/system'); } catch {} }

  container.innerHTML = `
    <div class="settings-grid">
      <div class="settings-sect" style="order:5">🗄 Данные и восстановление</div>
      <div class="settings-sect" style="order:19">🧾 Проверка чеков и почта</div>
      <div class="settings-sect" style="order:29">🧩 Чек-Пул и интеграции</div>
      <div class="settings-sect" style="order:39">👤 Личное и доступ</div>
      <div class="settings-sect" style="order:49">📱 Устройство и служебное</div>
      <div class="glass card" id="wa-card" style="order:40">
        <div class="card-title">🔑 Быстрый вход
          <span class="form-hint">отпечаток / лицо / PIN устройства</span></div>
        <div id="wa-list" class="form-hint" style="margin:6px 0">Загружаем…</div>
        <div id="wa-actions" style="display:flex;gap:8px;flex-wrap:wrap;margin-top:8px"></div>
      </div>
      ${isAdmin() ? `
      <div class="glass card" id="mail-card" style="order:23;grid-column:1/-1">
        <div class="card-title">✉📬 Почтовый центр
          <span class="form-hint">письма кабинета · бот рассылок (v1.44.0)</span></div>
        <div class="form-grid" style="margin:8px 0">
          <label class="field"><span>SMTP-сервер</span>
            <input id="mc-host" placeholder="mail.chek.ymaster.ru" value=""></label>
          <label class="field"><span>Порт</span>
            <input id="mc-port" type="number" value="587" style="max-width:110px"></label>
          <label class="field"><span>Логин ящика</span>
            <input id="mc-user" placeholder="chek@chek.ymaster.ru" value=""></label>
          <label class="field"><span>Пароль ящика</span>
            <input id="mc-pass" type="password" placeholder="задан — оставьте пустым"></label>
          <label class="field"><span>Ящик отправителя (From)</span>
            <input id="mc-sender" placeholder="chek@chek.ymaster.ru" value=""></label>
          <label class="field"><span>Имя отправителя</span>
            <input id="mc-fromname" placeholder="Чек-Пул Ямастер" value=""></label>
          <label class="field"><span>Адрес сайта для ссылок в письмах</span>
            <input id="mc-baseurl" placeholder="https://chek.ymaster.ru" value=""></label>
          <label style="display:flex;gap:8px;align-items:center;cursor:pointer">
            <input type="checkbox" id="mc-tls" style="width:auto">
            <span>STARTTLS (обычно включён)</span></label>
        </div>
        <div class="form-grid" style="margin:8px 0">
          <label class="field"><span>Приветствие (переменная {name})</span>
            <input id="mc-greeting" value=""></label>
          <label class="field"><span>Подпись в конце письма</span>
            <input id="mc-signature" value=""></label>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin:8px 0">
          <button class="btn btn-sm btn-primary" id="mc-save">💾 Сохранить</button>
          <input id="mc-testto" placeholder="куда отправить тест" style="max-width:230px">
          <button class="btn btn-sm" id="mc-test">✉ Тестовое письмо</button>
        </div>
        <div class="form-hint" style="margin:4px 0 10px">Для доставляемости добавьте в DNS домена
          SPF и DKIM своего почтового сервера: <code>v=spf1 mx ~all</code> и подпись DKIM.</div>

        <div class="card-title" style="margin-top:14px">🤖 Бот рассылок
          <span class="spacer"></span>
          <label style="display:flex;gap:8px;align-items:center;cursor:pointer">
            <input type="checkbox" id="mc-bot" style="width:auto">
            <span>включён</span></label></div>
        <div id="mc-rules" class="form-hint" style="margin:6px 0">Загружаем…</div>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin:10px 0">
          <button class="btn btn-sm" id="mc-newrule">+ Новое правило</button>
        </div>
        <div id="mc-log" style="margin-top:8px"></div>
      </div>` : ''}
      ${isAdmin() && appSet ? `
      <div class="glass card" style="order:22">
        <div class="card-title">Общие</div>
        <label style="display:flex;gap:12px;align-items:center;cursor:pointer;margin-bottom:10px">
          <input type="checkbox" id="app-autoverify" ${appSet.auto_verify ? 'checked' : ''} style="width:auto">
          <span>Автоматически проверять чек в ФНС сразу после сканирования</span></label>
        <p class="form-hint">Экономит время бухгалтера: чек проверяется без участия человека,
        статусы обновляются в реальном времени у всех пользователей.</p>
        <label class="field" style="max-width:280px;margin-top:12px"><span>Срок сдачи авансового отчёта, дней от даты чека</span>
          <input type="number" id="app-deadline" min="1" max="365" value="${appSet.advance_deadline_days || 10}">
          <span class="form-hint">По умолчанию 10. Подотчётник обязан отчитаться не позднее 3 рабочих
          дней после израсходования (п. 6.3 Указания ЦБ 3210-У) — срок в организации устанавливает
          руководитель приказом; просрочка подсвечивается в «Базе чеков».</span></label>
        <button class="btn btn-primary btn-sm" id="app-save" style="margin-top:12px">💾 Сохранить</button>
      </div>` : ''}

      ${isAdmin() && fns ? `
      <div class="glass card" style="order:20">
        <div class="card-title">Проверка чеков (ФНС)</div>
        <div class="segmented" style="margin-bottom:14px">
          <button id="seg-mock" class="${fns.provider === 'mock' ? 'active' : ''}">Демо (mock)</button>
          <button id="seg-fns" class="${fns.provider === 'fns' ? 'active' : ''}">Реальное API ФНС</button>
        </div>
        <div class="info-callout" id="fns-hint" style="font-size:12.5px">
          ${fns.provider === 'mock'
            ? '<b>Демо-провайдер</b> имитирует ответы ФНС — для знакомства с системой и обучения.'
            : '<b>Реальное API</b> (openapi.nalog.ru). Требуется Мастер-токен ФНС — заявка в ФНС (см. docs/knowledge/fns_auth.md).'}</div>
        <label class="field" style="margin-bottom:12px"><span>Мастер-токен ${fns.has_master_token ? '(задан: ' + esc(fns.master_token_masked) + ')' : '(не задан)'}</span>
          <input id="fns-token" type="password" placeholder="вставьте мастер-токен ФНС"></label>
        <label class="field" style="margin-bottom:14px"><span>ClientAppId</span>
          <input id="fns-appid" value="${esc(fns.client_app_id)}"></label>
        <button class="btn btn-primary btn-sm" id="fns-save">💾 Сохранить настройки ФНС</button>
        <p class="form-hint" style="margin-top:10px">Кэш проверок: ${fns.cache_ttl_days} дней (повторная проверка не расходует лимиты).</p>
      </div>` : ''}

      ${isAdmin() && fns ? `
      <div class="glass card" style="order:31">
        <div class="card-title">🗂 ЕГРЮЛ/ЕГРИП — Checko.ru <span class="form-hint">(v1.13+)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Ключ заполняет карточки компаний
        реквизитами из ЕГРЮЛ/ЕГРИП по ИНН (полное название, ОГРН, КПП, адрес, руководитель,
        уставный капитал, ОКВЭД, налоговый орган, статус). Возьмите ключ в личном кабинете
        <a href="https://checko.ru/user/account/api" target="_blank" rel="noopener">checko.ru → API</a>
        — бесплатный тариф: 100 запросов в сутки.</p>
        <div class="info-callout" style="font-size:12.5px;margin-bottom:12px">🔒 Ключ хранится
        <b>зашифрованным</b> (AES-CBC + HMAC, ключ сервера) — в базе и логах его нет.
        Показывается только вам после подтверждения пароля; каждый показ — в журнале аудита.</div>
        <label class="field" style="margin-bottom:10px"><span>API-ключ Checko
          ${checkoSet && checkoSet.has_key ? '(задан: ' + esc(checkoSet.key_masked) + ')' : '(не задан)'}</span>
          <input id="checko-key" type="password" autocomplete="off" spellcheck="false"
                 placeholder="вставьте ключ из личного кабинета"></label>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-primary btn-sm" id="checko-save">💾 Сохранить ключ</button>
          <button class="btn btn-sm" id="checko-reveal">👁 Показать ключ</button>
          <button class="btn btn-sm" id="checko-test" title="Живой запрос ЕГРЮЛ по тестовому ИНН — тратит 1 запрос из 100 дневных">🧪 Проверить ключ</button>
        </div>
        <p class="form-hint" id="checko-status" style="margin-top:10px">${esc(checkoSet && checkoSet.hint || '')}</p>
      </div>` : ''}

      ${isAdmin() && tgSet ? `
      <div class="glass card" style="order:33">
        <div class="card-title">🔔 Telegram-бот <span class="form-hint">(v1.16.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Напоминания сотрудникам «сдай чек за сегодня»
        и уведомления о принятых чеках. Токен — у
        <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> (/newbot),
        вставьте один раз — хранится зашифрованным. Если бот молчит —
        <b>сначала «🔍 Диагностика»</b>: во многих сетях РФ api.telegram.org
        заблокирован, тогда поможет прокси ниже.</p>
        <label class="field" style="margin-bottom:10px"><span>Токен бота
          ${tgSet.has_token ? '(задан: ' + esc(tgSet.token_masked) + (tgSet.bot_username ? ', @' + esc(tgSet.bot_username) : '') + ')' : '(не задан)'}</span>
          <input id="tg-token" type="password" autocomplete="off" placeholder="123456789:AA…"></label>
        <label class="field" style="margin-bottom:10px"><span>Прокси для Telegram
          ${tgSet.proxy_configured ? '(задан: ' + esc(tgSet.proxy_masked || '•••') + ')' : '(не задан — нужен, если api.telegram.org недоступен с сервера)'}</span>
          <input id="tg-proxy" autocomplete="off" placeholder="socks5://логин:пароль@хост:порт (пусто — не менять; «-» — убрать)"></label>
        <label style="display:flex;gap:10px;align-items:center;cursor:pointer;margin:6px 0 12px">
          <input type="checkbox" id="tg-enabled" ${tgSet.enabled ? 'checked' : ''} style="width:auto">
          <span>Включить бота: напоминания в ${esc(tgSet.reminder_time)} сотрудникам без чеков за сегодня</span></label>
        <div class="form-grid">
          <label class="field"><span>Время напоминания (ЧЧ:ММ)</span>
            <input id="tg-time" value="${esc(tgSet.reminder_time)}" placeholder="18:00"></label>
          <label class="field"><span>Подключено сотрудников</span>
            <input value="${tgSet.bound_users}" disabled></label>
          <label class="field full"><span>Текст напоминания ({name} — имя сотрудника)</span>
            <input id="tg-text" value="${esc(tgSet.reminder_text)}"></label>
        </div>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-primary btn-sm" id="tg-save">💾 Сохранить и запустить</button>
          <button class="btn btn-sm" id="tg-diag">🔍 Диагностика</button>
          <button class="btn btn-sm" id="tg-test">🧪 Тест себе</button>
          <button class="btn btn-sm" id="tg-refresh"> ⟳ Обновить данные бота</button>
        </div>
        <p class="form-hint" id="tg-diag-out" style="margin-top:10px"></p>
        <p class="form-hint" style="margin-top:6px">${tgSet.worker_running ? '✅ Воркер запущен' : '⏸ Воркер не запущен'} ·
          статус воркера также виден после сохранения</p>
      </div>` : ''}

      ${isAdmin() && poolSet ? `
      <div class="glass card" style="order:30">
        <div class="card-title">🧩 Чек-Пул <span class="form-hint">(Этап 9.1 · v1.39.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Открытая база чеков (план docs/plan.md): любой человек
        сдаёт чек на странице «Сдать чек» (#/public) — строкой QR или фото кода;
        мы проверяем чек по официальным источникам и начисляем балл; компании
        смогут подбирать чеки для отчётов (следующие этапы). Ядро приложения
        не меняется; приём выключен по умолчанию.
        ${poolSet.enabled ? '<b>Приём включён — форма на сайте принимает чеки (бот — резервный канал).</b>' : 'Приём сейчас выключен.'}
        <a href="#/public">Открыть страницу приёма →</a>
        <a href="#/pooladmin" style="margin-left:12px">Панель модерации и разметки →</a>
        <a href="#/fraud" style="margin-left:12px">Антифрод →</a>
        <a href="#/poolpick" style="margin-left:12px">Подбор из пула →</a></p>
        <label style="display:flex;gap:10px;align-items:center;cursor:pointer;margin:6px 0 12px">
          <input type="checkbox" id="pool-enabled" ${poolSet.enabled ? 'checked' : ''} style="width:auto">
          <span>Принимать чеки в пул (форма на сайте; бот — резервный канал)</span></label>
        <div class="form-grid">
          <label class="field"><span>Чеков в пуле</span><input value="${poolSet.receipts_total}" disabled></label>
          <label class="field"><span>Проверены (с баллами)</span><input value="${poolSet.verified}" disabled></label>
          <label class="field"><span>На ручной проверке</span><input value="${poolSet.pending}" disabled></label>
          <label class="field"><span>Отклонены</span><input value="${poolSet.rejected}" disabled></label>
          <label class="field"><span>Участников</span><input value="${poolSet.users_total}" disabled></label>
          <label class="field"><span>Баллов начислено</span><input value="${poolSet.points_total}" disabled></label>
        </div>
        <label class="field" style="max-width:340px;margin:8px 0"><span>Квота «Подбора из пула», чеков/мес на компанию (0 — подбор выключен; тариф — при запуске: подписка 5 000 ₽/мес или 50 ₽/чек)</span>
          <input id="pool-pick-limit" type="number" min="0" value="${poolSet.pick_monthly_limit ?? 100}"></label>
        <p class="form-hint" style="margin:8px 0">1 чек = ${poolSet.points_per_receipt} балл ·
          лимит ${poolSet.daily_limit} чеков/сутки с человека · дубль баллов не приносит.</p>
        <button class="btn btn-primary btn-sm" id="pool-save">💾 Сохранить</button>
        <div class="card-title" style="margin-top:16px;font-size:15px">🔌 Платное API для внешних клиентов
          <span class="form-hint">(v1.39.0: анонимные агрегаты — не «продажа чеков»)</span></div>
        <label style="display:flex;gap:10px;align-items:center;cursor:pointer;margin:6px 0">
          <input type="checkbox" id="pool-api-enabled" ${poolSet.api_enabled ? 'checked' : ''} style="width:auto">
          <span>Включить API (ключи партнёров, лимиты по тарифу)</span></label>
        <p class="form-hint" style="margin:2px 0 8px">Наружу уходят только счётчики и агрегаты по регионам/отраслям/
          продавцам и фильтрам; сырые чеки и участники — никогда. Вызовов за месяц:
          <b>${fmtInt(poolSet.api_calls_month || 0)}</b> · ключей: <b>${fmtInt(poolSet.api_keys_active || 0)}</b>
          активных из ${poolSet.api_keys_total || 0} · лимиты по умолчанию:
          ${poolSet.api_defaults ? poolSet.api_defaults.rate_per_hour + '/час · ' + poolSet.api_defaults.monthly_quota + '/мес' : '—'}.</p>
        <div id="pool-api-box"></div>
        <p class="form-hint" style="margin-top:8px">📜 ${esc(poolSet.offerta)}</p>
        ${smtpSet ? `
        <div class="card-title" style="margin-top:14px;font-size:15px">✉️ Почта кабинета <span class="form-hint">(v1.32.0: подтверждение e-mail, вход по ссылке)</span></div>
        <div class="form-grid">
          <label class="field"><span>SMTP-хост</span><input id="pool-smtp-host" value="${esc(smtpSet.host)}" placeholder="smtp.yandex.ru"></label>
          <label class="field"><span>Порт</span><input id="pool-smtp-port" value="${esc(smtpSet.port)}"></label>
          <label class="field"><span>Логин</span><input id="pool-smtp-user" value="${esc(smtpSet.user)}"></label>
          <label class="field"><span>Пароль</span><input id="pool-smtp-pass" type="password" placeholder="${smtpSet.has_password ? 'задан — пусто = не менять' : 'пароль SMTP'}"></label>
          <label class="field"><span>Письмо «от кого»</span><input id="pool-smtp-from" value="${esc(smtpSet.sender)}"></label>
          <label class="field"><span>Адрес сайта (для ссылок)</span><input id="pool-smtp-base" value="${esc(smtpSet.base_url)}" placeholder="https://chek.ymaster.ru"></label>
        </div>
        <label style="display:flex;gap:10px;align-items:center;cursor:pointer;margin:6px 0">
          <input type="checkbox" id="pool-smtp-tls" ${smtpSet.tls ? 'checked' : ''} style="width:auto">
          <span>STARTTLS (обычно включён)</span></label>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <button class="btn btn-primary btn-sm" id="pool-smtp-save">💾 Сохранить почту</button>
          <input id="pool-smtp-test-email" type="email" placeholder="куда прислать тест" style="max-width:230px">
          <button class="btn btn-sm" id="pool-smtp-test">✉️ Тест письма</button>
        </div>
        ${smtpSet.configured ? '<p class="form-hint" style="margin-top:6px">Почта настроена — вход по ссылке и подтверждение e-mail работают.</p>'
          : '<p class="form-hint" style="margin-top:6px">Без SMTP кабинет работает по паролю: вход по ссылке и письма подтверждения отключены.</p>'}` : ''}
      </div>` : ''}

      ${me ? `
      <div class="glass card" style="order:34">
        <div class="card-title">🔔 Telegram — личное <span class="form-hint">(v1.16.0)</span></div>
        ${me.telegram_bound
          ? `<div class="info-callout" style="margin-bottom:10px">✅ Чат привязан — уведомления приходят в Telegram.
               Напоминание приходит, если за день ни одного чека.</div>
             <button class="btn btn-sm btn-bad" id="tg-unbind">🔌 Отвязать</button>`
          : `<p class="form-hint" style="margin-bottom:10px">Получайте уведомления о чеках и напоминания.
             Нажмите «Получить код», затем в Telegram отправьте боту: <b>/start КОД</b>.</p>
             <div style="display:flex;gap:10px;align-items:center;flex-wrap:wrap">
               <button class="btn btn-primary btn-sm" id="tg-code">🔑 Получить код</button>
               <span id="tg-code-out" style="font-size:20px;font-weight:700;letter-spacing:2px"></span>
             </div>
             <p class="form-hint" id="tg-code-hint" style="margin-top:8px"></p>`}
      </div>` : ''}

      ${isAdmin() && appSet ? `
      <div class="glass card" style="order:21">
        <div class="card-title">📥 Источники данных чека <span class="form-hint">(v1.57.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Полные данные чека (магазин, ИНН, позиции, место расчётов,
        касса, смена, налог, свойства заказа, итоги НДС) система получает из
        <b>proverkacheka.com</b> — единственного стабильно работающего источника
        (v1.57.0: Приложение ФНС, Честный Знак и ОФД-ру выведены из системы —
        сервисы перестали отвечать; их поля из настроек игнорируются).
        При блокировке источник «остывает» с растущей паузой, между запросами —
        2–7 секунд. <b>API ФНС</b> (мастер-токен в карточке «Проверка чеков») —
        на перспективу: при появлении токена источник автоматически станет первым.</p>
        <label class="field" style="margin-bottom:10px"><span>Токен proverkacheka.com
          ${ext && ext.has_proverkacheka_token ? '(задан: ' + esc(ext.proverkacheka_token_masked) + ')' : '(не задан — получите в личном кабинете proverkacheka.com → Справка → API)'}</span>
          <input id="ext-pke" type="password" placeholder="токен API"></label>
        <label class="field" style="margin-bottom:10px"><span>Порядок источников</span>
          <input id="ext-order" value="${esc(ext ? ext.external_order : 'proverkacheka')}">
          <small class="form-hint">proverkacheka — по токену (квота 12–14/сутки); fns_api добавится первым автоматически, когда будет задан мастер-токен ФНС (карточка «Проверка чеков»)</small></label>
        <label style="display:flex;gap:10px;align-items:center;cursor:pointer;margin:6px 0 12px">
          <input type="checkbox" id="ext-auto" ${ext && ext.external_auto ? 'checked' : ''} style="width:auto">
          <span>Автоматически получать данные после сканирования</span></label>
        <div style="display:flex;gap:8px;flex-wrap:wrap">
          <button class="btn btn-primary btn-sm" id="ext-save">💾 Сохранить источники</button>
          <button class="btn btn-sm" id="ext-test">🧪 Тест контрольным чеком</button>
        </div>
        <p class="form-hint" id="ext-status" style="margin-top:10px"></p>
      </div>` : ''}

      ${isAdmin() ? `
      <div class="glass card" style="order:13">
        <div class="card-title">🖥 Сервер и команды <span class="form-hint">(v1.17.0)</span></div>
        <dl class="kv" style="font-size:13px" id="sys-info"><dt>Загрузка…</dt><dd></dd></dl>
        <p class="form-hint" style="margin:10px 0 6px">Команды выполняются на сервере по SSH.
        Первая — одноразовая: разрешает приложению перезапускать себя без пароля
        (после неё обновления из приложения идут полностью автоматически).</p>
        <div id="sys-cmds"></div>
      </div>

      <div class="glass card" style="order:11">
        <div class="card-title">🔄 Обновления <span class="form-hint">(v1.6.0)</span></div>
        <div class="upd-status-line"><span class="upd-dot upd-dot--wait" id="upd-dot"></span>
          <span id="upd-status-text">Проверяю…</span></div>
        <dl class="kv" style="font-size:13px;margin-top:10px">
          <dt>Установлена</dt><dd id="upd-current">v—</dd>
          <dt>Сборка</dt><dd><code class="inline" id="upd-commit">—</code></dd>
          <dt>Проверено</dt><dd id="upd-checked">—</dd>
          <dt>Последнее обновление</dt><dd id="upd-last">—</dd>
        </dl>
        <div id="upd-ready" style="margin:10px 0"><div class="skeleton" style="height:34px"></div></div>
        <div id="upd-avail" class="hidden" style="margin:10px 0">
          <div class="info-callout" style="margin-bottom:10px">🆕 Доступна версия <b id="upd-remote">v—</b>.
            <a href="#" id="upd-whats" style="margin-left:6px">Что изменится?</a></div>
          <button class="btn btn-primary btn-sm" id="btn-apply-update">⬇ Обновить (данные и настройки сохранятся)</button>
        </div>
        <p class="form-error hidden" id="upd-err" style="margin:8px 0"></p>
        <div id="upd-manual" class="hidden" style="margin:10px 0">
          <div class="info-callout" style="margin-bottom:8px">Сервер не смог достучаться до GitHub
          (в РФ периодически блокируют <code class="inline">raw.githubusercontent.com</code>).
          Обновите вручную одной командой на сервере — данные сохранятся:</div>
          <pre class="codeblock">sudo bash /opt/ymaster-check/deploy.sh --update</pre>
          <p class="form-hint" style="margin-top:6px">Скрипт обновит файлы через git (порт 443 — работает даже при блокировках),
          поставит зависимости и перезапустит сервис. Данные и настройки сохраняются.</p>
        </div>
        <details style="margin:10px 0">
          <summary style="cursor:pointer;font-size:12.5px;color:var(--text-dim)">🎛 Источник обновлений (репозиторий и ветка)</summary>
          <div class="upd-repo-grid" style="margin-top:10px">
            <label class="field"><span>Репозиторий (owner/repo)</span>
              <input id="upd-repo" placeholder="Rimlin-UNC/1c_chek"></label>
            <label class="field"><span>Ветка</span>
              <input id="upd-branch-input" placeholder="arena/01a0caaa-1c-chek"></label>
          </div>
          <button class="btn btn-sm" id="btn-save-repo" style="margin-top:8px">💾 Сохранить источник</button>
        </details>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:10px">
          <button class="btn btn-sm" id="btn-check-update">🔍 Проверить обновления</button>
          <button class="btn btn-sm" id="btn-changelog">📜 История версий</button>
          <button class="btn btn-sm" id="btn-backup">💾 Скачать резервную копию БД</button>
        </div>
        <p class="form-hint" style="margin-top:10px">Обновление ставится прямо отсюда: копия БД → только изменения с GitHub →
        установка зависимостей → проверка целостности → рестарт. При сбое — автоматический откат.
        Проверка идёт по трём независимым каналам (api.github.com → raw → git), поэтому работает даже при блокировках.</p>
      </div>` : ''}

      ${isAdmin() ? `
      <div class="glass card" style="order:12">
        <div class="card-title">↩ Последние рабочие версии
          <span class="form-hint">(v1.46.0 · хранятся три последние)</span></div>
        <p class="form-hint" style="margin-bottom:8px">Программа сама запоминает версии, на которых успешно работала.
        Если новая версия проблемная — откатитесь к рабочей одной кнопкой: база не трогается,
        файлы возвращаются, приложение перезапускается. Если приложение вообще не запускается —
        на сервере: <span class="cell-mono">sudo bash rollback.sh</span></p>
        <div id="rel-list"><p class="form-hint">Загружаем…</p></div>
        <details style="margin-top:10px">
          <summary class="form-hint" style="cursor:pointer">Откат к произвольному коммиту — если нужной версии нет в списке</summary>
          <div style="display:flex;gap:8px;margin-top:8px;flex-wrap:wrap">
            <input id="rb-commit" class="cell-mono" placeholder="хеш коммита, например 3944e7e"
                   style="max-width:300px" autocomplete="off">
            <button class="btn btn-sm" id="rb-commit-go">↩ Откатиться к коммиту</button>
          </div>
          <p class="form-hint" style="margin-top:6px">Хеш виден в журнале версий на GitHub
          («история» файла) или на сервере: <span class="cell-mono">git -C /opt/ymaster-check log --oneline -10</span></p>
        </details>
      </div>` : ''}

      ${isAdmin() && onec ? `
      <div class="glass card" style="order:32">
        <div class="card-title">Интеграция с 1С</div>
        <label class="field" style="margin-bottom:12px"><span>Токен Push-доступа</span>
          <div class="token-line"><input id="onec-token" readonly value="${esc(onec.api_token_masked)}">
          <button class="btn btn-sm" id="onec-reveal">показать</button></div></label>
        <button class="btn btn-sm" id="onec-regen" style="margin-bottom:12px">♻ Сгенерировать новый токен</button>
        <div class="info-callout" style="font-size:12.5px;margin-bottom:0">
          Адрес для 1С: <code class="inline">${location.origin}/onec/v1/receipts/pull</code><br>
          Заголовок: <code class="inline">X-API-Token</code>. Подробности — вкладка «Выгрузка в 1С» и
          <code class="inline">docs/INTEGRATION_1C.md</code>.</div>
      </div>` : ''}

      ${!isAdmin() && me ? `
      <div class="glass card" style="order:43">
        <div class="card-title">${state.me.role === 'accountant' ? '🏢 Ваша компания' : '👤 Мои чеки'} <span class="form-hint">(v1.17.0)</span></div>
        ${state.me.role === 'accountant' ? `
        <dl class="kv" style="font-size:13px">
          <dt>Компания</dt><dd>${esc(me.company_name || '—')}</dd>
          <dt>ИНН</dt><dd>${esc(me.company_inn || 'не задан — попросите администратора указать ИНН в карточке компании')}</dd>
          <dt>Срок авансового отчёта</dt><dd>${appSum ? appSum.advance_deadline_days + ' дн. от даты чека (п. 6.3 Указания ЦБ 3210-У)' : '—'}</dd>
          <dt>Проверка ФНС сразу</dt><dd>${appSum && appSum.auto_verify ? 'включена' : 'по кнопке'}</dd>
        </dl>` : `
        <dl class="kv" style="font-size:13px">
          <dt>Срок авансового отчёта</dt><dd>${appSum ? appSum.advance_deadline_days + ' дн. от даты чека' : '—'}</dd>
          <dt>Проверка чеков</dt><dd>${appSum && appSum.auto_verify ? 'автоматическая (ФНС)' : 'по кнопке «Проверить»'}</dd>
        </dl>
        <p class="form-hint">Сфотографируйте чек сразу после покупки — он сам попадёт
        к бухгалтеру. Если забыли — Telegram напомнит (привяжите чат ниже).</p>`}
      </div>` : ''}

      <div class="glass card" style="order:52">
        <div class="card-title">🧹 Обслуживание устройства <span class="form-hint">(v1.17.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Если после обновления сервера что-то
        отображается по-старому (иконки, цифры, интерфейс) — сбросьте локальный кэш приложения.
        Обычно сброс происходит автоматически.</p>
        <button class="btn btn-sm" id="btn-cache-reset">🧹 Сбросить кэш приложения</button>
      </div>

      <div class="glass card" style="order:50">
        <div class="card-title">🔕 Уведомления</div>
        <p class="form-hint" style="margin-bottom:8px">«Спокойный час» прячет некритичные
        уведомления на выбранный срок; ошибки показываются всегда.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:center">
          <button class="btn btn-sm" id="btn-quiet">Отключить</button>
          <select id="quiet-dur" class="company-select" style="max-width:150px">
            <option value="30">на 30 минут</option>
            <option value="60" selected>на 1 час</option>
            <option value="180">на 3 часа</option>
          </select>
        </div>
        <div class="form-hint" id="quiet-status" style="margin-top:6px"></div>
      </div>

      <div class="glass card" style="order:51">
        <div class="card-title">📲 Приложение на устройстве <span class="form-hint">(v1.5.0)</span></div>
        <p class="pwa-hint">Установите Ямастер Чек как приложение: иконка на домашнем экране,
        полноэкранный режим, быстрый доступ к сканеру. Работает на Android, iPhone/iPad,
        Windows и macOS — без магазина приложений.</p>
        <button class="btn btn-primary btn-sm" id="btn-pwa" style="margin-top:10px">📲 Установить / как установить</button>
      </div>

      ${isAdmin() ? `
      <div class="glass card" style="order:10">
        <div class="card-title">💾 Резервные копии <span class="form-hint">(v1.5.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Копии создаются автоматически: ежедневные (7 шт.),
        перед каждым обновлением (5) и архив месяца (12 месяцев) — каталог data/backups на сервере.
        Данные не затрагиваются ни обновлениями, ни git.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <select id="bk-kind" style="max-width:250px">
            <option value="manual">Ручная — хранятся 10</option>
            <option value="daily">Дневная — хранятся 7</option>
            <option value="weekly">Недельная — хранятся 2</option>
            <option value="archive">Месячный архив — хранятся 12</option>
          </select>
          <button class="btn btn-sm btn-primary" id="btn-bk-create">＋ Создать копию сейчас</button>
          <p class="form-hint" style="margin:8px 0 0">Автоматически: каждый день — дневная копия, каждый понедельник — недельная,
          1-го числа — месячный архив. В копию входит вся база: чеки, пользователи, компании, настройки.
          Каждая копия проверяется на целостность; архивная история (недельные и месячные) обновлениями не переписывается.
          Скачанную раньше копию можно вернуть: кнопка «⬆ Загрузить копию» проверит файл и добавит его в список («загруженные», хранятся 10).</p>
          <button class="btn btn-sm" id="btn-bk-refresh">↻ Обновить список</button>
          <button class="btn btn-sm" id="btn-bk-import"
                  title="Загрузить копию из файла: .db, .sqlite, .sqlite3">⬆ Загрузить копию</button>
          <input type="file" id="bk-import-file" accept=".db,.sqlite,.sqlite3" style="display:none">
        </div>
        <div id="bk-filters" style="display:flex;gap:10px;align-items:center;flex-wrap:wrap;margin:8px 0"></div>
        <div id="bk-list"><div class="skeleton" style="height:60px"></div></div>
      </div>` : ''}

      <div class="glass card" style="order:41">
        <div class="card-title">Мой профиль</div>
        <dl class="kv">
          <dt>Логин</dt><dd>${esc(state.me.username)}</dd>
          <dt>Роль</dt><dd>${roleLabel(state.me.role)}</dd>
          <dt>Организация</dt><dd>${esc(state.me.organization || '—')}</dd>
        </dl>
        <div class="form-grid">
          <label class="field"><span>Старый пароль</span><input id="p-old" type="password"></label>
          <label class="field"><span>Новый пароль</span><input id="p-new" type="password"></label>
        </div>
        <button class="btn btn-sm btn-primary" id="p-save" style="margin-top:12px">Сменить пароль</button>
      </div>

      <div class="glass card" style="order:42">
        <div class="card-title">Безопасность и доступ</div>
        <dl class="kv">
          <dt>Регистрация</dt><dd>только по приглашениям администратора</dd>
          <dt>Администратор</dt><dd>всегда один, передача прав — в разделе «Пользователи»</dd>
          <dt>Защита входа</dt><dd>блокировка после 5 неудачных попыток</dd>
          <dt>Лимиты запросов</dt><dd>включены (защита от перебора и DoS)</dd>
        </dl>
      </div>

      <div class="glass card" style="order:53">
        <div class="card-title">О системе</div>
        <dl class="kv">
          <dt>Продукт</dt><dd>${esc(about.app)} v${esc(about.version)}</dd>
          <dt>Разработчик</dt><dd>${esc(about.vendor)}</dd>
          <dt>Сайт</dt><dd><a href="${esc(about.site)}" target="_blank" rel="noopener">${esc(about.site)}</a></dd>
          <dt>E-mail</dt><dd><a href="mailto:${esc(about.email)}">${esc(about.email)}</a></dd>
          <dt>Чеков в базе</dt><dd>${fmtInt(about.receipts_total)}</dd>
        </dl>
        <p class="form-hint">Все права на систему принадлежат ООО «Ямастер». Использование и модификация
        — согласно лицензии проекта.</p>
      </div>
    </div>`;

  // PWA-кнопка в настройках (все роли)
  const bp = $('#btn-pwa');
  if (bp) bp.onclick = showInstallDialog;

  // Резервные копии (админ)
  if (isAdmin()) {
    // v1.48.0: фильтры копий — тип и дата (выпадающие списки справа)
    const bkState = { kind: '', date: '' };
    const kindNames = { daily: 'ежедневная (7)', weekly: 'недельная (2)',
                        preupdate: 'перед обновлением (5)',
                        manual: 'вручную (10)',
                        imported: 'загруженная (10)',
                        archive: 'месячный архив (12)' }; // v1.49.0
    const renderBkFilters = (items) => {
      const box = $('#bk-filters');
      if (!box) return;
      const kinds = {};
      const dates = {};
      items.forEach(b => {
        kinds[b.kind] = (kinds[b.kind] || 0) + 1;
        const d = (b.created_at || '').slice(0, 10);
        if (d) dates[d] = (dates[d] || 0) + 1;
      });
      box.innerHTML =
        `<span class="form-hint" style="margin-right:auto">Копий в архиве: <b>${items.length}</b>` +
        ` · показано: <b>${items.filter(b => (!bkState.kind || b.kind === bkState.kind) &&
            (!bkState.date || (b.created_at || '').slice(0, 10) === bkState.date)).length}</b></span>` +
        `<label class="form-hint">Дата <select id="bk-date"></select></label>` +
        `<label class="form-hint">Тип <select id="bk-type"></select></label>`;
      const dsel = box.querySelector('#bk-date');
      const ksel = box.querySelector('#bk-type');
      dsel.innerHTML = '<option value="">Все даты</option>' +
        Object.keys(dates).sort().reverse().map(d =>
          `<option value="${d}" ${bkState.date === d ? 'selected' : ''}>${d} (${dates[d]})</option>`).join('');
      ksel.innerHTML = '<option value="">Все типы</option>' +
        Object.keys(kindNames).filter(k => kinds[k]).map(k =>
          `<option value="${k}" ${bkState.kind === k ? 'selected' : ''}>${kindNames[k]} — ${kinds[k]}</option>`).join('');
      dsel.onchange = () => { bkState.date = dsel.value; renderBackups(items); };
      ksel.onchange = () => { bkState.kind = ksel.value; renderBackups(items); };
    };
    const renderBackups = (items) => {
      const el = $('#bk-list');
      if (!el) return;
      const rowsText = (b) => (b.rows
        ? `<span class="cell-mono" style="font-size:11.5px">${b.rows.receipts ?? '—'} чек. · ` +
          `${b.rows.users ?? '—'} польз. · ${b.rows.companies ?? '—'} комп.</span>` +
          (b.copy_version ? ` <span class="chip exported"><span class="dot"></span>v${esc(b.copy_version)}</span>` : '')
        : '<span class="form-hint">—</span>');
      const visible = items.filter(b => (!bkState.kind || b.kind === bkState.kind) &&
          (!bkState.date || (b.created_at || '').slice(0, 10) === bkState.date));
      el.innerHTML = visible.length
        ? `<table class="data" style="min-width:0"><thead><tr><th>Копия</th><th>Тип</th><th>В копии</th><th>Размер</th><th></th></tr></thead><tbody>
           ${visible.slice(0, 24).map(b => `<tr style="cursor:default">
             <td class="cell-mono" style="font-size:11.5px">${esc(b.name)}</td>
             <td>${kindNames[b.kind] || b.kind}${b.verified ? ' <span class="chip exported" title="Копия проверена при создании"><span class="dot"></span>✓</span>' : ''}${b.source ? ` <span class="form-hint" style="font-size:11.5px" title="Загружено из файла: ${esc(b.source)}">из файла</span>` : ''}</td>
             <td>${rowsText(b)}</td>
             <td>${b.size_kb} КБ</td>
             <td style="white-space:nowrap">
               <button class="btn btn-sm" data-verify="${esc(b.name)}"
                       title="Проверить копию: целостность базы и хеш">🛡</button>
               <button class="btn btn-sm" data-restore="${esc(b.name)}"
                       title="Восстановить базу из этой копии">↩ Восстановить</button>
               <a href="/api/v1/admin/backups/${encodeURIComponent(b.name)}/download" class="btn btn-sm" download title="Скачать копию">⬇</a>
             </td>
           </tr>`).join('')}</tbody></table>`
        : (items.length
          ? '<p class="form-hint">Нет копий по выбранным условиям фильтра</p>'
          : '<p class="form-hint">Копий пока нет — создайте первую кнопкой выше</p>');
      el.querySelectorAll('button[data-verify]').forEach(btn => {
        btn.onclick = async () => {
          const name = btn.getAttribute('data-verify');
          btn.disabled = true;
          try {
            const r = await api.post('/api/v1/admin/backups/verify', { name });
            toast(r.message, r.ok ? 'ok' : 'err', '🛡 Проверка копии');
          } catch (e) { toast(e.message, 'err', '🛡 Проверка копии'); }
          btn.disabled = false;
        };
      });
      el.querySelectorAll('button[data-restore]').forEach(btn => {
        btn.onclick = async () => {
          const name = btn.getAttribute('data-restore');
          if (!confirm(`Восстановить базу из копии «${name}»?\n\n` +
              'Текущие данные будут заменены данными копии.\n' +
              'Перед восстановлением автоматически создаётся страховая копия текущего состояния.\n\n' +
              'После восстановления потребуется перезапуск приложения.')) return;
          btn.disabled = true;
          try {
            const r = await api.post('/api/v1/admin/backups/restore', { name });
            toast(r.message, 'ok', '↩ Восстановление');
            try { const it = (await api.get('/api/v1/admin/backups')).items; renderBkFilters(it); renderBackups(it); } catch (_e) {}
          } catch (e) { toast(e.message, 'err', 'Восстановление'); btn.disabled = false; }
        };
      });
    };
    const loadBackups = async () => {
      try {
        const items = (await api.get('/api/v1/admin/backups')).items;
        renderBkFilters(items);
        renderBackups(items);
      }
      catch { const el = $('#bk-list'); if (el) el.innerHTML = '<p class="form-hint">Список недоступен</p>'; }
    };
    loadBackups();
    const bcr = $('#btn-bk-create');
    if (bcr) bcr.onclick = async () => {
      bcr.disabled = true;
      try {
        const kindSel = $('#bk-kind');
        const r = await api.post('/api/v1/admin/backups',
                                 { kind: kindSel ? kindSel.value : 'manual' });
        toast(r.message, 'ok', '💾'); renderBkFilters(r.items); renderBackups(r.items);
      }
      catch (e) { toast(e.message, 'err'); }
      bcr.disabled = false;
    };
    const brf = $('#btn-bk-refresh');
    if (brf) brf.onclick = loadBackups;
    // v1.49.0: загрузка копии из внешнего источника (файл с компьютера)
    const bif = $('#btn-bk-import'), bifFile = $('#bk-import-file');
    if (bif && bifFile) bif.onclick = () => bifFile.click();
    if (bifFile) bifFile.onchange = async () => {
      const f = bifFile.files && bifFile.files[0];
      bifFile.value = '';
      if (!f) return;
      bif.disabled = true;
      try {
        const fd = new FormData();
        fd.append('file', f, f.name);
        const resp = await fetch('/api/v1/admin/backups/import', {
          method: 'POST',
          headers: { 'Authorization': 'Bearer ' + getToken() },
          body: fd,
        });
        const r = await resp.json().catch(() => ({}));
        if (!resp.ok) throw new Error(r.detail || 'Загрузка не удалась');
        toast(r.message, 'ok', '⬆ Загрузка копии');
        renderBkFilters(r.items); renderBackups(r.items);
      } catch (e) { toast(e.message, 'err', '⬆ Загрузка копии'); }
      bif.disabled = false;
    };
  }

  // v1.46.0: «Последние рабочие версии» — откат одним щелчком
  // v1.48.0: секции без карточек (для роли) не показываем
  document.querySelectorAll('.settings-sect').forEach((h) => {
    const o = parseInt(h.style.order || '0', 10);
    const cards = container.querySelectorAll('.settings-grid > .glass.card');
    let has = false;
    cards.forEach((c) => {
      const co = parseInt(c.style.order || '60', 10);
      if (co >= o && co < o + 9) has = true;
    });
    if (!has) h.style.display = 'none';
  });

  if (isAdmin()) {
    let relPoll = null;
    const relBox = $('#rel-list');
    const relStop = () => { if (relPoll) { clearInterval(relPoll); relPoll = null; } };
    let relItems = [];
    const others = () => relItems.filter(v => !v.current);
    const relConfirm = (title, lines) =>
      confirm(title + '\n\n' + lines.join('\n'));

    const relRender = (items) => {
      if (!relBox) return;
      relItems = items || [];
      const cur = relItems.find(v => v.current);
      const head =
        '<div style="display:flex;gap:8px;align-items:center;flex-wrap:wrap;margin-bottom:8px">' +
        `<span class="form-hint">Сейчас работает: <b>v${esc(cur ? cur.version : '…')}</b> · ` +
        `в реестре ${relItems.length} из 3</span>` +
        '<button class="btn btn-sm" id="rel-refresh" title="Обновить список">🔄 Обновить</button></div>';
      if (!relItems.length) {
        relBox.innerHTML = head +
          '<p class="form-hint">Реестр пока пуст. Записи появляются автоматически: ' +
          'после каждого успешного старта программы и перед каждым обновлением. ' +
          'Как только вы обновитесь, предыдущая версия появится здесь и её можно ' +
          'будет откатить одной кнопкой. Пока можно откатиться к коммиту вручную (ниже).</p>';
      } else if (!others().length) {
        relBox.innerHTML = head +
          '<p class="form-hint">В реестре пока только текущая версия. Предыдущие рабочие ' +
          'версии появятся здесь после обновлений — и к ним можно будет откатиться кнопкой. ' +
          'Срочно нужен откат сейчас — воспользуйтесь откатом к коммиту (ниже) либо на сервере: ' +
          '<span class="cell-mono">sudo bash rollback.sh</span></p>';
      } else {
        relBox.innerHTML = head +
          '<table class="data" style="min-width:0"><thead>' +
          '<tr><th>Версия</th><th>Коммит</th><th>Когда работала</th><th></th></tr></thead><tbody>' +
          relItems.map(v => `<tr>
            <td><b>v${esc(v.version)}</b>${v.current
              ? ' <span class="chip exported"><span class="dot"></span>работает сейчас</span>'
              : (v.note ? `<div class="form-hint">${esc(v.note)}</div>` : '')}</td>
            <td class="cell-mono" style="font-size:11.5px">${esc(v.commit_short)}</td>
            <td>${esc((v.at || '').slice(0, 16).replace('T', ' '))}</td>
            <td>${v.current
              ? '<span class="form-hint">—</span>'
              : `<button class="btn btn-sm" data-rb="${esc(v.version)}|${esc(v.commit)}">↩ Откатиться</button>`}</td>
          </tr>`).join('') + '</tbody></table>';
      }
      // откат к версии из реестра
      relBox.querySelectorAll('button[data-rb]').forEach(btn => {
        btn.onclick = async () => {
          const [ver, commit] = btn.getAttribute('data-rb').split('|');
          if (!relConfirm(`Откатиться к рабочей версии v${ver}?`, [
              '• Файлы программы вернутся к проверенной версии.',
              '• База данных НЕ трогается; перед откатом — копия базы.',
              '• Приложение перезапустится (~10 секунд).'])) return;
          btn.disabled = true;
          try {
            const r = await api.post('/api/v1/admin/update/rollback',
                                     { version: ver, commit });
            toast(r.message, 'ok', '↩ Откат');
            relWatch();
          } catch (e) { toast(e.message, 'err', 'Откат'); btn.disabled = false; }
        };
      });
      // обновить список
      const rf = document.getElementById('rel-refresh');
      if (rf) rf.onclick = () => { rf.disabled = true; relLoad().then(() => { rf.disabled = false; }); };
      // откат к произвольному коммиту
      const go = document.getElementById('rb-commit-go');
      const inp = document.getElementById('rb-commit');
      if (go && inp) go.onclick = async () => {
        const c = inp.value.trim();
        if (!/^[0-9a-f]{7,40}$/.test(c)) {
          toast('Хеш коммита: 7–40 символов 0–9 a–f', 'err', '↩ Откат');
          return;
        }
        if (!relConfirm(`Откатиться к коммиту ${c.slice(0, 8)}?`, [
            '• Коммит должен быть рабочей версией программы.',
            '• База данных НЕ трогается; перед откатом — копия базы.',
            '• Приложение перезапустится (~10 секунд).'])) return;
        go.disabled = true;
        try {
          const r = await api.post('/api/v1/admin/update/rollback',
                                   { version: 'коммит ' + c.slice(0, 8), commit: c });
          toast(r.message, 'ok', '↩ Откат');
          relWatch();
        } catch (e) { toast(e.message, 'err', 'Откат'); go.disabled = false; }
      };
    };
    const relLoad = async () => {
      try {
        const r = await api.get('/api/v1/admin/update/releases');
        relRender(r.items || []);
      } catch (_e) {
        if (relBox) relBox.innerHTML = '<p class="form-hint">Список недоступен</p>';
      }
    };
    const relWatch = () => {
      relStop();
      const t0 = Date.now();
      relPoll = setInterval(async () => {
        if (!document.getElementById('rel-list')) { relStop(); return; }  // ушли со страницы
        try {
          const st = await api.get('/api/v1/admin/update/status');
          if (st.job && st.job.running) {
            if (relBox) relBox.innerHTML =
              `<div class="chip new"><span class="dot"></span>${esc(st.job.step || 'выполняется')}… ${st.job.progress || 0}%</div>` +
              (st.job.log || []).slice(-2).map(l => `<div class="form-hint">${esc(l)}</div>`).join('');
          } else {
            relStop();
            if (st.job && st.job.finished) {
              if (st.job.success) toast('Откат выполнен — страница сейчас перезагрузится', 'ok', '↩');
              else toast(st.job.error || 'Откат не удался — состояние возвращено', 'err', '↩ Откат');
              setTimeout(() => location.reload(), 2500);
              return;
            }
            relLoad();
          }
        } catch (_e) { /* сеть мигнула — ждём */ }
        if (Date.now() - t0 > 300000) { relStop(); relLoad(); }
      }, 1500);
    };
    relLoad();
  }

  if (isAdmin()) {
    const loadUpdCard = async (fresh) => {
      try {
        const st = await api.get('/api/v1/admin/system');
        const cur = $('#upd-current'); if (cur) cur.textContent = 'v' + st.version;
        // v1.9.0: хэш сборки — сразу видно, что обновление реально применилось
        const cm = $('#upd-commit');
        if (cm) { cm.textContent = (st.commit || '—').slice(0, 7) || '—'; cm.title = st.commit || ''; }
        const ri = $('#upd-repo'); if (ri) ri.value = st.repo_url || '';
        const bi = $('#upd-branch-input'); if (bi) bi.value = st.branch || '';
      } catch {}
      // v1.8.1: когда обновлялись в последний раз
      api.get('/api/v1/admin/update/status').then(r => {
        const ls = r.last_success, el = $('#upd-last');
        if (el) el.textContent = ls && ls.to
          ? `v${ls.to} · ${new Date(ls.at).toLocaleString('ru-RU')}`
          : 'ещё не было';
      }).catch(() => {});
      // v1.9.1: готовность к обновлению (GitHub / право на перезапуск / копия БД)
      api.get('/api/v1/admin/update/preflight').then(pf => {
        const el = $('#upd-ready');
        if (!el) return;
        const chip = (name, ok, hint) =>
          `<span class="pf-chip ${ok ? 'ok' : 'bad'}" title="${hint}">${ok ? '✓' : '✗'} ${name}</span>`;
        const rmode = pf.reexec_ok
          ? (pf.restart_mode === 'sudoers' || pf.restart_mode === 'systemctl'
              ? 'sudoers + самоперезапуск' : 'самоперезапуск без пароля')
          : 'нет права';
        el.innerHTML = [
          chip('GitHub', pf.github_ok, 'Сервер может скачать обновление'),
          chip('Git-репозиторий', pf.git_ok !== false,
            pf.git_ok === false ? 'Будет восстановлен автоматически при обновлении' : 'git на месте'),
          chip('Перезапуск', pf.can_restart,
            pf.can_restart ? 'Режим: ' + rmode : 'Недоступен — sudo bash deploy.sh --update'),
          chip('Копия БД', pf.db_backup_ok, 'Перед обновлением будет сделана резервная копия'),
        ].join(' ') + (pf.ready
          ? '<span class="form-hint" style="margin-left:8px">готово к обновлению из приложения</span>'
          : '<span class="form-hint" style="margin-left:8px">обновление из приложения недоступно</span>');
      }).catch(() => { const el = $('#upd-ready'); if (el) el.textContent = ''; });
      setUpdState('checking');
      try {
        // fresh=true (кнопка) — всегда свежая проверка; иначе можно из кэша
        const url = fresh ? '/api/v1/admin/update/check?force=1'
                          : '/api/v1/admin/update/check?cache=1';
        const r = await api.get(url, { retries: 1 });
        const ch = $('#upd-checked');
        if (r.ok === false) {                    // v1.6.0: структурированный ответ
          state.githubUnreachable = true;
          state.updateAvailable = null;
          const avail = $('#upd-avail'); if (avail) avail.classList.add('hidden');
          if (ch) ch.textContent = '—';
          setUpdState('error', r.error);
          return;
        }
        state.githubUnreachable = false;
        if (ch) { ch.textContent = new Date(r.checked_at).toLocaleTimeString('ru-RU');
                  ch.title = 'канал: ' + (r.source || ''); }
        state.updateAvailable = r.update_available ? r : null;
        const avail = $('#upd-avail');
        if (avail) avail.classList.toggle('hidden', !r.update_available);
        const rem = $('#upd-remote'); if (rem) rem.textContent = 'v' + r.remote_version;
        setUpdState(r.update_available ? 'available' : 'latest', null, r);
      } catch (e) {
        state.githubUnreachable = true;
        setUpdState('error', e.message);
      }
    };
    loadUpdCard();
    // v1.52.1: обновление выполняется — вернуть окно хода (если не закрыли его сами)
    api.get('/api/v1/admin/update/status').then(r => {
      const st = r.job || {};
      if (st.running && !st.finished && st.started_at
          && state.updAutoDismissed !== st.started_at) openUpdateProgress();
    }).catch(() => {});
    const bc = $('#btn-check-update');
    if (bc) bc.onclick = async () => {
      bc.disabled = true; bc.textContent = 'Проверяю…';
      await loadUpdCard(true);
      bc.disabled = false; bc.textContent = '🔍 Проверить обновления';
      if (state.githubUnreachable) toast('GitHub недоступен с сервера — воспользуйтесь ручной инструкцией в карточке', 'err', '🔄 Обновление');
    };
    const bsr = $('#btn-save-repo');
    if (bsr) bsr.onclick = async () => {
      bsr.disabled = true;
      try {
        const r = await api.put('/api/v1/admin/update/repo', {
          repo_url: $('#upd-repo').value.trim() || undefined,
          repo_branch: $('#upd-branch-input').value.trim() || undefined });
        toast(r.message, 'ok', '🔄');
      } catch (e) { toast(e.message, 'err'); }
      bsr.disabled = false;
    };
    const ba = $('#btn-apply-update');
    if (ba) ba.onclick = () => {
      // v1.10.0: через диалог — там предпроверка и поле пароля сервера
      if (state.updateAvailable) showUpdateDialog(state.updateAvailable);
    };
    const bw = $('#upd-whats');
    if (bw) bw.onclick = (e) => { e.preventDefault(); api.get('/api/v1/admin/update/changelog').then(r => openModal(`<div class="modal-title">📜 Что изменится</div><pre class="codeblock" style="max-height:55vh">${esc(r.changelog.slice(0, 6000))}</pre>`)); };
    const bl = $('#btn-changelog');
    if (bl) bl.onclick = () => api.get('/api/v1/admin/update/changelog').then(r => openModal(`<div class="modal-title">📜 История версий</div><pre class="codeblock" style="max-height:55vh">${esc(r.changelog.slice(0, 6000))}</pre>`));
    const bb = $('#btn-backup');
    if (bb) bb.onclick = async () => {
      try { const { blob, filename } = await api.download('/api/v1/admin/backup'); downloadBlob(blob, filename); toast('Резервная копия скачана: ' + filename, 'ok'); }
      catch (e) { toast(e.message, 'err'); }
    };
  }

  // v1.17.0: «Сервер и команды» + сброс кэша
  if (isAdmin() && sysInfo) {
    const si = $('#sys-info');
    if (si) {
      const up = sysInfo.uptime_s
        ? Math.floor(sysInfo.uptime_s / 86400) + ' д. ' + Math.floor(sysInfo.uptime_s % 86400 / 3600) + ' ч.'
        : '—';
      si.innerHTML = `
        <dt>Версия / сборка</dt><dd>v${esc(sysInfo.version)} · <code class="inline">${esc((sysInfo.commit || '').slice(0, 7) || '—')}</code></dd>
        <dt>Python</dt><dd>${esc(sysInfo.python)}</dd>
        <dt>Работает</dt><dd>${up} · БД ${esc(String(sysInfo.db_size_mb))} МБ</dd>
        <dt>Данных</dt><dd>компаний ${sysInfo.counts.companies} · пользователей ${sysInfo.counts.users} · чеков ${fmtInt(sysInfo.counts.receipts)}</dd>
        <dt>Служебный пользователь</dt><dd><code class="inline">${esc(sysInfo.service_user)}</code>${sysInfo.reexec ? ' · самоперезапуск ✅' : ''}</dd>`;
    }
    const cmds = [
      ['Разрешить автоперезапуск (однократно)', sysInfo.sudoers_cmd],
      ['Обновить из терминала', 'sudo bash deploy.sh --update'],
      ['Статус сервиса', 'systemctl status ' + sysInfo.unit],
      ['Логи сервера', 'journalctl -u ' + sysInfo.unit + ' -f'],
    ];
    const sc = $('#sys-cmds');
    if (sc) sc.innerHTML = cmds.map(([name, cmd], i) => `
      <div style="margin-bottom:10px">
        <div class="form-hint" style="margin-bottom:4px">${esc(name)}</div>
        <div class="cmd-line"><code>${esc(cmd)}</code>
          <button class="btn btn-sm cmd-copy" data-cmd="${esc(cmd)}">копировать</button></div>
      </div>`).join('');
    sc && sc.querySelectorAll('.cmd-copy').forEach(b => {
      b.onclick = () => copyCmd(b.dataset.cmd, b);
    });
  }
  makeSettingsCollapsible(container);   // v1.21.0: сворачиваемые блоки
  bindAppearance();                 // v1.25.0: «Спокойный час» (уведомления)
  const bcr = $('#btn-cache-reset');
  if (bcr) bcr.onclick = () => hardReset();

  // v1.30.0: Чек-Пул — включение приёма чеков
  if (isAdmin() && poolSet) {
    const psv = $('#pool-save');
    if (psv) psv.onclick = async () => {
      psv.disabled = true;
      try {
        const r = await api.put('/api/v1/pool-admin/settings',
                                { enabled: $('#pool-enabled').checked,
                                  pick_limit: parseInt($('#pool-pick-limit').value, 10) || 0 });
        toast(r.message, r.enabled ? 'ok' : 'warn', '🧩');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
      psv.disabled = false;
    };
    // v1.39.0: платное API — переключатель и ключи
    const aps = $('#pool-api-enabled');
    if (aps) aps.onchange = async () => {
      try {
        const r = await api.put('/api/v1/pool-admin/settings',
                                { enabled: $('#pool-enabled').checked,
                                  api_enabled: aps.checked });
        toast(aps.checked ? 'API включён' : 'API выключен', aps.checked ? 'ok' : 'warn', '🔌');
      } catch (e) { toast(e.message, 'err'); aps.checked = !aps.checked; }
    };
    poolApiKeysLoad();
  }

  // v1.32.0: SMTP кабинета Чек-Пула — сохранение и тест письма
  if (isAdmin() && poolSet && smtpSet) {
    const pss = $('#pool-smtp-save');
    if (pss) pss.onclick = async () => {
      pss.disabled = true;
      try {
        const body = {
          host: $('#pool-smtp-host').value.trim(),
          port: parseInt($('#pool-smtp-port').value, 10) || 587,
          user: $('#pool-smtp-user').value.trim(),
          sender: $('#pool-smtp-from').value.trim(),
          base_url: $('#pool-smtp-base').value.trim(),
          tls: $('#pool-smtp-tls').checked,
        };
        const pw = $('#pool-smtp-pass').value;
        if (pw) body.password = pw;
        const r = await api.put('/api/v1/pool-admin/smtp', body);
        toast(r.message, 'ok', '✉️');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
      pss.disabled = false;
    };
    const pst = $('#pool-smtp-test');
    if (pst) pst.onclick = async () => {
      pst.disabled = true;
      try {
        const r = await api.post('/api/v1/pool-admin/smtp-test',
                                 { email: $('#pool-smtp-test-email').value.trim() });
        toast(r.message, r.ok ? 'ok' : 'err', '✉️');
      } catch (e) { toast(e.message, 'err'); }
      pst.disabled = false;
    };
  }

  // v1.16.0: Telegram — сохранение/тест/личная привязка
  if (isAdmin() && tgSet) {
    const tts = $('#tg-save');
    if (tts) tts.onclick = async () => {
      tts.disabled = true;
      try {
        const body = {
          enabled: $('#tg-enabled').checked,
          reminder_time: $('#tg-time').value.trim(),
          reminder_text: $('#tg-text').value.trim(),
        };
        const tok = $('#tg-token').value.trim();
        if (tok) body.bot_token = tok;
        const prx = $('#tg-proxy').value.trim();          // v1.29.0
        if (prx) body.proxy = prx;
        const r = await api.put('/api/v1/settings/telegram', body);
        toast(r.message, r.worker_running ? 'ok' : 'warn', '🔔');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
      tts.disabled = false;
    };
    const tgd = $('#tg-diag');                            // v1.29.0
    if (tgd) tgd.onclick = async () => {
      const out = $('#tg-diag-out');
      out.textContent = 'Проверяю путь до api.telegram.org…';
      try {
        const r = await api.post('/api/v1/settings/telegram/diagnose', {});
        const mark = r.verdict === 'ok' ? '✅' : '❌';
        out.innerHTML = `${mark} ${esc(r.message)}<br>` +
          `<span class="form-hint">напрямую: ${r.direct.ok ? 'доступен' : 'блокируется' +
            (r.direct.error ? ' (' + esc(r.direct.error) + ')' : '')}` +
          ` · через прокси: ${r.proxy.ok ? 'доступен' : 'не задан/не работает'}</span>`;
      } catch (e) { out.textContent = 'Ошибка: ' + e.message; }
    };
    const ttt = $('#tg-test');
    if (ttt) ttt.onclick = async () => {
      try { const r = await api.post('/api/v1/settings/telegram/test', {});
        toast(r.message, 'ok', '🔔'); }
      catch (e) { toast(e.message, 'err'); }
    };
    const tgr = $('#tg-refresh');
    if (tgr) tgr.onclick = async () => {
      try { const r = await api.post('/api/v1/settings/telegram/refresh-bot', {});
        toast('Бот: @' + r.bot_username, 'ok', '🔔'); route(true); }
      catch (e) { toast(e.message, 'err'); }
    };
  }
  if (me) {
    const tc = $('#tg-code');
    if (tc) tc.onclick = async () => {
      try {
        const r = await api.post('/api/v1/users/me/telegram/bind', {});
        $('#tg-code-out').textContent = r.code;
        $('#tg-code-hint').textContent = r.instruction + ' — код действует 15 минут.';
        toast('Код получен — отправьте боту /start ' + r.code, 'ok', '🔔');
      } catch (e) { toast(e.message, 'err'); }
    };
    const tu = $('#tg-unbind');
    if (tu) tu.onclick = async () => {
      if (!confirm('Отвязать Telegram-уведомления?')) return;
      try { const r = await api.post('/api/v1/users/me/telegram/unbind', {});
        toast(r.message, 'ok'); route(true); }
      catch (e) { toast(e.message, 'err'); }
    };
  }

  // v1.15.0: ключ Checko — сохранение и показ по паролю (секрет)
  if (isAdmin() && checkoSet) {
    const ck = $('#checko-key');
    const cstat = $('#checko-status');
    const cs = $('#checko-save');
    if (cs) cs.onclick = async () => {
      const val = (ck.value || '').trim();
      if (!val) return toast('Вставьте API-ключ из личного кабинета checko.ru', 'warn');
      cs.disabled = true;
      try {
        const r = await api.put('/api/v1/settings/checko', { api_key: val });
        ck.value = '';
        if (cstat) cstat.textContent = r.message;
        toast(r.message, 'ok', '🔒');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
      cs.disabled = false;
    };
    const ct = $('#checko-test');
    if (ct) ct.onclick = async () => {
      ct.disabled = true; ct.textContent = '…запрашиваю ЕГРЮЛ';
      try {
        const r = await api.post('/api/v1/settings/checko/test', {});
        if (cstat) cstat.textContent = r.message +
          (r.meta && r.meta.today_request_count !== undefined
            ? ` · запросов сегодня: ${r.meta.today_request_count}` : '');
        toast(r.message, 'ok', '🗂');
      } catch (e) {
        toast(e.message, 'err');
        if (cstat) cstat.textContent = 'Ошибка: ' + e.message;
      }
      ct.disabled = false; ct.textContent = '🧪 Проверить ключ';
    };
    const cr = $('#checko-reveal');
    if (cr) cr.onclick = async () => {
      const pwd = prompt('Подтвердите пароль администратора, чтобы показать ключ:');
      if (!pwd) return;
      cr.disabled = true;
      try {
        const r = await api.post('/api/v1/settings/checko/reveal', { password: pwd });
        ck.type = 'text';
        ck.value = r.api_key;
        ck.readOnly = true;
        toast('Ключ показан (записан в аудит). Скройте через 30 секунд автоматически', 'warn', '👁');
        setTimeout(() => {
          ck.value = ''; ck.type = 'password'; ck.readOnly = false;
          if (cstat) cstat.textContent = 'Ключ скрыт. Повторите «Показать», если нужен снова.';
        }, 30000);
      } catch (e) { toast(e.message, 'err'); }
      cr.disabled = false;
    };
  }

  if (isAdmin() && ext) {
    $('#ext-save').onclick = async () => {
      try {
        await api.put('/api/v1/settings/external', {
          proverkacheka_token: $('#ext-pke').value.trim() || undefined,
          external_order: $('#ext-order').value.trim(),
          external_auto: $('#ext-auto').checked,
        });
        toast('Источники данных сохранены', 'ok');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
    };
    $('#ext-test').onclick = async () => {
      const st = $('#ext-status');
      st.textContent = 'Проверяю (учтите паузу 2–7 с между запросами)…';
      try {
        const r = await api.post('/api/v1/settings/external/test', {});
        st.innerHTML = r.ok
          ? `Результат: источник <b>${esc(r.source || '—')}</b>, чек ${r.found ? 'найден' : 'не найден'}${r.items_count ? `, позиций: ${r.items_count}` : ''}. ${esc(r.message)}`
          : `Не удалось: ${esc(r.message)}`;
      } catch (e) { st.textContent = 'Ошибка: ' + e.message; }
    };
  }

  if (isAdmin() && appSet) {
    $('#app-save').onclick = async () => {
      try {
        await api.put('/api/v1/settings/app', {
          auto_verify: $('#app-autoverify').checked,
          advance_deadline_days: parseInt($('#app-deadline').value, 10) || 10 });
        toast('Настройки сохранены', 'ok');
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  if (isAdmin() && fns) {
    $('#seg-mock').onclick = async () => { await saveFns('mock'); };
    $('#seg-fns').onclick = async () => {
      const token = $('#fns-token').value.trim();
      if (!token && !fns.has_master_token) {
        return toast('Для реального API нужен Мастер-токен ФНС', 'err', 'Нет токена');
      }
      await saveFns('fns');
    };
    async function saveFns(provider) {
      try {
        await api.put('/api/v1/settings/fns', {
          provider,
          master_token: $('#fns-token').value.trim() || undefined,
          client_app_id: $('#fns-appid').value.trim() || undefined,
        });
        toast('Настройки ФНС сохранены', 'ok');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
    }
    $('#onec-regen').onclick = async () => {
      if (!confirm('Сгенерировать новый токен? Старый в 1С перестанет работать.')) return;
      await api.put('/api/v1/settings/onec', { regen_token: true });
      toast('Токен обновлён', 'ok');
      route(true);
    };
    $('#onec-reveal').onclick = async () => {
      const s = await api.get('/api/v1/settings/onec/reveal');
      $('#onec-token').value = s.api_token;
    };
  }
  $('#p-save').onclick = async () => {
    try {
      await api.post('/api/v1/auth/change-password', {
        old_password: $('#p-old').value,
        new_password: $('#p-new').value,
      });
      toast('Пароль изменён', 'ok');
      $('#p-old').value = $('#p-new').value = '';
    } catch (e) { toast(e.message, 'err'); }
  };
  waBindSettingsCard();               // v1.43.0: карточка «Быстрый вход» (все роли)
  bindMailCenter();                   // v1.44.0: почтовый центр (админ)
}

// v1.44.0: Почтовый центр — SMTP/шаблоны + бот рассылок + журнал.
async function bindMailCenter() {
  const $m = (id) => document.getElementById(id);
  if (!$m('mail-card')) return;
  let cfg = null, rules = [];
  const DAY_TITLES = { daily: 'ежедневно', mon: 'понедельник', tue: 'вторник',
    wed: 'среда', thu: 'четверг', fri: 'пятница', sat: 'суббота', sun: 'воскресенье' };
  const AUD = {
    pool_unverified: 'без подтверждённого e-mail',
    pool_inactive: 'не сдавали чеки N дней',
    pool_all: 'все с подтверждённым e-mail',
    custom: 'список адресов' };
  try { cfg = await api.get('/api/v1/mail-admin/config'); }
  catch (e) { $m('mc-rules').textContent = e.message || 'Ошибка загрузки'; return; }
  $m('mc-host').value = cfg.host || '';
  $m('mc-port').value = cfg.port || 587;
  $m('mc-user').value = cfg.user || '';
  $m('mc-sender').value = cfg.sender || '';
  $m('mc-fromname').value = cfg.from_name || '';
  $m('mc-baseurl').value = cfg.base_url || '';
  $m('mc-tls').checked = !!cfg.tls;
  $m('mc-greeting').value = cfg.greeting || '';
  $m('mc-signature').value = cfg.signature || '';
  $m('mc-bot').checked = !!cfg.bot_on;
  $m('mc-pass').placeholder = cfg.has_password ? 'задан — оставьте пустым' : 'пароль ящика';

  $m('mc-save').onclick = async () => {
    try {
      const r = await api.put('/api/v1/mail-admin/config', {
        host: $m('mc-host').value.trim(), port: Number($m('mc-port').value) || 587,
        user: $m('mc-user').value.trim(),
        password: $m('mc-pass').value || '',
        sender: $m('mc-sender').value.trim(),
        tls: $m('mc-tls').checked,
        base_url: $m('mc-baseurl').value.trim(),
        from_name: $m('mc-fromname').value.trim(),
        greeting: $m('mc-greeting').value.trim(),
        signature: $m('mc-signature').value.trim(),
        bot_on: $m('mc-bot').checked,
      });
      $m('mc-pass').value = '';
      toast(r.message, 'ok', 'Почтовый центр');
    } catch (e) { toast(e.message, 'err', 'Почтовый центр'); }
  };
  $m('mc-test').onclick = async () => {
    const to = $m('mc-testto').value.trim();
    if (!to.includes('@')) { toast('Укажите адрес для теста', 'err'); return; }
    try {
      const r = await api.post('/api/v1/mail-admin/test', { email: to });
      toast(r.message, r.ok ? 'ok' : 'err', 'Тестовое письмо');
    } catch (e) { toast(e.message, 'err'); }
  };

  const loadRules = async () => {
    try { rules = (await api.get('/api/v1/mail-admin/rules')).items; }
    catch (e) { $m('mc-rules').textContent = e.message; return; }
    const box = $m('mc-rules');
    if (!rules.length) {
      box.innerHTML = 'Правил нет. Создайте: например, «Напомнить подтвердить e-mail» ' +
        '(условие, аудитория «без подтверждённого e-mail», 3 дня) — бот сам напишет каждому.';
    } else {
      box.innerHTML = rules.map(r => `
        <div style="display:flex;gap:10px;align-items:center;padding:7px 0;border-bottom:1px solid var(--line,#eef0f4)">
          <input type="checkbox" data-rr="${r.id}" ${r.enabled ? 'checked' : ''} style="width:auto" title="включено">
          <div style="flex:1"><b>${esc(r.name)}</b>
            <div class="form-hint">${r.trigger === 'schedule'
              ? (DAY_TITLES[r.schedule] || r.schedule) + ' в ' + esc(r.hh_mm)
              : 'условие: ' + esc(AUD[r.condition_key] || r.condition_key) + ' (' + r.cond_days + ' дн.), после ' + esc(r.hh_mm)}
            · ${esc(r.subject)}${r.last_result ? ' · ' + esc(r.last_result) : ''}</div></div>
          <button class="btn btn-sm" data-run="${r.id}" title="Запустить сейчас">▶</button>
          <button class="btn btn-sm btn-bad" data-del="${r.id}" title="Удалить">✕</button>
        </div>`).join('');
      box.querySelectorAll('[data-rr]').forEach(c => c.onchange = async () => {
        try { await api.patch('/api/v1/mail-admin/rules/' + c.dataset.rr,
          { enabled: c.checked }); toast('Сохранено', 'ok'); }
        catch (e) { toast(e.message, 'err'); }
      });
      box.querySelectorAll('[data-run]').forEach(b => b.onclick = async () => {
        try {
          const r = await api.post(`/api/v1/mail-admin/rules/${b.dataset.run}/run`, {});
          toast(r.message, 'ok', 'Рассылка');
          loadRules(); loadLog();
        } catch (e) { toast(e.message, 'err'); }
      });
      box.querySelectorAll('[data-del]').forEach(b => b.onclick = async () => {
        if (!confirm('Удалить правило?')) return;
        try { await api.del('/api/v1/mail-admin/rules/' + b.dataset.del);
          toast('Удалено', 'ok'); loadRules(); }
        catch (e) { toast(e.message, 'err'); }
      });
    }
  };

  $m('mc-newrule').onclick = () => {
    const { slot, close } = openModal(`
      <div class="modal-title">🤖 Новое правило рассылки</div>
      <div class="form-grid" style="margin-top:8px">
        <label class="field"><span>Название</span>
          <input id="nr-name" placeholder="Возвращение участника"></label>
        <label class="field"><span>Тип</span>
          <select id="nr-trigger">
            <option value="schedule">По расписанию (день и время)</option>
            <option value="condition">По условию (раз в сутки)</option>
          </select></label>
        <div id="nr-sched-wrap">
          <label class="field"><span>День</span>
            <select id="nr-schedule"><option value="daily">Ежедневно</option>
              ${Object.keys(DAY_TITLES).filter(k => k !== 'daily').map(k =>
                `<option value="${k}">${DAY_TITLES[k]}</option>`).join('')}
            </select></label>
        </div>
        <div id="nr-cond-wrap" style="display:none">
          <label class="field"><span>Аудитория</span>
            <select id="nr-aud">
              ${Object.entries(AUD).map(([k, v]) => `<option value="${k}">${v}</option>`).join('')}
            </select></label>
          <label class="field"><span>Дней (порог условия)</span>
            <input id="nr-days" type="number" value="14" min="1" max="365"></label>
        </div>
        <label class="field"><span>Время ( HH:MM)</span>
          <input id="nr-time" value="09:00" placeholder="09:00"></label>
        <label class="field"><span>Тема письма</span>
          <input id="nr-subject" placeholder="Мы вас ждём в Чек-Пуле"></label>
        <label class="field"><span>Текст ({name}, {days}, {points}, {count}, {stats}, {link})</span>
          <textarea id="nr-body" rows="4" placeholder="Вы не сдавали чеки {days} дней. Вернитесь — баллы ждут: {link}"></textarea></label>
        <label class="field" id="nr-rcpt-wrap" style="display:none"><span>Адреса через запятую</span>
          <input id="nr-rcpt" placeholder="dir@company.ru"></label>
      </div>
      <div class="modal-actions">
        <button class="btn" id="nr-cancel">Отмена</button>
        <button class="btn btn-primary" id="nr-ok">Создать</button>
      </div>`);
    const trig = slot.querySelector('#nr-trigger');
    trig.onchange = () => {
      slot.querySelector('#nr-sched-wrap').style.display = trig.value === 'schedule' ? '' : 'none';
      slot.querySelector('#nr-cond-wrap').style.display = trig.value === 'condition' ? '' : 'none';
      slot.querySelector('#nr-rcpt-wrap').style.display =
        (trig.value === 'condition' && slot.querySelector('#nr-aud').value === 'custom') ? '' : 'none';
    };
    slot.querySelector('#nr-aud').onchange = () => trig.onchange();
    slot.querySelector('#nr-cancel').onclick = close;
    slot.querySelector('#nr-ok').onclick = async () => {
      try {
        const r = await api.post('/api/v1/mail-admin/rules', {
          name: slot.querySelector('#nr-name').value.trim(),
          trigger: trig.value,
          schedule: slot.querySelector('#nr-schedule').value,
          hh_mm: slot.querySelector('#nr-time').value.trim() || '09:00',
          condition_key: slot.querySelector('#nr-aud').value,
          cond_days: Number(slot.querySelector('#nr-days').value) || 14,
          recipients: slot.querySelector('#nr-rcpt').value.trim(),
          subject: slot.querySelector('#nr-subject').value.trim(),
          body: slot.querySelector('#nr-body').value.trim(),
        });
        toast(r.message, 'ok', 'Бот рассылок');
        close(); loadRules();
      } catch (e) { toast(e.message, 'err', 'Бот рассылок'); }
    };
  };

  const loadLog = async () => {
    try {
      const d = await api.get('/api/v1/mail-admin/log?limit=12');
      const st = (s) => s === 'sent' ? '<span class="chip exported"><span class="dot"></span>отправлено</span>'
        : (s === 'error' ? '<span class="chip failed"><span class="dot"></span>ошибка</span>'
          : '<span class="chip unknown"><span class="dot"></span>пропущено</span>');
      document.getElementById('mc-log').innerHTML = d.items.length ? `
        <div class="card-title" style="margin-top:10px">Журнал отправок
          <span class="form-hint">хранение 90 дней (152-ФЗ)</span></div>
        <div class="table-wrap"><table class="data"><thead><tr>
          <th>Когда</th><th>Кому</th><th>Тема</th><th>Статус</th></tr></thead><tbody>
          ${d.items.map((l, i) => `<tr style="cursor:default">
            <td class="cell-date">${fmtDate(l.created_at)}</td>
            <td>${esc(d.masked[i] || l.to_email)}</td>
            <td>${esc(l.subject || '—')}</td>
            <td>${st(l.status)}${l.error ? `<div class="form-hint">${esc(l.error)}</div>` : ''}</td>
          </tr>`).join('')}</tbody></table></div>`
        : '';
    } catch (e) { /* журнал не критичен */ }
  };
  loadRules(); loadLog();
}

// v1.43.0: карточка «Быстрый вход» в настройках — список устройств,
// привязка текущего и отвязка. Доступна всем ролям.
async function waBindSettingsCard() {
  const list = $('#wa-list'), actions = $('#wa-actions');
  if (!list || !actions) return;
  if (!await waPlatformAvailable()) {
    list.innerHTML = 'Это устройство не поддерживает вход по отпечатку или лицу ' +
      '(нужен телефон/ноутбук со сканером: Windows Hello, Touch ID, Android Biometric). ' +
      'Вход по паролю работает как раньше.';
    return;
  }
  let items = [];
  try { items = (await api.get('/api/v1/auth/passkeys')).items; }
  catch (e) { list.textContent = e.message || 'Не удалось загрузить'; return; }
  list.innerHTML = items.length ? items.map(k =>
    `<div style="display:flex;gap:10px;align-items:center;padding:6px 0;border-bottom:1px solid var(--line,#eef0f4)">
       <span aria-hidden="true">🔑</span>
       <div style="flex:1"><b>${esc(k.label)}</b>
         <div class="form-hint">привязано: ${fmtDate(k.created_at)}${k.last_used_at ? ' · последний вход: ' + fmtDate(k.last_used_at) : ''}</div></div>
       <button class="btn btn-sm btn-bad" data-wa="${esc(k.id)}" data-name="${esc(k.label)}">Отвязать</button>
     </div>`).join('')
    : 'Привяжите это устройство — и входить можно будет по отпечатку/лицу, без пароля.';
  actions.innerHTML = '<button class="btn btn-sm btn-primary" id="wa-add">+ Привязать это устройство</button>';
  $('#wa-add').onclick = async () => {
    const name = prompt('Название устройства (например, «Телефон Марии»)', '') || '';
    try {
      const r = await waRegisterCore(name);
      toast(r.message, 'ok', 'Быстрый вход');
      waBindSettingsCard();
    } catch (e) {
      toast(e.waCancelled ? 'Устройство отменило запрос — попробуйте ещё раз'
                          : (e.message || 'Не удалось привязать'), 'err', 'Быстрый вход');
    }
  };
  list.querySelectorAll('[data-wa]').forEach(b => {
    b.onclick = async () => {
      if (!confirm('Отвязать «' + b.dataset.name + '»? Вход по нему станет невозможен.')) return;
      try {
        const r = await api.del('/api/v1/auth/passkeys/' + b.dataset.wa);
        toast(r.message, 'ok', 'Быстрый вход');
        waBindSettingsCard();
      } catch (e) { toast(e.message, 'err'); }
    };
  });
}

// --------------------------------------------------------------------------
//  PWA: Service Worker
// --------------------------------------------------------------------------
function registerServiceWorker() {
  if ('serviceWorker' in navigator && location.protocol.startsWith('http')) {
    navigator.serviceWorker.register('/sw.js').then(reg => {
      // v1.8.0: новый Service Worker установлен → предлагаем перезагрузку
      reg.addEventListener('updatefound', () => {
        const nw = reg.installing;
        if (!nw) return;
        nw.addEventListener('statechange', () => {
          if (nw.state === 'installed' && navigator.serviceWorker.controller) {
            // v1.50.0: после обновления из окна — там уже всё сообщено, не дублируем
            let justUpdated = false;
            try { justUpdated = sessionStorage.getItem('ymaster-updated') === '1'; } catch (e) {}
            if (justUpdated) { try { sessionStorage.removeItem('ymaster-updated'); } catch (e) {} return; }
            toast('Обновление загружено — нажмите, чтобы перезагрузить страницу',
                  'info', '🔄');
            document.querySelector('.toast:last-child')?.addEventListener('click',
              () => location.reload(), { once: true });
          }
        });
      });
    }).catch(() => { /* не критично */ });
  }
}

// --------------------------------------------------------------------------
boot();
