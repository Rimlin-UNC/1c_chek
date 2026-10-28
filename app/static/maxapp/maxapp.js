// Ямастер Чек — мини-приложение MAX (v1.69.0)
// ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
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
    // v1.70.7: камера, если MAX не открыл сканер. v1.70.42: внутри непрерывного
    // потока запасной путь уже свой (непрерывная камера), одиночный кадр не нужен
    if (!BATCH.loop) scanCamera();
  }
}

async function submit(qr) {
  // v1.70.39: на сервер уходит ТОЛЬКО текст QR. Строка из поля ввода и
  // code.data своей камеры проходят как есть, объект из нативного сканера
  // раскрывается в value — «[object Object]» больше не может улететь на сервер.
  const text = qrFromScan(qr).slice(0, 4000);
  if (!text) { say('Не удалось прочитать текст QR-кода — повторите', false); return; }
  // v1.70.42: ОДИН конвейер приёма. Сканер MAX, своя камера, фото и вставка
  // строки идут в одну сессию: общие счётчики, мгновенная проверка дубликата и
  // уточнение статусов опросом. Второй ветки отправки больше нет — раньше
  // одиночный скан и «пачка» соревновались за один и тот же суточный лимит,
  // показывая разные картинки одному человеку.
  const btn = $('btn-send');
  if (btn) btn.disabled = true;
  try {
    ensureSession();
    await batchHandle(text);
  } finally { if (btn) btn.disabled = false; }
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
// v1.70.42: единственная кнопка сканирования — запускает общий поток сдачи
$('btn-scan').onclick = startBatch;
$('btn-send').onclick = () => {
  const v = $('qr-input').value.trim();
  if (v.length < 10) { say('Вставьте строку QR-кода чека', false); return; }
  submit(v);
};
$('btn-cam').onclick = scanCamera;
$('btn-photo').onclick = pickPhoto;
$('qr-file').onchange = uploadPhoto;
$('btn-consent-toggle').onclick = toggleConsent;
// v1.70.64: владелец — «в мини приложении в профиле добавить кнопку
// открыть приложение и сделать переход в приложение»
const oaBtn = $('btn-open-app');
if (oaBtn) oaBtn.onclick = () => openLink(location.origin + '/');

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
      const r = await api('POST', '/api/v1/maxapp/confirm-login',
                {code: code, init_data: (wa() && wa().initData) || ''});
      $('svc-confirm').disabled = true;
      // v1.70.64: перекидываем на сайт сами (билет одноразовый); если
      // окружение не дало открыть ссылку — прежняя подсказка рукой.
      if (r && r.web_link) {
        $('svc-msg').textContent = 'Готово! Открываем сайт…';
        setTimeout(() => openLink(r.web_link), 600);
      } else {
        $('svc-msg').textContent =
          'Готово! Вернитесь на сайт — вход выполнится сам.';
      }
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

// ==========================================================================
//  v1.70.40: поточная сдача чеков (до 100 за сессию).
//  v1.70.42: ОДИН поток сдачи — владелец: «не стоит плодить пакетное
//   сканирование и сканирование qr-чеком». Отдельной кнопки «Сканировать
//   пачкой» больше нет: обычная кнопка сканирования открывает тот же поток
//   (камера открывается снова после каждого чека), а сессия со счётчиками
//   начинается с первого чека любым способом ввода — сканер MAX, своя камера,
//   фото или вставка строки. Конвейер приёма один: batchHandle → /scan,
//   уточнение статусов → /batch-status, рубеж 100 → окно «продолжаем?»
//   • камера открывается снова после каждого чека — кнопок нажимать не нужно,
//     сдачу завершают «Готово» или отмена сканера;
//   • каждый чек проверяется СРАЗУ: дубликат (ФН+ФД+ФП уже в базе) — красный
//     счётчик и сообщение «уже есть в базе», новый чек — синий «на модерации»
//     и анимация «чек улетает в пул»;
//   • статусы уточняются опросом /batch-status: синий становится оранжевым
//     (принят, балл начислен) или красным (не прошёл проверку);
//   • после каждых 100 чеков — окно «продолжаем или закончим?» (рубеж
//     сдвигается ещё на 100, суточный лимит при этом остаётся прежним);
//   • суточный лимит антифрода виден заранее: «сегодня сдано N из 100».
//
//  Источник истины — сервер: клиентская проверка повторов внутри сессии
//  экономит запрос и мгновенно отвечает на повторное сканирование того же
//  чека, но окончательное решение о дубликате принимает precheck_ingest.
// ==========================================================================
const BATCH_CAP = 100;          // рубеж сессии = суточный лимит (ingest.DAILY_LIMIT)
const BATCH_POLL_MS = 2500;     // интервал опроса статусов
const BATCH_POLL_MAX = 40;      // ~100 с: дальше статусы видны во вкладке «Чеки»
const BATCH_ROWS = 8;           // сколько последних чеков показывать в панели
const BATCH = {
  active: false, loop: false, reads: 0, accepted: 0,
  items: [], seen: {}, counts: {ok: 0, pend: 0, bad: 0},
  cap: BATCH_CAP, tries: 0, timer: 0, t0: 0,
  day: {today: 0, limit: BATCH_CAP, left: BATCH_CAP},
};
let _bcam = null;               // непрерывная своя камера (если нет сканера MAX)

function batchKey(fn, fd, fp) {
  return (fn || '') + '|' + (fd || '') + '|' + (fp || '');
}

function batchStateRow(x) {
  const label = x.state === 'ok' ? 'принят' : (x.state === 'pend' ? 'на модерации'
                                                                   : 'не принят');
  const sum = x.sum ? ' · ' + Number(x.sum).toFixed(2) + ' ₽' : '';
  const who = x.fd ? 'ФД ' + x.fd : (x.who || 'чек');
  return '<div class="brow s-' + x.state + '"><span class="bdot"></span>' +
    '<span class="bwho">' + esc(who) + '</span>' +
    '<span class="bst">' + esc(label + sum) + '</span></div>';
}

// v1.70.43: импульс фазы и строка счётчиков запасной камеры — те же три цвета,
// что у плиток карточки. Каждая фаза служит целью прилёта чека, поэтому после
// сканирования видно, КУДА именно попал чек: принят, на модерации или нет.
function pulsePhase(el) {
  if (!el) return;
  el.classList.remove('pulse');
  void el.offsetWidth;                     // перезапуск анимации импульса
  el.classList.add('pulse');
}

function camCountsHTML(c) {
  return 'принято <span class="seg seg-ok" id="cam-seg-ok">' + c.ok + '</span>' +
    ' · на модерации <span class="seg seg-pend" id="cam-seg-pend">' + c.pend +
    '</span> · не принято <span class="seg seg-bad" id="cam-seg-bad">' + c.bad +
    '</span>';
}

function batchRender() {
  const c = {ok: 0, pend: 0, bad: 0};
  BATCH.items.forEach((x) => { if (c[x.state] !== undefined) c[x.state] += 1; });
  BATCH.counts = c;
  const set = (id, n) => {
    const el = $(id);
    if (el) { const b = el.querySelector('b'); if (b) b.textContent = n; }
  };
  set('bc-ok', c.ok); set('bc-pend', c.pend); set('bc-bad', c.bad);
  const prog = $('batch-prog');
  if (prog) prog.textContent = BATCH.items.length + ' / ' + BATCH.cap;
  const day = $('batch-day');
  if (day) {
    day.textContent = 'Сегодня сдано ' + BATCH.day.today + ' из ' + BATCH.day.limit +
      (BATCH.day.left > 0 ? ' — осталось ' + BATCH.day.left
                          : ' — суточный лимит исчерпан');
  }
  const list = $('batch-list');
  if (list) {
    list.innerHTML = BATCH.items.slice(-BATCH_ROWS).reverse().map(batchStateRow).join('');
  }
  const cam = $('cam-counts');
  if (cam) {
    cam.innerHTML = camCountsHTML(c);
  }
}

// «Чек улетает в пул»: короткий полёт к нужному счётчику + импульс счётчика.
// При настройке «меньше движения» анимация отключена в CSS — счётчики работают.
function batchFly(state) {
  const id = state === 'ok' ? 'ok' : (state === 'pend' ? 'pend' : 'bad');
  const camOn = !!$('cam-v');                  // запасная камера открыта
  const seg = camOn ? $('cam-seg-' + id) : null;
  const target = seg || $('bc-' + id);
  if (!target) return;
  pulsePhase(target);
  if (seg) pulsePhase($('bc-' + id));          // плитка карточки мигает вместе
  let reduced = false;
  try {
    reduced = window.matchMedia &&
      window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  } catch (e) { reduced = false; }
  if (reduced) return;
  // Источник — то, откуда чек только что пришёл: видео запасной камеры, иначе
  // кнопка сканирования. Раньше при открытой камере значок летел из левого
  // верхнего угла, потому что скрытая кнопка возвращала нулевой прямоугольник.
  const src = (camOn ? $('cam-v') : $('btn-scan')) || target;
  const a = src.getBoundingClientRect();
  const b = target.getBoundingClientRect();
  const chip = document.createElement('div');
  chip.className = 'fly-chip s-' + state;
  chip.textContent = state === 'bad' ? '✕' : '🧾';
  chip.style.left = Math.round(a.left + a.width / 2 - 14) + 'px';
  chip.style.top = Math.round(a.top + 4) + 'px';
  chip.style.setProperty('--fly-x',
    Math.round(b.left + b.width / 2 - a.left - a.width / 2) + 'px');
  chip.style.setProperty('--fly-y', Math.round(b.top - a.top) + 'px');
  document.body.appendChild(chip);
  setTimeout(() => { try { chip.remove(); } catch (e) {} }, 700);
}

function batchAdd(item) {
  BATCH.items.push(item);
  batchRender();
  batchFly(item.state);
}

function batchSetButtons(busy) {
  // одиночные способы сдачи на время пачки выключаем — один конвейер, без путаницы
  ['btn-scan', 'btn-cam', 'btn-photo'].forEach((id) => {
    const b = $(id);
    if (b) b.disabled = busy;
  });
}

function batchSetDay(d) {
  if (d && typeof d.today === 'number') {
    BATCH.day = {today: d.today, limit: d.limit || BATCH.day.limit,
                 left: (typeof d.left === 'number' ? d.left
                                                   : Math.max(0, (d.limit || BATCH.day.limit) - d.today))};
  }
  if (d && typeof d.user_points === 'number') refresh(d);
}

// v1.70.42: сессия начинается с ПЕРВОГО чека любым способом ввода — отдельной
// кнопки «пачкой» нет ни в разметке, ни в логике. Одиночный чек это та же
// сессия, в которой один чек: счётчики, проверка дубликата и уточнение статусов
// работают одинаково.
function ensureSession() {
  if (BATCH.active) return;
  BATCH.active = true; BATCH.loop = false; BATCH.reads = 0; BATCH.accepted = 0;
  BATCH.items = []; BATCH.seen = {}; BATCH.tries = 0; BATCH.t0 = Date.now();
  BATCH.cap = BATCH_CAP;
  BATCH.counts = {ok: 0, pend: 0, bad: 0};
  const sum = $('batch-sum');
  if (sum) { sum.textContent = ''; sum.classList.add('hidden'); }
  const card = $('batch-card');
  if (card) card.classList.remove('hidden');
  batchRender();
}

// ---- приём одного чека в сессию ------------------------------------------
async function batchHandle(text) {
  const norm = String(text || '').replace(/\s+/g, '');
  if (!norm) return;
  BATCH.reads += 1;            // сканер сработал — шаг непрерывного потока
  if (BATCH.seen[norm]) {
    // повтор внутри сессии: отвечаем мгновенно, запрос на сервер не уходит
    batchAdd({state: 'bad', who: 'повтор в этой сессии', message: ''});
    say('Этот чек вы уже сдали в этой сессии — дважды он в базу не попадёт', false);
    return;
  }
  BATCH.seen[norm] = true;
  let d;
  try {
    d = await api('POST', '/api/v1/maxapp/scan',
                  {qr: String(text).slice(0, 4000), form_ms: 5000});
  } catch (e) {
    BATCH.seen[norm] = false;      // не отправлено — можно повторить
    batchAdd({state: 'bad', who: 'не отправлено', message: e.message || ''});
    say(e.message || 'Сервер недоступен — чек не отправлен', false);
    return;
  }
  batchSetDay(d);
  if (d.accepted === false) {
    const dup = d.result === 'duplicate';
    batchAdd({state: 'bad', who: dup ? 'дубликат' : 'не принято',
              message: d.message || ''});
    say(dup ? (d.message || 'Такой чек уже есть в базе — не принят')
            : (d.message || 'Не принято'), false);
    if (d.result === 'flood') stopBatch('flood');
    return;
  }
  batchAdd({state: 'pend', fn: d.fn || '', fd: d.fd || '', fp: d.fp || '',
            key: batchKey(d.fn, d.fd, d.fp), sum: 0,
            message: d.message || 'Чек принят — проверяем'});
  BATCH.accepted += 1;
  if (BATCH.accepted === 1) loadSummary();   // одиночная сдача видит баллы сразу
  say('Чек добавлен в пул — проверяем ✓');
  batchPollSoon();
}

// ---- уточнение статусов (синий → оранжевый/красный) -----------------------
function batchPollSoon() {
  if (BATCH.timer) return;
  BATCH.timer = setTimeout(batchPoll, BATCH_POLL_MS);
}

async function batchPoll() {
  BATCH.timer = 0;
  const pend = BATCH.items.filter((x) => x.state === 'pend' && x.key);
  if (!pend.length) return;
  if (BATCH.tries >= BATCH_POLL_MAX) return;
  BATCH.tries += 1;
  let d;
  try {
    d = await api('POST', '/api/v1/maxapp/batch-status',
                  {keys: pend.map((x) => ({fn: x.fn, fd: x.fd, fp: x.fp}))});
  } catch (e) { batchPollSoon(); return; }    // сеть мигнула — попробуем ещё
  let changed = 0;
  (d.items || []).forEach((it) => {
    if (!it || !it.found || !it.status) return;
    const key = batchKey(it.fn, it.fd, it.fp);
    const row = BATCH.items.find((x) => x.key === key);
    if (!row) return;
    const st = it.status === 'verified' ? 'ok'
      : (it.status === 'pending' ? 'pend' : 'bad');
    row.sum = it.sum || row.sum || 0;
    row.points = it.points || 0;
    if (it.message) row.message = it.message;
    if (st !== row.state) { row.state = st; changed += 1; batchFly(st); }
  });
  batchSetDay(d);
  if (changed) {
    batchRender();
    const ok = BATCH.counts.ok;
    say(ok ? 'Принято в пул: ' + ok + ' (баллы начислены)' : 'Есть чеки, которые не прошли проверку',
        ok ? true : false);
  }
  if (BATCH.items.some((x) => x.state === 'pend')) batchPollSoon();
}

// ---- непрерывная своя камера (когда сканера MAX нет) ----------------------
function batchCamClose(finish) {
  if (_bcam) {
    const ws = _bcam.waiters;
    _bcam = null;
    ws.forEach((w) => { try { w(''); } catch (e) {} });
  }
  stopCam();
  const svc = $('view-service');
  if (svc) svc.classList.add('hidden');
  const appView = $('view-app');
  if (appView) appView.classList.remove('hidden');
  if (finish) stopBatch('manual');
}

async function batchCamOpen() {
  let stream = null;
  try {
    stream = await navigator.mediaDevices.getUserMedia(
      {video: {facingMode: 'environment'}});
  } catch (e) { return false; }
  _camStream = stream;                  // общий stopCam() останавливает и её
  showService('Сканер чеков',
    '<video id="cam-v" autoplay playsinline muted ' +
    'style="width:100%;border-radius:12px;background:#000"></video>' +
    '<div class="cam-counts hint" id="cam-counts">' +
    camCountsHTML({ok: 0, pend: 0, bad: 0}) + '</div>' +
    '<p class="hint" style="margin-top:8px">Наводите камеру на QR чеков один за ' +
    'другим — каждый распознаётся сам и сразу проверяется на дубликат.</p>' +
    '<div style="height:8px"></div>' +
    '<button type="button" class="btn secondary" id="cam-done">✅ Готово — ' +
    'закончить сдачу</button>',
    () => batchCamClose(true));
  const v = $('cam-v');
  v.srcObject = stream;
  try { await v.play(); } catch (e) {}
  const done = $('cam-done');
  if (done) done.onclick = () => batchCamClose(true);
  _bcam = {waiters: []};
  batchRender();
  const cv = document.createElement('canvas');
  const cx = cv.getContext('2d', {willReadFrequently: true});
  let last = {text: '', at: 0};
  const tick = () => {
    if (!_bcam) return;                          // экран закрыли
    if (v.videoWidth > 0) {
      cv.width = v.videoWidth; cv.height = v.videoHeight;
      cx.drawImage(v, 0, 0);
      let code = null;
      try {
        const img = cx.getImageData(0, 0, cv.width, cv.height);
        code = window.jsQR(img.data, img.width, img.height);
      } catch (e) {}
      if (code && code.data) {
        const now = Date.now();
        // антисдвоение: тот же код не чаще раза в 2.5 с (как на сайте)
        if (!(code.data === last.text && now - last.at < 2500)) {
          last = {text: code.data, at: now};
          const w = _bcam.waiters.shift();
          if (w) { try { w(code.data); } catch (e) {} }
        }
      }
    }
    setTimeout(tick, 180);
  };
  tick();
  return true;
}

function cameraBatchOnce() {
  return new Promise((resolve) => {
    if (_bcam) { _bcam.waiters.push(resolve); return; }
    batchCamOpen().then((ok) => {
      if (!ok || !_bcam) { resolve(''); return; }
      _bcam.waiters.push(resolve);
    }).catch(() => resolve(''));
  });
}

// один шаг единого потока: сканер MAX первым, своя камера — запасной путь.
// v1.70.42: читатель ОДИН — нативный сканер читает и сдаёт scanQr() (тот же
// примитив, что и одиночное нажатие), свою непрерывную камеру открывает
// cameraBatchOnce(). Возвращает true, если чек прочитан и сдан, и false, если
// сканер отменён или не вернул текст: для потока это «готово», а не ошибка.
async function batchScanOnce() {
  const app = wa();
  if (app && typeof app.openCodeReader === 'function') {
    const before = BATCH.reads;
    try { await scanQr(); } catch (e) { return false; }
    return BATCH.reads > before;
  }
  const text = await cameraBatchOnce();
  if (!text) return false;
  await batchHandle(text);
  return true;
}

// ---- окно рубежа «100 чеков» ---------------------------------------------
function batchAskMore() {
  return new Promise((resolve) => {
    const m = $('batch-modal');
    const more = $('btn-batch-more');
    const fin = $('btn-batch-finish');
    const txt = $('batch-modal-text');
    const title = $('batch-modal-title');
    if (!m || !more || !fin) { resolve(false); return; }
    if (title) title.textContent = 'Сдано ' + BATCH.items.length + ' чеков';
    if (txt) {
      txt.textContent = 'Вы отсканировали ' + BATCH.items.length +
        ' чеков. Продолжаем сдачу или закончим и посмотрим итог?';
    }
    m.classList.remove('hidden');
    const done = (val) => {
      m.classList.add('hidden');
      more.onclick = null; fin.onclick = null;
      resolve(val);
    };
    more.onclick = () => { BATCH.cap += BATCH_CAP; batchRender(); done(true); };
    fin.onclick = () => done(false);
  });
}

// ---- итог сессии ----------------------------------------------------------
function batchSummaryText() {
  const c = BATCH.counts;
  const secs = Math.max(1, Math.round((Date.now() - BATCH.t0) / 1000));
  const mins = Math.floor(secs / 60);
  const dur = mins ? mins + ' мин ' + (secs % 60) + ' с' : secs + ' с';
  const pts = BATCH.items.reduce((a, x) => a + (x.points || 0), 0);
  return 'Итог сдачи: принято ' + c.ok + ', на модерации ' + c.pend +
    ', не принято ' + c.bad + ' · баллов +' + pts + ' · время ' + dur +
    '. Сегодня сдано ' + BATCH.day.today + ' из ' + BATCH.day.limit + '.';
}

function stopBatch(reason) {
  const was = BATCH.active;
  BATCH.active = false;
  BATCH.loop = false;
  if (_bcam) batchCamClose(false);
  batchSetButtons(false);
  const sum = $('batch-sum');
  if (sum) {
    let text = batchSummaryText();
    if (reason === 'flood') {
      text = 'Суточный лимит исчерпан — дальше чеки не принимаются. ' + text;
    } else if (reason === 'empty') {
      text = 'Ни одного чека не сдано. ' + text;
    }
    sum.textContent = text;
    sum.classList.remove('hidden');
  }
  if (was) say(batchSummaryText(), BATCH.counts.bad === 0);
  loadSummary();
  if (BATCH.items.some((x) => x.state === 'pend')) batchPollSoon();
}

async function startBatch() {
  if (BATCH.loop) { say('Сканер уже открыт — сканируйте следующий чек'); return; }
  ensureSession();
  BATCH.loop = true;
  batchSetButtons(true);
  say('Сканируйте чеки один за другим — камера открывается снова после каждого. '
    + '«Готово» — закончить сдачу.');
  // цикл сдачи: сканер открывается сам после каждого чека
  while (BATCH.active && BATCH.loop) {
    if (BATCH.items.length >= BATCH.cap) {
      const more = await batchAskMore();
      if (!more) break;
    }
    let ok = false;
    try { ok = await batchScanOnce(); } catch (e) { ok = false; }
    if (!ok) break;                    // отмена сканера = «готово», не ошибка
  }
  stopBatch('done');
}


// ---- v1.70.42: кнопка «Готово» завершает единый поток сдачи ---------
$('btn-batch-stop').onclick = () => stopBatch('manual');

boot();
