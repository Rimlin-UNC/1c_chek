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
  camera: null,
  view: 'dashboard',
  routeParam: '',
  receiptsSelected: new Set(),
  recents: JSON.parse(localStorage.getItem('ymaster_recents') || '[]'),
};

const VIEW_TITLES = {
  dashboard: 'Дашборд', scan: 'Сканирование чеков', receipts: 'База чеков',
  export: 'Выгрузка в 1С', mapping: 'Маппинг реквизитов', users: 'Пользователи и приглашения',
  audit: 'Журнал действий', settings: 'Настройки', companies: 'Компании',
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
  showLogin();
}

// Экран «Подключение…» — исключает мигание карточки входа при проверке токена
function showSplash(show) {
  let el = document.getElementById('boot-splash');
  if (!el) {
    el = document.createElement('div');
    el.id = 'boot-splash';
    el.style.cssText = 'position:fixed;inset:0;z-index:300;background:#0b1020;display:flex;flex-direction:column;gap:14px;align-items:center;justify-content:center;color:#9aa3bd;font-size:14px';
    el.innerHTML = '<img src="/img/logo.svg" width="52" height="52" alt=""><b>Ямастер Чек</b><span class="spinner"></span>';
    document.body.appendChild(el);
  }
  el.style.display = show ? 'flex' : 'none';
}

function showLogin() {
  $('#register-screen').classList.add('hidden');
  $('#login-screen').classList.remove('hidden');
  $('#app-shell').classList.add('hidden');
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

const isViewingAs = () => !!(state.me && state.me.viewing_as);

function viewAsForget() {
  try { sessionStorage.removeItem(VIEWAS_TOK); } catch (e) {}
  try { sessionStorage.removeItem(VIEWAS_NAME); } catch (e) {}
}

// Полоса возврата под шапкой + скрытие «Выйти» в режиме просмотра
function applyViewAsMode() {
  const bar = $('#viewas-bar');
  const chip = $('#user-chip');
  if (!bar || !chip) return;
  const viewing = isViewingAs();
  bar.classList.toggle('hidden', !viewing);
  $('#btn-logout').classList.toggle('hidden', viewing);
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
  chip.onclick = (e) => {
    if (e.target.closest('#btn-logout')) return;   // «Выйти» — отдельное действие
    togglePersonaMenu();
  };
  chip.onkeydown = (e) => {
    if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); togglePersonaMenu(); }
  };
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
    if (!document.hidden && state.me) route(true);
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
  const hash = location.hash.replace(/^#\//, '') || 'dashboard';
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
  };
  if (view in guard && !guard[view]) { location.hash = '#/dashboard'; return; }

  const renderers = {
    dashboard: viewDashboard, scan: viewScan, receipts: viewReceipts,
    export: viewExport, mapping: viewMapping, users: viewUsers,
    audit: viewAudit, settings: viewSettings, companies: viewCompanies,
  };
  (renderers[view] || viewDashboard)(container);
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
  const closeSidebar = () => {
    $('#sidebar').classList.remove('open');
    document.body.classList.remove('sb-open');
    $('#btn-sidebar').setAttribute('aria-expanded', 'false');   // v1.10.1: a11y
  };
  $('#btn-sidebar').onclick = () => {
    const opened = $('#sidebar').classList.toggle('open');
    document.body.classList.toggle('sb-open', opened);
    $('#btn-sidebar').setAttribute('aria-expanded', String(opened)); // v1.10.1: a11y
  };
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
    closeSidebar();                       // v1.10.1: перешли в другой раздел — меню закрыто
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
  // акцент
  const row = $('#accent-row');
  if (row && !row.dataset.bound) {
    row.dataset.bound = '1';
    row.querySelectorAll('.swatch').forEach(b => {
      b.onclick = () => {
        try { localStorage.setItem('ymaster-accent', b.dataset.accent); } catch (e) {}
        applyAccent(b.dataset.accent);
        renderAppearanceState();
      };
    });
  }
  // плотность
  const seg = $('#density-seg');
  if (seg && !seg.dataset.bound) {
    seg.dataset.bound = '1';
    seg.querySelectorAll('button').forEach(b => {
      b.onclick = () => {
        try { localStorage.setItem('ymaster-density', b.dataset.density); } catch (e) {}
        applyDensity(b.dataset.density);
        renderAppearanceState();
      };
    });
  }
  // спокойный час
  const q = $('#btn-quiet');
  if (q && !q.dataset.bound) {
    q.dataset.bound = '1';
    q.onclick = () => {
      if (quietActive()) {
        try { localStorage.removeItem(QUIET_KEY); } catch (e) {}
        toast('Спокойный час отключён — уведомления снова показываются', 'info', 'Оформление');
      } else {
        const mins = parseInt(($('#quiet-dur') || {}).value || '60', 10);
        try { localStorage.setItem(QUIET_KEY, String(Date.now() + mins * 60000)); } catch (e) {}
        toast(`Спокойный час включён до ${new Date(Date.now() + mins * 60000).toLocaleTimeString('ru-RU', { hour: '2-digit', minute: '2-digit' })}`, 'info', 'Оформление');
      }
      renderAppearanceState();
    };
  }
  renderAppearanceState();
}

function applyAccent(v) {
  if (v) document.documentElement.setAttribute('data-accent', v);
  else document.documentElement.removeAttribute('data-accent');
}

function applyDensity(v) {
  if (v) document.documentElement.setAttribute('data-density', v);
  else document.documentElement.removeAttribute('data-density');
}

function renderAppearanceState() {
  let acc = '', den = '';
  try { acc = localStorage.getItem('ymaster-accent') || ''; den = localStorage.getItem('ymaster-density') || ''; } catch (e) {}
  document.querySelectorAll('#accent-row .swatch').forEach(b =>
    b.classList.toggle('active', (b.dataset.accent || '') === acc));
  document.querySelectorAll('#density-seg button').forEach(b =>
    b.classList.toggle('active', (b.dataset.density || '') === den));
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
        <input id="er-category" value="${esc(r.category || '')}" list="er-cats" placeholder="Канцелярия, ГСМ, Хозтовары…">
        <datalist id="er-cats">${[...(viewReceipts._cats || [])].map(c => `<option value="${esc(c)}">`).join('')}</datalist></label>
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
      category: slot.querySelector('#er-category').value.trim(),
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
// ==========================================================================
//  v1.7.0: Темы — светлая / тёмная / авто («как на устройстве»)
// ==========================================================================
function applyTheme(t) {
  document.documentElement.setAttribute('data-theme', t);
  try { localStorage.setItem('ymaster-theme', t); } catch (e) {}
  const meta = document.querySelector('meta[name="theme-color"]');
  const dark = t === 'dark'
    || (t === 'auto' && window.matchMedia('(prefers-color-scheme: dark)').matches);
  if (meta) meta.setAttribute('content', dark ? '#1e1e1e' : '#f5f5f5');
}

window.matchMedia('(prefers-color-scheme: dark)').addEventListener?.('change', () => {
  let cur = 'auto';
  try { cur = localStorage.getItem('ymaster-theme') || 'light'; } catch (e) {}
  if (cur === 'auto') applyTheme('auto');   // перерисовать под новую системную тему
});

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
  api.get('/api/v1/admin/update/check', { retries: 1 }).then(r => {
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

function openUpdateProgress() {
  const { slot, close } = openModal(`
    <div class="modal-title">🔄 Обновление системы</div>
    <div class="progress-outer"><div class="progress-inner" id="upd-bar" style="width:5%"></div></div>
    <p class="form-hint" id="upd-step">Запуск…</p>
    <pre class="codeblock" id="upd-log" style="max-height:240px;font-size:11.5px"></pre>
    <div class="modal-actions"><button class="btn btn-primary hidden" id="upd-done">Готово</button></div>`, { onClose: () => clearInterval(state.updTimer) });
  clearInterval(state.updTimer);
  state.updTimer = setInterval(async () => {
    let resp;
    try { resp = await api.get('/api/v1/admin/update/status'); }
    catch { return; }
    let st = resp.job;
    // v1.8.1: после рестарта процесс новый, in-memory job пуст — успех
    // читаем из сохранённого маркера (свежий < 10 минут)
    const last = resp.last_success;
    if (!st.running && !st.finished && last && last.to
        && Date.now() - new Date(last.at).getTime() < 10 * 60 * 1000) {
      clearInterval(state.updTimer);
      const bar = slot.querySelector('#upd-bar');
      if (bar) bar.style.width = '100%';
      const stepEl = slot.querySelector('#upd-step');
      if (stepEl) stepEl.textContent = `Обновление до v${last.to} установлено — сервис перезапущен`;
      const btn = slot.querySelector('#upd-done');
      if (btn) { btn.classList.remove('hidden'); btn.onclick = () => { close(); location.reload(); }; }
      toast(`Готово: v${last.to} — страница будет перезагружена`, 'ok', '🔄');
      return;
    }
    const bar = slot.querySelector('#upd-bar');
    if (bar) bar.style.width = Math.max(5, st.progress) + '%';
    const stepEl = slot.querySelector('#upd-step');
    if (stepEl) stepEl.textContent = st.running ? (st.step || '…') : (st.success ? 'Обновление завершено' : (st.error ? 'Ошибка: ' + st.error : ''));
    const log = slot.querySelector('#upd-log');
    if (log) log.textContent = (st.log || []).join('\n');
    if (st.finished) {
      clearInterval(state.updTimer);
      const btn = slot.querySelector('#upd-done');
      if (btn) btn.classList.remove('hidden');
      btn.onclick = () => { close(); if (st.success) location.reload(); };
      if (st.success) toast(st.needs_restart
        ? 'Обновление установлено. Перезапустите сервис (systemctl restart ymaster-check) — в облаке Timeweb это делает панель'
        : 'Обновление установлено и применено!', 'ok', 'v' + st.to_version);
      if (st.rolled_back) toast('Произошёл сбой — система автоматически откатилась на прежнюю версию. Данные целы.', 'warn', 'Безопасность');
    }
  }, 1500);
}

const WHATS_NEW = {
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
        <label class="field"><span>Сотрудник</span>
          <input id="f-assignee" placeholder="Иванов"></label>
        <label class="field"><span>Кто добавил</span>
          <select id="f-creator"><option value="">все добавившие</option></select></label>
        <label class="field"><span>Данные чека</span>
          <select id="f-full"><option value="">все</option>
            <option value="true">📥 полные данные получены</option>
            <option value="false">⏳ ожидают данных</option></select></label>
        <label class="field"><span>Статья расходов</span>
          <input id="f-category" placeholder="Канцелярия" list="f-cats">
          <datalist id="f-cats">${(viewReceipts._cats || []).map(c => `<option value="${esc(c)}">`).join('')}</datalist></label>
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

  const filters = {
    q: '', status: '', fns_status: '', exported: '', assignee: '',
    creator: '', full_data: '',
    category: '', notified: '', date_from: '', date_to: '', page: 1,
  };
  let pageInfo = { total: 0, total_sum: 0, page_size: 50 };

  async function load() {
    const p = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); });
    if (companyIdParam()) p.set('company_id', companyIdParam());   // v1.11.0
    const data = await api.get('/api/v1/receipts?' + p.toString());
    pageInfo = data;
    // v1.8.0: данные не изменились → не перерисовываем (без мерцания)
    const rcptSig = JSON.stringify(data);
    if (viewReceipts._sig === rcptSig) { updateSelInfo(); return; }
    viewReceipts._sig = rcptSig;
    viewReceipts._rows = data.items;
    // datalist имён сотрудников — из текущих строк + ранее введённые
    const names = new Set(viewReceipts._names || []);
    data.items.forEach(x => { if (x.assignee) names.add(x.assignee); });
    viewReceipts._names = [...names];
    const cats = new Set(viewReceipts._cats || []);
    data.items.forEach(x => { if (x.category) cats.add(x.category); });
    viewReceipts._cats = [...cats];
    // v1.23.0: селектор «Кто добавил» — те, кто добавлял чеки этой компании
    if (acc && !viewReceipts._creatorsLoaded) {
      viewReceipts._creatorsLoaded = true;
      api.get('/api/v1/receipts/creators' + (companyIdParam() ? '?company_id=' + companyIdParam() : ''))
        .then(list => {
          const sel = $('#f-creator');
          if (!sel) return;
          sel.innerHTML = '<option value="">все добавившие</option>' +
            list.map(u => `<option value="${esc(u.id)}">${esc(u.name)} (${u.count})</option>`).join('');
          sel.value = viewReceipts._creatorKeep || '';
        }).catch(() => {});
    }
    const el = $('#receipts-table');
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
    $('#pg-prev').onclick = () => { filters.page--; load(); };
    $('#pg-next').onclick = () => { filters.page++; load(); };
    updateSelInfo();
  }

  function receiptRow(r) {
    const canDel = isAdmin() || (!r.exported && r.created_by_id === state.me.id);
    const isOwner = r.created_by_id === state.me.id;
    const notifiedMark = r.notified ? ' <span title="Сотрудник уведомляет бухгалтерию">🔔</span>' : '';
    const detailsMark = r.details_source ? `<span class="form-hint" title="Источник данных: ${esc(r.details_source)}">${r.details_source === 'fns_api' ? 'ФНС' : r.details_source === 'proverkacheka' ? 'ПК' : r.details_source === 'custom' ? 'свой' : '✎'}</span>` : '';
    // v1.23.0: полные данные получены → запрос не нужен, только изменение
    const actions = acc
      ? `<td style="white-space:nowrap">
           ${r.full_data
             ? '<span class="form-hint" title="Полные данные чека получены из сервиса проверки">📥✓</span>'
             : `<button class="btn btn-sm r-fetch" data-act="fetch" data-id="${r.id}" title="Получить полные данные чека из сервиса проверки">📥</button>`}
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
      filters.assignee = $('#f-assignee').value.trim();
      filters.creator = $('#f-creator') ? $('#f-creator').value : '';
      viewReceipts._creatorKeep = filters.creator;
      filters.full_data = $('#f-full') ? $('#f-full').value : '';
      filters.category = $('#f-category') ? $('#f-category').value.trim() : '';
      filters.notified = $('#f-notified') ? $('#f-notified').value : '';
    }
    filters.date_from = $('#f-from').value;
    filters.date_to = $('#f-to').value;
    filters.page = 1;
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

function rcptBlocks(r, qrUrl) {
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
    blocks.push(`<div style="text-align:center;margin-top:1.5mm"><img src="${qrUrl}" alt="Фискальный QR чека" style="width:26mm;height:26mm"></div>`);
  }
  return blocks;
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
  const { close } = openModal(`<div class="modal-title">🖨 Печать чеков</div>
    <p class="form-hint">Готовлю раскрой листов A4…</p><div class="spinner"></div>`);
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

    // 1) Фискальные QR-коды чеков (PNG с сервера) — как на кассовом чеке
    const qrUrls = {};
    for (const r of data.items.slice(0, 60)) {
      try {
        const { blob } = await api.download(`/api/v1/receipts/${r.id}/qr.png`);
        qrUrls[r.id] = URL.createObjectURL(blob);
      } catch { /* чек без QR — печатаем без него */ }
    }

    // 2) Рендерим чеки и режем длинные по строкам позиций
    const pieces = [];                                      // { html, h }
    for (const r of data.items) {
      const blocks = rcptBlocks(r, qrUrls[r.id]);
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
    close();
    toast(`Раскрой готов: ${pages.length} стр. × 3 колонки · ${data.items.length} чек. В диалоге печати выберите «Сохранить как PDF»`, 'ok', '🖨');
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
  const close = () => { drawer.classList.remove('open'); setTimeout(() => drawer.remove(), 300); };
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
    кнопкой «Сделать администратором» у бухгалтера.</div>
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
  let fns = null, onec = null, appSet = null, ext = null, checkoSet = null, tgSet = null, me = null;
  try { me = await api.get('/api/v1/auth/me'); } catch {}
  try { if (isAdmin()) { fns = await api.get('/api/v1/settings/fns'); onec = await api.get('/api/v1/settings/onec'); appSet = await api.get('/api/v1/settings/app'); ext = await api.get('/api/v1/settings/external'); checkoSet = await api.get('/api/v1/settings/checko'); tgSet = await api.get('/api/v1/settings/telegram'); } }
  catch { /* ignore */ }
  const about = await api.get('/api/v1/about');
  let appSum = null, sysInfo = null;
  try { appSum = await api.get('/api/v1/settings/app/summary'); } catch {}
  if (isAdmin()) { try { sysInfo = await api.get('/api/v1/admin/system'); } catch {} }

  container.innerHTML = `
    <div class="settings-grid">
      ${isAdmin() && appSet ? `
      <div class="glass card">
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
      <div class="glass card">
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
      <div class="glass card">
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
      <div class="glass card">
        <div class="card-title">🔔 Telegram-бот <span class="form-hint">(v1.16.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Напоминания сотрудникам «сдай чек за сегодня»
        и уведомления о принятых чеках. Токен — у
        <a href="https://t.me/BotFather" target="_blank" rel="noopener">@BotFather</a> (/newbot),
        вставьте один раз — хранится зашифрованным.</p>
        <label class="field" style="margin-bottom:10px"><span>Токен бота
          ${tgSet.has_token ? '(задан: ' + esc(tgSet.token_masked) + (tgSet.bot_username ? ', @' + esc(tgSet.bot_username) : '') + ')' : '(не задан)'}</span>
          <input id="tg-token" type="password" autocomplete="off" placeholder="123456789:AA…"></label>
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
          <button class="btn btn-sm" id="tg-test">🧪 Тест себе</button>
          <button class="btn btn-sm" id="tg-refresh"> ⟳ Обновить данные бота</button>
        </div>
        <p class="form-hint" style="margin-top:10px">${tgSet.worker_running ? '✅ Воркер запущен' : '⏸ Воркер не запущен'} ·
          статус воркера также виден после сохранения</p>
      </div>` : ''}

      ${me ? `
      <div class="glass card">
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
      <div class="glass card">
        <div class="card-title">📥 Источники данных чека <span class="form-hint">(v1.2.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Полные данные чека (магазин, ИНН, позиции) система получает
        из источников по порядку. Между запросами — случайная пауза 2–7 секунд,
        при блокировке источник временно «остывает» и включается следующий — банов не будет.</p>
        <label class="field" style="margin-bottom:10px"><span>Токен proverkacheka.com
          ${ext && ext.has_proverkacheka_token ? '(задан: ' + esc(ext.proverkacheka_token_masked) + ')' : '(не задан — получите в личном кабинете proverkacheka.com → Справка → API)'}</span>
          <input id="ext-pke" type="password" placeholder="токен API"></label>
        <label class="field" style="margin-bottom:10px"><span>ОФД-ру «QR Cash» (tokenSecret) — API ofd.ru по базе ФНС
          ${ext && ext.has_ofd_ru_token ? '(задан: ' + esc(ext.ofd_ru_token_masked) + ')' : '(не задан — личный кабинет ofd.ru → QR Cash)'}</span>
          <input id="ext-ofd" type="password" placeholder="tokenSecret"></label>
        <label class="field" style="margin-bottom:10px"><span>Свои источники (до 10) — по одному в строке: Название | URL</span>
          <textarea id="ext-custom-urls" rows="3" placeholder="проверкачека | https://…/api/check">${esc((ext ? ext.external_custom_urls : []) .map(u => (u.name || 'custom') + ' | ' + u.url).join('\n'))}</textarea>
          <small class="form-hint">Контракт: POST {qrraw} → JSON с items + totalSum. Подойдёт любой ваш шлюз к сервисам проверки.</small></label>
        <label class="field" style="margin-bottom:10px"><span>Порядок источников</span>
          <input id="ext-order" value="${esc(ext ? ext.external_order : 'fns_api,ofd_ru,proverkacheka,custom')}">
          <small class="form-hint">fns_api — API ФНС (токен в карточке «Проверка чеков»), ofd_ru — ОФД-ру, proverkacheka, custom — свои</small></label>
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
      <div class="glass card">
        <div class="card-title">🖥 Сервер и команды <span class="form-hint">(v1.17.0)</span></div>
        <dl class="kv" style="font-size:13px" id="sys-info"><dt>Загрузка…</dt><dd></dd></dl>
        <p class="form-hint" style="margin:10px 0 6px">Команды выполняются на сервере по SSH.
        Первая — одноразовая: разрешает приложению перезапускать себя без пароля
        (после неё обновления из приложения идут полностью автоматически).</p>
        <div id="sys-cmds"></div>
      </div>

      <div class="glass card">
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

      ${isAdmin() && onec ? `
      <div class="glass card">
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

      <div class="glass card">
        <div class="card-title">🎨 Оформление <span class="form-hint">(v1.7.0)</span></div>
        <div class="seg" id="theme-seg">
          <button data-t="auto" type="button">🌓 Как на устройстве</button>
          <button data-t="light" type="button">☀️ Светлая</button>
          <button data-t="dark" type="button">🌙 Тёмная</button>
        </div>
        <p class="form-hint" style="margin-top:8px">«Как на устройстве» — тема меняется вместе с
        настройкой системы/телефона автоматически.</p>
      </div>

      ${!isAdmin() && me ? `
      <div class="glass card">
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

      <div class="glass card">
        <div class="card-title">🧹 Обслуживание устройства <span class="form-hint">(v1.17.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Если после обновления сервера что-то
        отображается по-старому (иконки, цифры, интерфейс) — сбросьте локальный кэш приложения.
        Обычно сброс происходит автоматически.</p>
        <button class="btn btn-sm" id="btn-cache-reset">🧹 Сбросить кэш приложения</button>
      </div>

      <div class="glass card">
        <div class="card-title">🎨 Оформление <span class="form-hint">(v1.20.0 — стиль «Ямастер»)</span></div>
        <label class="appearance-label">Акцентный цвет</label>
        <div class="swatch-row" id="accent-row" role="radiogroup" aria-label="Акцентный цвет">
          <button class="swatch" data-accent="" style="--sw:#ff7a00" title="Оранжевый (по умолчанию)" aria-label="Оранжевый"></button>
          <button class="swatch" data-accent="indigo" style="--sw:#4b0082" title="Индиго" aria-label="Индиго"></button>
          <button class="swatch" data-accent="sea" style="--sw:#2e8b57" title="Морская зелень" aria-label="Морская зелень"></button>
          <button class="swatch" data-accent="sky" style="--sw:#5bc0de" title="Голубой" aria-label="Голубой"></button>
        </div>
        <label class="appearance-label">Плотность интерфейса</label>
        <div class="segmented" id="density-seg">
          <button data-density="">Просторный</button>
          <button data-density="compact">Компактный</button>
        </div>
        <label class="appearance-label">🔕 Спокойный час</label>
        <p class="form-hint" style="margin-bottom:8px">Некритичные уведомления скрыты; ошибки показываются всегда.</p>
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

      <div class="glass card">
        <div class="card-title">📲 Приложение на устройстве <span class="form-hint">(v1.5.0)</span></div>
        <p class="pwa-hint">Установите Ямастер Чек как приложение: иконка на домашнем экране,
        полноэкранный режим, быстрый доступ к сканеру. Работает на Android, iPhone/iPad,
        Windows и macOS — без магазина приложений.</p>
        <button class="btn btn-primary btn-sm" id="btn-pwa" style="margin-top:10px">📲 Установить / как установить</button>
      </div>

      ${isAdmin() ? `
      <div class="glass card">
        <div class="card-title">💾 Резервные копии <span class="form-hint">(v1.5.0)</span></div>
        <p class="form-hint" style="margin-bottom:10px">Копии создаются автоматически: ежедневные (7 шт.),
        перед каждым обновлением (5) и архив месяца (12 месяцев) — каталог data/backups на сервере.
        Данные не затрагиваются ни обновлениями, ни git.</p>
        <div style="display:flex;gap:8px;flex-wrap:wrap;margin-bottom:10px">
          <button class="btn btn-sm btn-primary" id="btn-bk-create">＋ Создать копию сейчас</button>
          <button class="btn btn-sm" id="btn-bk-refresh">↻ Обновить список</button>
        </div>
        <div id="bk-list"><div class="skeleton" style="height:60px"></div></div>
      </div>` : ''}

      <div class="glass card">
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

      <div class="glass card">
        <div class="card-title">Безопасность и доступ</div>
        <dl class="kv">
          <dt>Регистрация</dt><dd>только по приглашениям администратора</dd>
          <dt>Администратор</dt><dd>всегда один, передача прав — в разделе «Пользователи»</dd>
          <dt>Защита входа</dt><dd>блокировка после 5 неудачных попыток</dd>
          <dt>Лимиты запросов</dt><dd>включены (защита от перебора и DoS)</dd>
        </dl>
      </div>

      <div class="glass card">
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

  // v1.7.0: переключатель тем
  const seg = $('#theme-seg');
  if (seg) {
    let cur = 'auto';
    try { cur = localStorage.getItem('ymaster-theme') || 'light'; } catch (e) {}
    seg.querySelectorAll('button').forEach(b => {
      b.classList.toggle('on', b.dataset.t === cur);
      b.onclick = () => {
        applyTheme(b.dataset.t);
        seg.querySelectorAll('button').forEach(x => x.classList.toggle('on', x === b));
        toast(b.dataset.t === 'auto' ? 'Тема следует за устройством' : b.dataset.t === 'light' ? 'Светлая тема' : 'Тёмная тема', 'ok', '🎨');
      };
    });
  }

  // PWA-кнопка в настройках (все роли)
  const bp = $('#btn-pwa');
  if (bp) bp.onclick = showInstallDialog;

  // Резервные копии (админ)
  if (isAdmin()) {
    const renderBackups = (items) => {
      const el = $('#bk-list');
      if (!el) return;
      const kindNames = { daily: 'ежедневная', preupdate: 'перед обновлением',
                          manual: 'вручную', archive: 'архив месяца' };
      el.innerHTML = items.length
        ? `<table class="data" style="min-width:0"><thead><tr><th>Копия</th><th>Тип</th><th>Размер</th><th></th></tr></thead><tbody>
           ${items.slice(0, 12).map(b => `<tr style="cursor:default">
             <td class="cell-mono" style="font-size:11.5px">${esc(b.name)}</td>
             <td>${kindNames[b.kind] || b.kind}</td>
             <td>${b.size_kb} КБ</td>
             <td><a href="/api/v1/admin/backups/${encodeURIComponent(b.name)}/download" class="btn btn-sm" download>⬇</a></td>
           </tr>`).join('')}</tbody></table>`
        : '<p class="form-hint">Копий пока нет — создайте первую кнопкой выше</p>';
    };
    const loadBackups = async () => {
      try { renderBackups((await api.get('/api/v1/admin/backups')).items); }
      catch { const el = $('#bk-list'); if (el) el.innerHTML = '<p class="form-hint">Список недоступен</p>'; }
    };
    loadBackups();
    const bcr = $('#btn-bk-create');
    if (bcr) bcr.onclick = async () => {
      bcr.disabled = true;
      try { const r = await api.post('/api/v1/admin/backups'); toast(r.message, 'ok', '💾'); renderBackups(r.items); }
      catch (e) { toast(e.message, 'err'); }
      bcr.disabled = false;
    };
    const brf = $('#btn-bk-refresh');
    if (brf) brf.onclick = loadBackups;
  }

  if (isAdmin()) {
    const loadUpdCard = async () => {
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
        const r = await api.get('/api/v1/admin/update/check', { retries: 1 });
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
    const bc = $('#btn-check-update');
    if (bc) bc.onclick = async () => {
      bc.disabled = true; bc.textContent = 'Проверяю…';
      await loadUpdCard();
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
  bindAppearance();                 // v1.20.0: акцент, плотность, тихий час
  const bcr = $('#btn-cache-reset');
  if (bcr) bcr.onclick = () => hardReset();

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
        const r = await api.put('/api/v1/settings/telegram', body);
        toast(r.message, r.worker_running ? 'ok' : 'warn', '🔔');
        route(true);
      } catch (e) { toast(e.message, 'err'); }
      tts.disabled = false;
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
        const urls = $('#ext-custom-urls').value.split('\n')
          .map(l => l.trim()).filter(Boolean)
          .map(line => {
            const m = line.split('|');
            return m.length >= 2
              ? { name: m[0].trim(), url: m.slice(1).join('|').trim() }
              : { name: 'custom', url: line };
          })
          .filter(u => u.url);
        await api.put('/api/v1/settings/external', {
          proverkacheka_token: $('#ext-pke').value.trim() || undefined,
          ofd_ru_token: $('#ext-ofd').value.trim() || undefined,
          external_custom_urls: urls,
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
