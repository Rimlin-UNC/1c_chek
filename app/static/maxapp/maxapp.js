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
function waitForBridge(ms) {
  // Bridge (st.max.ru) может догружаться асинхронно — ждём до ms мс
  return new Promise((res) => {
    const t0 = Date.now();
    const tick = () => {
      if (wa() && wa().initData) return res(true);
      if (Date.now() - t0 >= ms) return res(false);
      setTimeout(tick, 150);
    };
    tick();
  });
}

async function boot() {
  const ok = await waitForBridge(1600);
  const app = wa();
  if (!app || !app.initData) {
    const bridge = !!app;
    $('view-loading').innerHTML =
      (bridge
        ? '<p>MAX не передал данные входа.</p>'
        : '<p>Откройте приложение из чата вашего бота в MAX — ' +
          'там работает вход без пароля.</p>') +
      '<div style="height:10px"></div>' +
      '<p class="hint">Откройте чат вашего бота в MAX и запустите ' +
      'приложение кнопкой в чате.</p>' +
      '<div style="height:8px"></div>' +
      '<button class="btn" id="mx-retry">Повторить</button>';
    $('mx-retry').onclick = () => { location.reload(); };
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
    // v1.70.0: вход с сайта (login_…) и приглашения (inv_…)
    const sp = (app.initDataUnsafe && app.initDataUnsafe.start_param) || '';
    if (sp.indexOf('login_') === 0) showLoginConfirm(sp);
    else if (sp.indexOf('inv_') === 0) showInvite(sp);
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
function pickPhoto() {
  const f = $('qr-file');
  if (f) f.click();
}

async function uploadPhoto(ev) {
  const f = ev && ev.target ? ev.target.files[0] : null;
  if (!f) return;
  say('Ищем QR на фото…');
  const fd = new FormData();
  fd.append('file', f);
  try {
    const r = await fetch(API + '/api/v1/maxapp/scan-image', {
      method: 'POST',
      headers: TOKEN ? {Authorization: 'Bearer ' + TOKEN} : {},
      body: fd,
    }).then(async x => {
      const d = await x.json().catch(() => ({}));
      if (!x.ok) throw new Error(d.detail || ('HTTP ' + x.status));
      return d;
    });
    refresh(r);
    if (r.accepted === false) say(r.message || 'Не принято', false);
    else { say('Чек принят! Баллы начислим после проверки ✓'); loadSummary(); }
  } catch (e) { say(e.message, false); }
  finally { $('qr-file').value = ''; }
}

async function scanQr() {
  const app = wa();
  if (!app || !app.openCodeReader) {
    say('Сканер недоступен — выберите фото с QR-кодом', false);
    pickPhoto();
    return;
  }
  try {
    if (app.requestScreenMaxBrightness) { try { await app.requestScreenMaxBrightness(); } catch (e) {} }
    const raw = await app.openCodeReader(false);   // камера; true — +галерея
    if (raw) await submit(raw);
  } catch (e) {
    say('Сканер не открылся — выберите фото с QR-кодом', false);
    pickPhoto();
  }
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
$('btn-photo').onclick = pickPhoto;
$('qr-file').onchange = uploadPhoto;
$('btn-share').onclick = () => {
  const app = wa();
  const link = ME.ref_link || 'https://max.ru/chek.ymaster.bot?startapp=ref_' + (ME.ref_code || '');
  const text = 'Сдавай чеки в «Ямастер Чек» прямо в MAX — копи баллы! Мой код: ' + (ME.ref_code || '');
  try {
    if (app && app.shareMaxContent) app.shareMaxContent({text, link});
    else if (app && app.shareContent) app.shareContent({text, link});
    else window.open(link, '_blank');
  } catch (e) { window.open(link, '_blank'); }
};

function openLink(url) {
  const app = wa();
  try { if (app && app.openLink) { app.openLink(url); return; } } catch (e) {}
  window.open(url, '_blank');
}

// ---- v1.70.0: сервис-экраны (вход с сайта, приглашения) -------------
function showService(title, html) {
  $('view-app').classList.add('hidden');
  const s = $('view-service');
  s.classList.remove('hidden');
  $('service-title').textContent = title;
  $('service-body').innerHTML = html;
  const app = wa();
  if (app && app.BackButton && app.BackButton.show) {
    try {
      app.BackButton.show();
      const back = () => {
        s.classList.add('hidden');
        $('view-app').classList.remove('hidden');
        try { app.BackButton.offClick(back); app.BackButton.hide(); } catch (e) {}
      };
      app.BackButton.onClick(back);
    } catch (e) {}
  }
}

async function showLoginConfirm(code) {
  showService('Вход на сайт',
    '<p>Сайт <b>chek.ymaster.ru</b> запрашивает вход под вашим ' +
    'MAX-аккаунтом' + (ME.name ? ' (' + esc(ME.name) + ')' : '') + '.</p>' +
    '<p class="hint">Подтвердите — на сайте всё произойдёт автоматически. ' +
    'Если это не вы открывали сайт — просто закройте этот экран.</p>' +
    '<div style="height:10px"></div>' +
    '<button class="btn" id="svc-confirm">Подтвердить вход</button>' +
    '<div style="height:8px"></div><div id="svc-msg" class="hint"></div>');
  $('svc-confirm').onclick = async () => {
    try {
      await api('POST', '/api/v1/maxapp/confirm-login', {code: code});
      $('svc-msg').textContent = 'Готово! Вернитесь на сайт — вход выполнится сам.';
      $('svc-confirm').disabled = true;
    } catch (e) { $('svc-msg').textContent = e.message; }
  };
}

async function showInvite(token) {
  showService('Приглашение', '<p class="hint">Загружаем…</p>');
  try {
    const d = await api('GET',
      '/api/v1/maxapp/invite?token=' + encodeURIComponent(token));
    if (!d.valid) {
      $('service-body').innerHTML =
        '<p>' + esc(d.message || 'Приглашение недействительно') + '</p>';
      return;
    }
    const roleRu = {accountant: 'Бухгалтер', manager: 'Руководитель',
                    user: 'Пользователь'}[d.role] || d.role;
    $('service-body').innerHTML =
      '<p>Вас пригласили в «Ямастер Чек»' +
      (d.company_name ? ' — компания <b>' + esc(d.company_name) + '</b>' : '') +
      '.<br>Роль: <b>' + esc(roleRu) + '</b>' +
      (d.expires_at ? '<br><span class="hint">действует до ' +
        esc(d.expires_at) + '</span>' : '') + '</p>' +
      '<div style="height:10px"></div>' +
      '<button class="btn" id="svc-accept">Принять и открыть сайт</button>' +
      '<div style="height:8px"></div><div id="svc-msg" class="hint"></div>';
    $('svc-accept').onclick = async () => {
      try {
        const r = await api('POST', '/api/v1/maxapp/invite/accept',
                            {token: token});
        $('svc-msg').textContent = 'Готово! Открываем сайт…';
        $('svc-accept').disabled = true;
        setTimeout(() => openLink(r.web_link), 600);
      } catch (e) { $('svc-msg').textContent = e.message; }
    };
  } catch (e) { $('service-body').innerHTML = '<p>' + esc(e.message) + '</p>'; }
}

boot();
