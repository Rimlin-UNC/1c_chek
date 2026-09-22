// ======================================================================
// Ямастер Чек — веб-клиент (SPA)
// Разработчик и владелец идеи: ООО «Ямастер»
// Сайт: https://ymaster.ru | E-mail: info@ymaster.ru
//
// Экраны: вход · дашборд · сканирование · чеки · выгрузка в 1С ·
//         маппинг · пользователи · журнал · настройки
// ======================================================================

import { api, getToken, setToken, clearToken, ApiError } from './api.js';
import { toast, esc, fmtSum, fmtInt, fmtDate, statusLabel, chip, openModal, animateNumber, debounce } from './ui.js';
import { injectIcons } from './icons.js';
import { barChart, donutChart } from './charts.js';
import { CameraScanner, decodeImageFile, offlineQueue, parseQrClient } from './scanner.js';

// --------------------------------------------------------------------------
//  Состояние
// --------------------------------------------------------------------------
const state = {
  me: null,
  ws: null,
  wsOk: false,
  camera: null,
  view: 'dashboard',
  receiptsSelected: new Set(),
  recents: JSON.parse(localStorage.getItem('ymaster_recents') || '[]'),
};

const VIEW_TITLES = {
  dashboard: 'Дашборд', scan: 'Сканирование чеков', receipts: 'База чеков',
  export: 'Выгрузка в 1С', mapping: 'Маппинг реквизитов', users: 'Пользователи',
  audit: 'Журнал действий', settings: 'Настройки',
};

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];

// --------------------------------------------------------------------------
//  Точка входа
// --------------------------------------------------------------------------
async function boot() {
  injectIcons();
  registerServiceWorker();
  bindShell();

  if (getToken()) {
    try {
      state.me = await api.get('/api/v1/auth/me');
      enterApp();
      return;
    } catch { clearToken(); }
  }
  showLogin();
}

function showLogin() {
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

function enterApp() {
  $('#login-screen').classList.add('hidden');
  $('#app-shell').classList.remove('hidden');
  $('#user-name').textContent = state.me.full_name || state.me.username;
  $('#user-role').textContent = state.me.role === 'admin' ? 'Администратор' : 'Оператор';
  $('#user-avatar').textContent = (state.me.full_name || state.me.username)[0].toUpperCase();
  $$('.admin-only').forEach(el => el.classList.toggle('hidden', state.me.role !== 'admin'));
  connectWS();
  window.addEventListener('ymaster:logout', () => { location.hash = ''; logout(); });
  if (!location.hash) location.hash = '#/dashboard';
  route();
  refreshBadges();
  window.addEventListener('online', flushOfflineQueue);
  flushOfflineQueue();
}

function logout() {
  clearToken();
  state.me = null;
  if (state.ws) { state.ws.close(); state.ws = null; }
  if (state.camera) { state.camera.stop(); state.camera = null; }
  location.reload();
}

// --------------------------------------------------------------------------
//  WebSocket: живые статусы
// --------------------------------------------------------------------------
function connectWS() {
  const proto = location.protocol === 'https:' ? 'wss' : 'ws';
  const ws = new WebSocket(`${proto}://${location.host}/ws/status`);
  state.ws = ws;
  ws.onopen = () => setWsStatus(true);
  ws.onclose = () => { setWsStatus(false); setTimeout(connectWS, 4000); };
  ws.onerror = () => setWsStatus(false);
  ws.onmessage = (ev) => {
    try {
      const msg = JSON.parse(ev.data);
      handleWsEvent(msg.type, msg.payload);
    } catch { /* ignore */ }
  };
}

function setWsStatus(ok) {
  state.wsOk = ok;
  const dot = $('#ws-status .live-dot');
  const txt = $('#ws-status-text');
  dot.classList.toggle('on', ok);
  dot.classList.toggle('off', !ok);
  txt.textContent = ok ? 'Живое подключение' : 'Переподключение…';
  $('#offline-indicator').classList.toggle('hidden', navigator.onLine && ok !== false);
}

function handleWsEvent(type, p) {
  if (p && p.demo) { refreshBadges(); if (state.view === 'dashboard' || state.view === 'receipts') route(true); return; }
  switch (type) {
    case 'receipt_created':
      toast(`Чек ФН ${p.fn || ''} ФД ${p.fd || ''} на ${fmtSum(p.total_sum)} принят`, 'ok', 'Новый чек');
      refreshBadges();
      if (state.view === 'dashboard' || state.view === 'receipts') route(true);
      break;
    case 'receipt_duplicate':
      toast(`Чек ФН ${p.fn} ФД ${p.fd} уже есть в базе — дубликат отсеян`, 'warn', 'Дубликат');
      break;
    case 'receipt_verifying':
      if (state.view === 'receipts' || state.view === 'dashboard') route(true);
      break;
    case 'receipt_verified': {
      const ok = p.fns_status === 'valid';
      toast(`ФНС: чек ФД ${p.fd} — ${statusLabel(p.fns_status)}`, ok ? 'ok' : (p.fns_status === 'invalid' ? 'err' : 'warn'), 'Проверка ФНС');
      refreshBadges();
      if (state.view === 'dashboard' || state.view === 'receipts') route(true);
      break;
    }
    case 'receipt_deleted':
      refreshBadges();
      if (state.view === 'receipts') route(true);
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
function route(silent = false) {
  if (state.camera) { state.camera.stop(); state.camera = null; }
  const hash = location.hash.replace(/^#\//, '') || 'dashboard';
  const view = hash.split('?')[0];
  state.view = view;
  const container = $('#view-container');
  $('#page-title').textContent = VIEW_TITLES[view] || 'Ямастер Чек';
  $$('.nav-item').forEach(a => a.classList.toggle('active', a.dataset.view === view));
  $('#sidebar').classList.remove('open');
  if (!silent) container.classList.remove('view-enter');
  const renderers = {
    dashboard: viewDashboard, scan: viewScan, receipts: viewReceipts,
    export: viewExport, mapping: viewMapping, users: viewUsers,
    audit: viewAudit, settings: viewSettings,
  };
  (renderers[view] || viewDashboard)(container);
  if (!silent) { void container.offsetWidth; container.classList.add('view-enter'); }
}

function bindShell() {
  $('#btn-logout').onclick = logout;
  $('#btn-sidebar').onclick = () => $('#sidebar').classList.toggle('open');
  window.addEventListener('hashchange', () => route());
}

// ==========================================================================
//  ЭКРАН: Дашборд
// ==========================================================================
async function viewDashboard(container) {
  container.innerHTML = `
    <div class="grid kpi-grid">
      ${kpiCard('kpi-total', 'Чеков в системе', 'всё время')}
      ${kpiCard('kpi-sum', 'Общая сумма', 'все чеки')}
      ${kpiCard('kpi-fns', 'Проверено ФНС', 'действительных')}
      ${kpiCard('kpi-export', 'Выгружено в 1С', 'ждут выгрузки')}
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
    <div id="demo-zone"></div>`;

  const stats = await api.get('/api/v1/dashboard/stats?days=14');
  animateNumber($('#kpi-total .kpi-value'), stats.total);
  animateNumber($('#kpi-sum .kpi-value'), stats.total_sum, fmtSum);
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

  const feed = await api.get('/api/v1/dashboard/recent?limit=12');
  const feedEl = $('#dash-feed');
  if (!feed.length) feedEl.innerHTML = emptyState('🔔', 'События появятся здесь');
  else feedEl.innerHTML = feed.map(f => `
    <div class="feed-item"><span class="feed-time">${fmtDate(f.created_at)}</span>
    <span class="feed-text">${esc(feedActionText(f))}</span></div>`).join('');

  if (stats.total === 0 && state.me.role === 'admin') {
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

function kpiCard(id, label, sub) {
  return `<div class="glass kpi" id="${id}">
    <div class="kpi-label">${label}</div>
    <div class="kpi-value">0</div>
    <div class="kpi-sub">${sub}</div></div>`;
}
function emptyState(ico, text) {
  return `<div class="empty-state"><span class="big-ico">${ico}</span>${text}</div>`;
}
function feedActionText(f) {
  const map = {
    receipt_created: 'отсканирован новый чек', receipt_duplicate: 'отсеян дубликат чека',
    login: 'вход в систему', login_failed: 'неудачная попытка входа',
    receipts_exported: 'выгрузка чеков в 1С', verify_queued: 'запущена проверка в ФНС',
    receipt_deleted: 'удалён чек', demo_data_loaded: 'загружены демо-данные',
    user_created: 'создан пользователь', mapping_updated: 'обновлён маппинг',
    fns_settings_updated: 'изменены настройки ФНС', onec_pull: '1С забрала чеки',
    onec_ack: '1С подтвердила загрузку',
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
        <div class="torch-note">Совет: чеки с экрана телефона тоже распознаются. Держите QR в рамке при хорошем свете.</div>
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
            <div class="step"><span class="step-num"></span><div>Чек автоматически проверяется в API ФНС (или демо-провайдером).</div></div>
            <div class="step"><span class="step-num"></span><div>Проверенные чеки выгружаются в 1С по настройкам маппинга.</div></div>
          </div>
        </div>
      </div>
    </div>`;

  renderRecents();

  // Камера
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

  // Загрузка файлов
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

  // Вставка текста
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
    duplicate: r.status === 'new' ? false : false,
    at: new Date().toISOString(),
  });
  state.recents = state.recents.slice(0, 12);
  localStorage.setItem('ymaster_recents', JSON.stringify(state.recents));
  renderRecents();
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
      const r = await api.post('/api/v1/receipts/scan', { qr_data: text, source });
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
      await api.post('/api/v1/receipts/scan', { qr_data: item.qr_data, source: item.source || 'web' });
      offlineQueue.shift();
      sent++;
    } catch (e) {
      if (e.status === 422) { offlineQueue.shift(); continue; } // битые данные — выбрасываем
      break; // сервер всё ещё недоступен
    }
  }
  if (sent) toast(`Синхронизировано сканов: ${sent}`, 'ok', 'Офлайн-очередь');
  refreshBadges();
  if (state.view === 'scan') route(true);
}

// Обработка загруженных изображений
async function handleFiles(files) {
  for (const file of files) {
    if (!file.type.startsWith('image/')) { toast(`${file.name}: не изображение`, 'warn'); continue; }
    try {
      const { valid, raws } = await decodeImageFile(file);
      if (valid.length === 0 && raws.length === 0) {
        // Клиент не нашёл — пробуем серверный OpenCV (устойчивее)
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
      // Несколько чеков на изображении — предложим выбрать
      const variants = e.data?.detail?.variants || [];
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

// Диалог: вставить QR-текст
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

// Диалог: ручной ввод реквизитов
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
async function viewReceipts(container) {
  container.innerHTML = `
    <div class="glass card">
      <div class="filter-bar">
        <label class="field"><span>Поиск (ФН/ФД/ФП)</span>
          <input id="f-q" placeholder="номер…"></label>
        <label class="field"><span>Статус</span>
          <select id="f-status"><option value="">все</option>
            <option value="new">Новые</option><option value="verifying">Проверяются</option>
            <option value="verified">Проверены</option><option value="failed">Отклонены</option></select></label>
        <label class="field"><span>Проверка ФНС</span>
          <select id="f-fns"><option value="">любая</option>
            <option value="valid">Действителен</option><option value="invalid">Недействителен</option>
            <option value="not_found">Не найден</option><option value="unknown">Не проверен</option></select></label>
        <label class="field"><span>Выгрузка в 1С</span>
          <select id="f-exp"><option value="">все</option>
            <option value="false">Ожидают выгрузки</option><option value="true">Выгружены</option></select></label>
        <label class="field"><span>С даты</span><input type="date" id="f-from"></label>
        <label class="field"><span>По дату</span><input type="date" id="f-to"></label>
        <button class="btn" id="btn-filter">Найти</button>
      </div>
      <div style="display:flex;flex-wrap:wrap;gap:10px;margin-bottom:14px">
        <button class="btn btn-sm btn-ok" id="btn-bulk-verify">✓ Проверить в ФНС (выбранные)</button>
        <button class="btn btn-sm" id="btn-bulk-verify-all">✓✓ Проверить все новые</button>
        <button class="btn btn-sm btn-primary" id="btn-bulk-export">⬇ Выгрузить в 1С (выбранные)</button>
        <button class="btn btn-sm btn-bad" id="btn-bulk-delete">🗑 Удалить выбранные</button>
        <span class="form-hint" style="align-self:center" id="sel-info">не выбрано</span>
      </div>
      <div class="table-wrap" id="receipts-table"><div class="skeleton" style="height:300px"></div></div>
      <div class="pagination" id="receipts-pager"></div>
    </div>`;

  const filters = {
    q: '', status: '', fns_status: '', exported: '', date_from: '', date_to: '', page: 1,
  };
  let pageInfo = { total: 0, total_sum: 0, page_size: 50 };

  async function load() {
    const p = new URLSearchParams();
    Object.entries(filters).forEach(([k, v]) => { if (v !== '' && v != null) p.set(k, v); });
    const data = await api.get('/api/v1/receipts?' + p.toString());
    pageInfo = data;
    const el = $('#receipts-table');
    if (!data.items.length) {
      el.innerHTML = emptyState('🧾', 'Чеки не найдены. Отсканируйте первый на вкладке «Сканирование»');
    } else {
      el.innerHTML = `<table class="data"><thead><tr>
        <th style="width:34px"><input type="checkbox" id="sel-all" style="width:auto"></th>
        <th>Дата чека</th><th>Сумма</th><th>ФН</th><th>ФД</th><th>ФП</th>
        <th>Статус</th><th>ФНС</th><th>1С</th><th>Источник</th></tr></thead>
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
    return `<tr data-id="${r.id}">
      <td><input type="checkbox" class="row-sel" data-id="${r.id}" style="width:auto"
        ${state.receiptsSelected.has(r.id) ? 'checked' : ''}></td>
      <td class="cell-date">${fmtDate(r.receipt_date)}</td>
      <td class="cell-sum">${fmtSum(r.total_sum)}</td>
      <td class="cell-mono">${r.fn}</td><td class="cell-mono">${r.fd}</td><td class="cell-mono">${r.fp}</td>
      <td>${chip(r.status)}</td>
      <td>${chip(r.fns_status)}</td>
      <td>${r.exported ? '<span class="chip exported"><span class="dot"></span>да</span>' : '<span class="chip unknown"><span class="dot"></span>нет</span>'}</td>
      <td class="cell-mono">${esc(r.source)}</td></tr>`;
  }

  function updateSelInfo() {
    $('#sel-info').textContent = state.receiptsSelected.size
      ? `выбрано: ${state.receiptsSelected.size}` : 'не выбрано';
  }

  $('#btn-filter').onclick = () => {
    filters.q = $('#f-q').value.trim();
    filters.status = $('#f-status').value;
    filters.fns_status = $('#f-fns').value;
    filters.exported = $('#f-exp').value;
    filters.date_from = $('#f-from').value;
    filters.date_to = $('#f-to').value;
    filters.page = 1;
    load();
  };
  $('#f-q').addEventListener('keydown', e => { if (e.key === 'Enter') $('#btn-filter').click(); });

  $('#btn-bulk-verify').onclick = async () => {
    if (!state.receiptsSelected.size) return toast('Выберите чеки галочками', 'warn');
    const r = await api.post('/api/v1/receipts/verify', { receipt_ids: [...state.receiptsSelected] });
    toast(r.message, 'info', 'Проверка ФНС');
  };
  $('#btn-bulk-verify-all').onclick = async () => {
    const r = await api.post('/api/v1/receipts/verify', { receipt_ids: [] });
    toast(r.message, 'info', 'Проверка ФНС');
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

  await load();
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
        <dt>Дата чека</dt><dd>${fmtDate(r.receipt_date)}</dd>
        <dt>ФН</dt><dd class="cell-mono">${r.fn}</dd>
        <dt>ФД</dt><dd class="cell-mono">${r.fd}</dd>
        <dt>ФП</dt><dd class="cell-mono">${r.fp}</dd>
        <dt>Признак расчёта</dt><dd>${r.operation === 2 ? 'Возврат прихода' : 'Приход'}</dd>
        <dt>Источник</dt><dd>${esc(r.source)}</dd>
        <dt>Сканировал</dt><dd>${esc(r.created_by || '—')}</dd>
        <dt>Принят в систему</dt><dd>${fmtDate(r.created_at)}</dd>
        <dt>Выгружен в 1С</dt><dd>${r.exported ? 'да · ' + fmtDate(r.exported_at) : 'нет'}</dd>
        <dt>Ответ ФНС</dt><dd>${esc(r.fns_message || '—')}</dd>
      </dl>
      <div class="card-title">Позиции чека (${r.items.length})</div>
      ${r.items.length ? `<table class="data" style="min-width:0"><thead><tr><th>Наименование</th><th>Кол.</th><th>Цена</th><th>Сумма</th></tr></thead>
        <tbody>${r.items.map(it => `<tr style="cursor:default">
          <td>${esc(it.name)}</td><td>${it.quantity}</td>
          <td class="cell-sum">${fmtSum(it.price)}</td><td class="cell-sum">${fmtSum(it.total)}</td></tr>`).join('')}</tbody></table>`
        : emptyState('📦', 'Позиции недоступны (из QR их получить нельзя — приходят от ФНС/ОФД)')}
      <div class="modal-actions" style="justify-content:flex-start">
        <button class="btn btn-ok btn-sm" id="d-verify">✓ Проверить в ФНС</button>
        ${state.me.role === 'admin' || !r.exported ? '<button class="btn btn-bad btn-sm" id="d-delete">🗑 Удалить</button>' : ''}
      </div>
    </div>`;
  document.body.appendChild(drawer);
  requestAnimationFrame(() => drawer.classList.add('open'));
  const close = () => { drawer.classList.remove('open'); setTimeout(() => drawer.remove(), 300); };
  drawer.querySelector('.modal-close').onclick = close;
  drawer.querySelector('.qr-box').onclick = () => {
    navigator.clipboard?.writeText(r.qr_data);
    toast('Строка QR скопирована', 'info');
  };
  drawer.querySelector('#d-verify').onclick = async () => {
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
        <div class="info-callout">Выгружаются <b>проверенные чеки</b>. Формат — EnterpriseData (JSON/XML),
        совместим с 1С:БП 3.0, ERP 2, УТ 11. Дубликаты в 1С отсекаются по ключу <b>ФН+ФД+ФП</b>.</div>
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
            ${onecSettings ? `<button class="btn btn-sm" id="btn-reveal" style="margin-left:6px">показать токен</button>` : '(доступно администратору)'}</div></div>
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
        navigator.clipboard?.writeText(s.api_token);
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
  const isAdmin = state.me.role === 'admin';
  const sf = catalog.source_fields, to = catalog.target_objects, tr = catalog.transforms;

  container.innerHTML = `
    <div class="info-callout">Маппинг определяет, <b>какие поля чека и в какие реквизиты 1С</b> попадут
    при выгрузке. Правила применяются и в файловом экспорте, и в Push-режиме.
    ${isAdmin ? '' : 'Просмотр — изменения вносит администратор.'}</div>
    <div class="glass card">
      <div class="card-title">Правила преобразования <span class="spacer"></span>
        ${isAdmin ? '<button class="btn btn-sm" id="m-add">+ Добавить правило</button>' : ''}</div>
      <div class="mapping-grid" id="mapping-rows">
        <div class="mapping-head">Поле чека</div><div class="mapping-head">Документ 1С</div>
        <div class="mapping-head">Реквизит 1С</div><div class="mapping-head">Преобразование</div><div></div><div></div>
      </div>
      <div class="modal-actions" style="justify-content:flex-start">
        ${isAdmin ? '<button class="btn btn-primary" id="m-save">💾 Сохранить маппинг</button>' : ''}
      </div>
    </div>
    <div class="grid cards-2-even" style="margin-top:16px">
      <div class="glass card"><div class="card-title">Поля чека (источники)</div>
        <ul class="catalog-list" style="list-style:none">${sf.map(f =>
          `<li><span>${f.name}</span><code>${f.code}</code></li>`).join('')}</ul></div>
      <div class="glass card"><div class="card-title">Документы и преобразования</div>
        <ul class="catalog-list" style="list-style:none">${to.map(o =>
          `<li><span>${o.name}</span><code>${o.code}</code></li>`).join('')}
          <li style="border:none"></li>
          ${tr.map(t => `<li><span>${t.name}</span><code>${t.code}</code></li>`).join('')}</ul></div>
    </div>`;

  const rowsEl = $('#mapping-rows');
  const rows = [...mapping.items];

  function addRow(m = { source_field: 'total_sum', target_object: 'ПоступлениеТоваровУслуг',
                        target_field: '', transform: 'direct', transform_param: '', is_active: true }) {
    const div = document.createElement('div');
    div.className = 'mapping-grid mapping-row';
    div.style.marginBottom = '8px';
    div.innerHTML = `
      <label><span class="mg-label">Поле чека</span>
        <select class="mr-src">${sf.map(f => `<option value="${f.code}" ${f.code === m.source_field ? 'selected' : ''}>${f.name}</option>`).join('')}</select></label>
      <label><span class="mg-label">Документ 1С</span>
        <select class="mr-obj">${to.map(o => `<option value="${o.code}" ${o.code === m.target_object ? 'selected' : ''}>${o.name}</option>`).join('')}</select></label>
      <label><span class="mg-label">Реквизит 1С</span>
        <input class="mr-field" placeholder="СуммаДокумента" value="${esc(m.target_field)}"></label>
      <label><span class="mg-label">Преобразование</span>
        <select class="mr-tr">${tr.map(t => `<option value="${t.code}" ${t.code === m.transform ? 'selected' : ''}>${t.name}</option>`).join('')}</select></label>
      <label title="Активно"><input type="checkbox" class="mr-active" ${m.is_active ? 'checked' : ''} style="width:auto"></label>
      <button class="btn-icon" title="Удалить правило" style="color:var(--bad)">🗑</button>`;
    if (!isAdmin) div.querySelectorAll('input,select').forEach(el => el.disabled = true);
    div.querySelector('.btn-icon').onclick = () => div.remove();
    rowsEl.appendChild(div);
  }
  rows.forEach(addRow);
  if (!rows.length) addRow();
  if (isAdmin) {
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
//  ЭКРАН: Пользователи
// ==========================================================================
async function viewUsers(container) {
  const users = await api.get('/api/v1/users');
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">Пользователи системы <span class="spacer"></span>
        <button class="btn btn-sm btn-primary" id="u-add">+ Создать пользователя</button></div>
      <div class="table-wrap"><table class="data"><thead><tr>
        <th>Логин</th><th>ФИО</th><th>Организация</th><th>Роль</th><th>Статус</th>
        <th>Последний вход</th><th></th></tr></thead><tbody>
        ${users.map(u => `<tr data-id="${u.id}" style="cursor:default">
          <td><b>${esc(u.username)}</b></td><td>${esc(u.full_name)}</td>
          <td>${esc(u.organization || '—')}</td>
          <td>${u.role === 'admin' ? '<span class="chip exported"><span class="dot"></span>админ</span>' : '<span class="chip unknown"><span class="dot"></span>оператор</span>'}</td>
          <td>${u.is_active ? '<span class="chip verified"><span class="dot"></span>активен</span>' : '<span class="chip failed"><span class="dot"></span>отключён</span>'}</td>
          <td class="cell-date">${fmtDate(u.last_login_at)}</td>
          <td><button class="btn btn-sm u-edit">✎</button></td></tr>`).join('')}
      </tbody></table></div>
    </div>`;

  $('#u-add').onclick = () => userDialog();
  $$('.u-edit').forEach(btn => btn.onclick = () => {
    const u = users.find(x => x.id === btn.closest('tr').dataset.id);
    userDialog(u);
  });

  function userDialog(u = null) {
    const { slot } = openModal(`
      <div class="modal-title">${u ? '✎ Пользователь: ' + esc(u.username) : '+ Новый пользователь'}</div>
      <div class="form-grid">
        <label class="field"><span>Логин</span><input id="u-username" value="${esc(u?.username || '')}" ${u ? 'disabled' : ''}></label>
        <label class="field"><span>Роль</span>
          <select id="u-role"><option value="user" ${u?.role === 'user' ? 'selected' : ''}>Оператор</option>
          <option value="admin" ${u?.role === 'admin' ? 'selected' : ''}>Администратор</option></select></label>
        <label class="field"><span>ФИО</span><input id="u-fullname" value="${esc(u?.full_name || '')}"></label>
        <label class="field"><span>Организация</span><input id="u-org" value="${esc(u?.organization || '')}"></label>
        <label class="field full"><span>${u ? 'Новый пароль (пусто — не менять)' : 'Пароль'}</span>
          <input id="u-pass" type="password" placeholder="${u ? '••••••' : 'минимум 6 символов'}"></label>
        ${u ? `<label class="field"><span>Активен</span>
          <select id="u-active"><option value="1" ${u.is_active ? 'selected' : ''}>Да</option>
          <option value="0" ${!u.is_active ? 'selected' : ''}>Нет</option></select></label>` : ''}
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
            role: slot.querySelector('#u-role').value,
          };
          const pass = slot.querySelector('#u-pass').value;
          if (pass) patch.password = pass;
          const active = slot.querySelector('#u-active');
          if (active) patch.is_active = active.value === '1';
          await api.patch('/api/v1/users/' + u.id, patch);
        } else {
          await api.post('/api/v1/users', {
            username: slot.querySelector('#u-username').value.trim(),
            password: slot.querySelector('#u-pass').value,
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

// ==========================================================================
//  ЭКРАН: Журнал
// ==========================================================================
async function viewAudit(container) {
  const rows = await api.get('/api/v1/dashboard/recent?limit=60');
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">Журнал действий (последние 60 событий)</div>
      ${rows.length ? rows.map(f => `
        <div class="feed-item"><span class="feed-time">${fmtDate(f.created_at)}</span>
        <span class="chip unknown mono">${esc(f.action)}</span>
        <span class="feed-text">${esc(feedActionText(f))}</span></div>`).join('')
      : emptyState('📜', 'Журнал пуст')}
    </div>`;
}

// ==========================================================================
//  ЭКРАН: Настройки
// ==========================================================================
async function viewSettings(container) {
  const isAdmin = state.me.role === 'admin';
  let fns = null, onec = null;
  try { if (isAdmin) { fns = await api.get('/api/v1/settings/fns'); onec = await api.get('/api/v1/settings/onec'); } }
  catch { /* ignore */ }
  const about = await api.get('/api/v1/about');

  container.innerHTML = `
    <div class="settings-grid">
      ${isAdmin && fns ? `
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

      ${isAdmin && onec ? `
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
        <div class="card-title">Мой профиль</div>
        <dl class="kv">
          <dt>Логин</dt><dd>${esc(state.me.username)}</dd>
          <dt>Роль</dt><dd>${state.me.role === 'admin' ? 'Администратор' : 'Оператор'}</dd>
          <dt>Организация</dt><dd>${esc(state.me.organization || '—')}</dd>
        </dl>
        <div class="form-grid">
          <label class="field"><span>Старый пароль</span><input id="p-old" type="password"></label>
          <label class="field"><span>Новый пароль</span><input id="p-new" type="password"></label>
        </div>
        <button class="btn btn-sm btn-primary" id="p-save" style="margin-top:12px">Сменить пароль</button>
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

  if (isAdmin && fns) {
    $('#seg-mock').onclick = () => route(true);
    $('#seg-fns').onclick = () => route(true);
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
    navigator.serviceWorker.register('/sw.js').catch(() => { /* не критично */ });
  }
}

// --------------------------------------------------------------------------
boot();
