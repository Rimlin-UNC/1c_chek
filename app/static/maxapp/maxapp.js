// Ямастер Чек — мини-приложение MAX (v1.69.0)
// ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// Bridge: st.max.ru/js/max-web-app.js → window.WebApp.
// Авторизация: initData → POST /api/v1/maxapp/session → JWT (память).
'use strict';
const API = '';                          // тот же origin, что и страница
let TOKEN = '';
let ME = { points: 0, receipts: [], ref_code: '' };

function wa() { return window.WebApp || null; }
function $(id) { return document.getElementById(id); }
function esc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g,
    c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}
function api(method, path, body) {
  return fetch(API + path, {
    method,
    headers: Object.assign({'Content-Type': 'application/json'},
      TOKEN ? {Authorization: 'Bearer ' + TOKEN} : {}),
    body: body ? JSON.stringify(body) : undefined,
  }).then(async r => {
    const d = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(d.detail || ('HTTP ' + r.status));
    return d;
  });
}
function say(text, ok) {
  const m = $('msg');
  m.textContent = text;
  m.style.color = ok === false ? '#c0392b' : 'var(--accent)';
  if (wa() && wa().HapticFeedback && wa().HapticFeedback.impactOccurred) {
    try { wa().HapticFeedback.impactOccurred(ok === false ? 'heavy' : 'light'); } catch (e) {}
  }
}
function refresh(res) {
  if (res && typeof res.user_points === 'number') ME.points = res.user_points;
  $('points').textContent = ME.points;
}

// ---- запуск ---------------------------------------------------------
async function boot() {
  const app = wa();
  if (!app || !app.initData) {
    $('view-loading').innerHTML = '<p>Откройте приложение из чата бота ' +
      '<b>chek.ymaster.bot</b> в MAX — так работает вход без пароля.</p>';
    return;
  }
  try {
    if (app.BackButton && app.BackButton.hide) app.BackButton.hide();
  } catch (e) {}
  try {
    const d = await api('POST', '/api/v1/maxapp/session',
                        {init_data: app.initData});
    TOKEN = d.access_token;
    ME = Object.assign(ME, d.me);
    $('view-loading').classList.add('hidden');
    $('view-app').classList.remove('hidden');
    await loadSummary();
  } catch (e) {
    $('view-loading').innerHTML = '<p>' + esc(e.message) + '</p>' +
      '<p class="hint">Закройте и откройте приложение из чата бота заново.</p>';
  }
}

async function loadSummary() {
  try {
    const d = await api('GET', '/api/v1/maxapp/summary');
    ME = Object.assign(ME, d);
    $('points').textContent = ME.points;
    $('ref-code').textContent = ME.ref_code || '—';
    $('receipts').innerHTML = (ME.receipts || []).map(r =>
      '<div class="rrow"><span>чек …' + esc(String(r.fp || '').slice(-4)) +
      '</span><span>' + esc(r.sum ? r.sum.toFixed(2) + ' ₽' : '') +
      ' <span class="status">' + esc(statusRu(r.status)) + '</span></span></div>'
    ).join('') || '<p class="hint">Пока нет чеков — отсканируйте первый!</p>';
    $('profile-extra').textContent =
      'Всего чеков: ' + ((ME.receipts || []).length) +
      '. За каждый проверенный чек — балл. Приглашённому другу и вам — бонус.';
  } catch (e) {
    if (String(e.message).indexOf('401') >= 0 || /Bearer/.test(e.message)) {
      TOKEN = ''; boot(); return;
    }
    say(e.message, false);
  }
}

function statusRu(s) {
  return ({verified: '✓', pending: '…', rejected: '×'})[s] || '…';
}

// ---- сканер ---------------------------------------------------------
async function scanQr() {
  const app = wa();
  if (!app || !app.openCodeReader) { say('Сканер недоступен — вставьте строку', false); return; }
  try {
    if (app.requestScreenMaxBrightness) { try { await app.requestScreenMaxBrightness(); } catch (e) {} }
    const raw = await app.openCodeReader(false);   // камера; true — +галерея
    if (raw) await submit(raw);
  } catch (e) { say('Сканирование прервано', false); }
}

async function submit(qr) {
  const btn = $('btn-send') || $('btn-scan');
  if (btn) btn.disabled = true;
  say('Проверяем чек…');
  try {
    const d = await api('POST', '/api/v1/maxapp/scan',
                        {qr: String(qr).slice(0, 4000), form_ms: 5000});
    refresh(d);
    if (d.accepted === false && d.result === 'duplicate') {
      say('Этот чек уже в пуле — спасибо!');
    } else if (d.accepted === false) {
      say(d.message || 'Не принято', false);
    } else {
      say('Чек принят! Баллы начислим после проверки ✓');
      loadSummary();
    }
  } catch (e) { say(e.message, false); }
  finally { if (btn) btn.disabled = false; }
}

// ---- навигация ------------------------------------------------------
document.querySelectorAll('nav button').forEach(b => {
  b.onclick = () => {
    document.querySelectorAll('nav button').forEach(x => x.classList.remove('active'));
    b.classList.add('active');
    ['scan', 'receipts', 'profile'].forEach(t =>
      $('tab-' + t).classList.toggle('hidden', t !== b.dataset.tab));
  };
});
$('btn-scan').onclick = scanQr;
$('btn-send').onclick = () => {
  const v = $('qr-input').value.trim();
  if (v.length < 10) { say('Вставьте строку QR-кода чека', false); return; }
  submit(v);
};
$('btn-share').onclick = () => {
  const app = wa();
  const link = 'https://max.ru/chek.ymaster.bot?startapp=ref_' + (ME.ref_code || '');
  const text = 'Сдавай чеки в «Ямастер Чек» прямо в MAX — копи баллы! Мой код: ' + (ME.ref_code || '');
  try {
    if (app && app.shareMaxContent) app.shareMaxContent({text, link});
    else if (app && app.shareContent) app.shareContent({text, link});
    else window.open(link, '_blank');
  } catch (e) { window.open(link, '_blank'); }
};

boot();
