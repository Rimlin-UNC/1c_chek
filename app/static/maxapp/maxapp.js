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
      '<button type="button" class="btn" id="mx-retry">Повторить</button>';
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
    // v1.70.0: вход с сайта (login_…) и приглашения (inv_…)
    const sp = (app.initDataUnsafe && app.initDataUnsafe.start_param) || '';
    // v1.70.37 (152-ФЗ): при первом входе — текст согласия, и карточка
    // заполняется данными MAX только после явного «согласен». Подтверждение
    // входа с сайта (login_…) не задерживаем: это другая задача человека.
    if (d.consent_doc && sp.indexOf('login_') !== 0 && sp.indexOf('inv_') !== 0) {
      showConsent(d.consent_doc, afterBoot);
      return;
    }
    await afterBoot(sp);
  } catch (e) {
    // v1.70.36: если сессия не поднялась (подпись initData, устаревшая
    // сессия), человек не должен остаться без входа: вход на сайт устроен
    // как device-code и подтверждается кодом сообщением боту — подпись для
    // него не нужна. Даём короткий путь на сайт прямо отсюда.
    $('view-loading').innerHTML = '<p>' + esc(e.message) + '</p>' +
      '<p class="hint">Закройте и откройте приложение из чата бота заново.</p>' +
      '<p class="hint">Войти или зарегистрироваться можно и без ' +
      'мини-приложения: на сайте нажмите «Войти через MAX» и подтвердите ' +
      'вход кодом — сообщением этому боту в чате.</p>' +
      '<p style="margin-top:10px"><button type="button" class="btn" ' +
      'id="mx-site">Открыть сайт</button></p>';
    const sb = $('mx-site');
    if (sb) sb.onclick = () => { location.href = 'https://chek.ymaster.ru/'; };
  }
}

async function afterBoot(sp) {
  $('view-loading').classList.add('hidden');
  $('view-app').classList.remove('hidden');
  await loadSummary();
  if (!sp) {
    const app = wa();
    sp = (app && app.initDataUnsafe && app.initDataUnsafe.start_param) || '';
  }
  if (sp.indexOf('login_') === 0) showLoginConfirm(sp);
  else if (sp.indexOf('inv_') === 0) showInvite(sp);
}

// ---- v1.70.37: согласие на обработку данных из MAX (152-ФЗ) ---------
// До согласия сервер хранит только идентификатор MAX: имя, фамилия, ник,
// фото и язык в карточку не пишутся. Экран показывает действующую редакцию
// текста (GET /api/v1/maxapp/consent), а «согласен» уходит вместе с
// initData — подпись платформы подтверждает, кто именно согласился.
let REVOKE_ARMED = false;

function consentMsg(text, ok) {
  const m = $('consent-msg');
  if (!m) return;
  m.textContent = text || '';
  m.style.color = ok === false ? '#c0392b' : 'var(--accent-text)';
}

function renderConsent(doc) {
  $('consent-title').textContent = doc.title || 'Согласие на обработку данных';
  $('consent-ver').textContent = doc.version || '—';
  const parts = ['<p class="consent-lead">' + esc(doc.lead || '') + '</p>'];
  (doc.sections || []).forEach(sec => {
    parts.push('<div class="consent-sec"><div class="consent-sec-title">' +
      esc(sec.title) + '</div><ul>' +
      (sec.items || []).map(i => '<li>' + esc(i) + '</li>').join('') +
      '</ul></div>');
  });
  parts.push('<p class="hint">Оператор: ' + esc(doc.operator || '') +
    '. Вопросы и отзыв согласия: ' + esc(doc.contact || '') + '</p>');
  $('consent-body').innerHTML = parts.join('');
}

function showConsent(doc, next) {
  window._consentNext = next || null;
  renderConsent(doc);
  const nav = document.querySelector('nav');
  if (nav) nav.classList.add('hidden');
  $('view-loading').classList.add('hidden');
  $('view-app').classList.add('hidden');
  $('view-consent').classList.remove('hidden');
  const box = $('consent-agree'), ok = $('btn-consent-ok');
  box.checked = false;
  ok.disabled = true;
  consentMsg('');
  box.onchange = () => { ok.disabled = !box.checked; };
  ok.onclick = giveConsent;
  $('btn-consent-skip').onclick = () => closeConsent();
}

function closeConsent() {
  $('view-consent').classList.add('hidden');
  const nav = document.querySelector('nav');
  if (nav) nav.classList.remove('hidden');
  const cb = window._consentNext;
  window._consentNext = null;
  if (cb) { try { cb(); } catch (e) {} }
}

async function giveConsent() {
  const app = wa();
  if (!app || !app.initData) {
    consentMsg('MAX не передал данные входа — закройте и откройте приложение ' +
      'заново', false);
    return;
  }
  const ok = $('btn-consent-ok');
  ok.disabled = true;
  consentMsg('Сохраняем согласие…');
  try {
    const d = await api('POST', '/api/v1/maxapp/consent', {init_data: app.initData});
    ME = Object.assign(ME, d.me || {});
    consentMsg('Готово: профиль заполнен данными MAX ✓');
    renderMe();
    setTimeout(closeConsent, 400);
  } catch (e) {
    consentMsg(e.message, false);
    ok.disabled = !$('consent-agree').checked;
  }
}

async function openConsentScreen() {
  try {
    const doc = await api('GET', '/api/v1/maxapp/consent');
    showConsent(doc, () => {
      $('view-app').classList.remove('hidden');
      loadSummary();
    });
  } catch (e) { say(e.message, false); }
}

async function toggleConsent() {
  const cs = ME.consent || {};
  if (!cs.given) { openConsentScreen(); return; }
  // отзыв — в два нажатия: случайно стереть профиль нельзя
  if (!REVOKE_ARMED) {
    REVOKE_ARMED = true;
    renderMe();
    setTimeout(() => { if (REVOKE_ARMED) { REVOKE_ARMED = false; renderMe(); } }, 5000);
    return;
  }
  REVOKE_ARMED = false;
  try {
    const d = await api('POST', '/api/v1/maxapp/consent/revoke');
    ME = Object.assign(ME, d.me || {});
    renderMe();
    say(d.message || 'Согласие отозвано — профиль очищен');
  } catch (e) { say(e.message, false); }
}

function consentDate(iso) {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? m[3] + '.' + m[2] + '.' + m[1] : '';
}

function renderMe() {
  const nameEl = $('me-name');
  if (!nameEl) return;
  const full = [ME.first_name, ME.last_name].filter(Boolean).join(' ');
  nameEl.textContent = full || ME.name || 'Участник';
  const ph = $('me-photo');
  if (ME.photo_url) { ph.src = ME.photo_url; ph.classList.remove('hidden'); }
  else { ph.removeAttribute('src'); ph.classList.add('hidden'); }
  const srcText = ME.reg_source === 'tg'
    ? 'регистрация через бота Telegram'
    : (ME.reg_source === 'web' ? 'регистрация на сайте'
      : 'регистрация через мини-приложение MAX');
  $('me-src').textContent = srcText;
  const badge = document.querySelector('#me-card .src-max');
  if (badge) badge.classList.toggle('hidden', ME.reg_source !== 'max');
  const cs = ME.consent || {};
  const btn = $('btn-consent-toggle');
  if (cs.given) {
    $('me-consent').textContent = 'Согласие на обработку данных (редакция ' +
      (cs.version || '') + ') дано ' + (consentDate(cs.at) || '—') +
      '. Имя, фамилия, ник и фото взяты из MAX. Отозвать можно здесь же.';
    btn.textContent = REVOKE_ARMED ? 'Точно отозвать — очистить профиль'
      : 'Отозвать согласие';
  } else {
    $('me-consent').textContent = 'Профиль пуст: согласие на обработку данных ' +
      'из MAX не дано. Сервис хранит только ваш идентификатор MAX — он нужен, ' +
      'чтобы начислять баллы.';
    btn.textContent = 'Дать согласие и заполнить профиль';
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
    renderMe();
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

// v1.70.39: нативный сканер MAX возвращает ОБЪЕКТ с полем value, а не строку.
// Документация MAX SDK: «const { value } = await openCodeReader()», тип моста —
// Promise<{value?: unknown}>; сам max-web-app.js шлёт хосту запрос
// WebAppOpenCodeReader и отдаёт его ответ как есть. Раньше результат
// оборачивался в String(qr), поэтому на сервер уходило «[object Object]» —
// 15 символов, проверка длины их пропускала, и человек получал «Не похоже на
// чек: нет обязательных реквизитов», хотя камера честно находила код.
// Здесь текст достаётся из любой известной формы ответа, а если его нет —
// возвращаем пустую строку и честно говорим об этом, не отправляя мусор.
function qrFromScan(raw) {
  if (raw == null) return '';
  if (typeof raw === 'string') return raw.trim();
  if (typeof raw !== 'object') return '';
  const keys = ['value', 'result', 'data', 'text', 'code', 'qr', 'qr_data'];
  for (const k of keys) {
    const v = raw[k];
    if (typeof v === 'string' && v.trim()) return v.trim();
    if (v && typeof v === 'object') {
      const deep = qrFromScan(v);          // вложенный ответ моста
      if (deep) return deep;
    }
  }
  return '';
}

async function scanQr() {
  const app = wa();
  if (!app || !app.openCodeReader) {
    scanCamera();                    // v1.70.7: своя камера вместо тупика
    return;
  }
  try {
    if (app.requestScreenMaxBrightness) { try { await app.requestScreenMaxBrightness(); } catch (e) {} }
    const raw = await app.openCodeReader(false);   // камера; true — +галерея
    const text = qrFromScan(raw);                  // v1.70.39: объект → строка
    if (!text) {
      say('Сканер MAX не вернул текст кода. Повторите попытку или сдайте чек ' +
          'фотографией — кнопка «Фото QR».', false);
      return;
    }
    await submit(text);
  } catch (e) {
    scanCamera();                    // v1.70.7: камера, если MAX не открыл сканер
  }
}

async function submit(qr) {
  // v1.70.39: на сервер уходит ТОЛЬКО текст QR. Строка из поля ввода и
  // code.data своей камеры проходят как есть, объект из нативного сканера
  // раскрывается в value — «[object Object]» больше не может улететь на сервер.
  const text = qrFromScan(qr).slice(0, 4000);
  if (!text) { say('Не удалось прочитать текст QR-кода — повторите', false); return; }
  const btn = $('btn-send') || $('btn-scan');
  if (btn) btn.disabled = true;
  say('Проверяем чек…');
  try {
    const d = await api('POST', '/api/v1/maxapp/scan',
                        {qr: text, form_ms: 5000});
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
$('btn-cam').onclick = scanCamera;
$('btn-photo').onclick = pickPhoto;
$('qr-file').onchange = uploadPhoto;
$('btn-consent-toggle').onclick = toggleConsent;
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
function showService(title, html, onBack) {
  window._svcBack = onBack || null;
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
        const cb = window._svcBack;
        window._svcBack = null;
        if (cb) { try { cb(); } catch (e) {} }
        s.classList.add('hidden');
        $('view-app').classList.remove('hidden');
        try { app.BackButton.offClick(back); app.BackButton.hide(); } catch (e) {}
      };
      app.BackButton.onClick(back);
    } catch (e) {}
  }
}

// ---- v1.70.7: собственная камера (getUserMedia + jsQR) --------------
let _camStream = null;

function stopCam() {
  if (_camStream) {
    try { _camStream.getTracks().forEach(x => x.stop()); } catch (e) {}
    _camStream = null;
  }
}

function closeCam() {
  stopCam();
  const s = $('view-service');
  if (s) s.classList.add('hidden');
  const a = $('view-app');
  if (a) a.classList.remove('hidden');
}

async function scanCamera() {
  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia(
      {video: {facingMode: 'environment'}});
  } catch (e) {
    say('Камера недоступна — выберите фото с QR-кодом', false);
    pickPhoto();
    return;
  }
  _camStream = stream;
  showService('Сканер камеры',
    '<video id="cam-v" autoplay playsinline muted ' +
    'style="width:100%;border-radius:12px;background:#000"></video>' +
    '<p class="hint" style="margin-top:8px">Наведите камеру на QR чека — ' +
    'распознаётся автоматически</p>' +
    '<div style="height:8px"></div>' +
    '<button type="button" class="btn secondary" id="cam-photo">🖼 Не считывается — выбрать фото</button>',
    closeCam);
  const v = $('cam-v');
  v.srcObject = stream;
  try { await v.play(); } catch (e) {}
  $('cam-photo').onclick = () => { closeCam(); pickPhoto(); };
  const cv = document.createElement('canvas');
  const cx = cv.getContext('2d', {willReadFrequently: true});
  const tick = () => {
    if (!_camStream) return;                     // экран закрыли
    if (v.videoWidth > 0) {
      cv.width = v.videoWidth;
      cv.height = v.videoHeight;
      cx.drawImage(v, 0, 0);
      let code = null;
      try {
        const img = cx.getImageData(0, 0, cv.width, cv.height);
        code = window.jsQR(img.data, img.width, img.height);
      } catch (e) {}
      if (code && code.data) {
        const raw = code.data;
        closeCam();
        submit(raw);
        return;
      }
    }
    setTimeout(tick, 180);
  };
  tick();
}

async function showLoginConfirm(code) {
  showService('Вход на сайт',
    '<p>Сайт <b>chek.ymaster.ru</b> запрашивает вход под вашим ' +
    'MAX-аккаунтом' + (ME.name ? ' (' + esc(ME.name) + ')' : '') + '.</p>' +
    '<p class="hint">Подтвердите — на сайте всё произойдёт автоматически. ' +
    'Если это не вы открывали сайт — просто закройте этот экран.</p>' +
    '<div style="height:10px"></div>' +
    '<button type="button" class="btn" id="svc-confirm">Подтвердить вход</button>' +
    '<div style="height:8px"></div><div id="svc-msg" class="hint"></div>');
  $('svc-confirm').onclick = async () => {
    try {
      // v1.70.37: initData — чтобы сервер взял username MAX из подписанного
      // payload: в карточке участника имени до согласия (152-ФЗ) нет.
      await api('POST', '/api/v1/maxapp/confirm-login',
                {code: code, init_data: (wa() && wa().initData) || ''});
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
      '<button type="button" class="btn" id="svc-accept">Принять и открыть сайт</button>' +
      '<div style="height:8px"></div><div id="svc-msg" class="hint"></div>';
    $('svc-accept').onclick = async () => {
      try {
        const r = await api('POST', '/api/v1/maxapp/invite/accept',
                            // v1.70.37: initData — username MAX для учётной
                            // записи сайта берётся из подписанного payload
                            {token: token,
                             init_data: (wa() && wa().initData) || ''});
        $('svc-msg').textContent = 'Готово! Открываем сайт…';
        $('svc-accept').disabled = true;
        setTimeout(() => openLink(r.web_link), 600);
      } catch (e) { $('svc-msg').textContent = e.message; }
    };
  } catch (e) { $('service-body').innerHTML = '<p>' + esc(e.message) + '</p>'; }
}

boot();
