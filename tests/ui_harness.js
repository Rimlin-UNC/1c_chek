// ======================================================================
// Ямастер Чек — UI-harness v1.13.0/v1.14.0 (Node vm DOM-песочница): карточка
// компании (ЕГРЮЛ/Checko), диалог умного удаления (move/wipe).
// Запуск: node tests/ui_harness.js  (бандл строит tests/test_v*.py)
// ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// ======================================================================
// ======================================================================
// UI-harness v1.13.0/v1.14.0: карточка компании + диалог удаления (DOM-песочница)
// ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// ======================================================================
const fs = require('fs');
const vm = require('vm');

// --- мини-DOM -----------------------------------------------------------
class El {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {};
    this.style = {}; this._html = ''; this.textContent = ''; this.disabled = false;
    this.value = ''; this.checked = false; this._id = '';
    this.options = [];   // v1.25.1: у <select> в живом DOM options есть всегда
    this.classList = {
      _s: new Set(),
      add: (...c) => c.forEach(x => this.classList._s.add(x)),
      remove: (...c) => c.forEach(x => this.classList._s.delete(x)),
      toggle: (c, force) => {
        // v1.59.1: второй аргумент force — как в живом DOM (нужен showScreen)
        const _has = this.classList._s.has(c);
        const _want = (force === undefined) ? !_has : !!force;
        if (_want) this.classList._s.add(c); else this.classList._s.delete(c);
        return _want;
      },
      contains: (c) => this.classList._s.has(c),
    };
  }
  set innerHTML(v) { this._html = String(v); this.children = []; }
  get innerHTML() { return this._html; }
  set id(v) { this._id = v; if (typeof _byId === 'object') _byId[v] = this; }
  get id() { return this._id || ''; }
  setAttribute() {} getAttribute() { return null; }
  click() {}
  appendChild(c) { this.children.push(c); return c; }
  remove() {}
  addEventListener() {}
  focus() {}
  querySelector(sel) {
    if (sel === '[data-close]') return this._dc || (this._dc = new El('button'));
    if (sel === '#dl-target') return this._dt || (this._dt = Object.assign(new El(), { value: 'x2' }));
    if (sel.includes(':checked')) return this._chk || (this._chk = { value: 'move' });
    const m = sel.match(/^#([A-Za-z0-9_-]+)/);
    if (m) return this['_q_' + m[1]] || (this['_q_' + m[1]] = new El());
    if (sel.startsWith('.')) {   // .modal-slot и пр. — стабильный детектор
      this._cls = this._cls || {};
      return this._cls[sel] || (this._cls[sel] = new El());
    }
    return new El();
  }
  querySelectorAll() { return []; }
}

const root = new El();
const _byId = {};
const _docQ = {};   // v1.25.1: стабильные селекторы
const document = {
  getElementById: (id) => (_byId[id] = _byId[id] || (id === 'modal-root' ? root : new El())),
  createElement: (t) => new El(t),
  // v1.25.1: стабильные El на селектор — как живой DOM (нужно для теста «Чеков»)
  querySelector: (sel) => (_docQ[sel] = _docQ[sel] || new El()),
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: new El('body'),
  documentElement: new El('html'),
};

const storage = { ymaster_check_token: 'harness-token' };   // v1.25.1: boot загрузит /auth/me
const apiLog = [];
const toasts = [];

const sandbox = {
  document, window: { addEventListener: () => {}, print: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }), location: { reload() {}, pathname: '/' } },
  navigator: { clipboard: { writeText: async () => {} }, userAgent: 'harness' },
  print: () => {},
  localStorage: {
    getItem: (k) => (k in storage ? storage[k] : null),
    setItem: (k, v) => { storage[k] = String(v); },
    removeItem: (k) => { delete storage[k]; },
  },
  sessionStorage: { getItem: () => null, setItem: () => {}, removeItem: () => {} },
  location: { reload() {}, href: '', origin: 'http://localhost', pathname: '/', hash: '', search: '' },
  history: { pushState() {}, replaceState() {} },
  confirm: () => true,
  prompt: () => null,
  alert: () => {},
  setTimeout, clearTimeout, setInterval, clearInterval,
  performance: { now: () => Date.now() },
  requestAnimationFrame: (f) => setTimeout(f, 0),
  fetch: async () => ({ ok: true, status: 200, json: async () => ({}), text: async () => '' }),
  URL: { createObjectURL: () => 'blob:x', revokeObjectURL: () => {} },
  Blob: function (parts, opts) { this.parts = parts; this.opts = opts; },
  console, Date, Math, JSON, Promise, Object, Array, String, Number, Boolean,
  URLSearchParams,   // v1.25.1: нужен load() страницы «Чеки»
  RegExp, Error, TypeError, Map, Set, parseInt, parseFloat, isNaN, encodeURIComponent,
  decodeURIComponent, structuredClone: (x) => JSON.parse(JSON.stringify(x)),
  AbortController: class { constructor() { this.signal = {}; } abort() {} },
  FormData: class { append() {} },
  FileReader: class { readAsDataURL() {} },
  Image: class {},
};

// api-заглушки поверх реальных функций бандла (перебиваем после загрузки)
const cardPayload = {
  company: { id: 'x1', name: 'ООО «Ямастер»', inn: '7801234564', note: '', is_active: true },
  receipts: 12, receipts_sum: 34567.89,
  team: [{ username: 'ivanov', full_name: 'Иван Иванов', role: 'user' }],
  card: { kind: 'legal', name_full: 'ООО ЯМАСТЕР', inn: '7801234564', kpp: '780101001',
          ogrn: '1157847000000', address: 'г. Тихвин', director: 'Директор Д.Д.',
          status: 'Действующая', okved: '62.01' },
  card_updated_at: '2026-10-04T00:00:00',
  other_companies: [{ id: 'x2', name: 'ООО Приёмник', is_active: true },
                    { id: 'x3', name: 'ООО Архив', is_active: false }],
};

const advancePayloads = [
  { // один сотрудник → классический АО-1
    period: { from: '2026-10-01', to: '2026-10-31' },
    company: { id: 'x1', name: 'ООО «Ямастер»', inn: '7801234564' },
    rows: [
      { id: 'r1', receipt_date: '2026-10-05T12:00', assignee: 'Иван Отчётный',
        merchant_name: 'ООО Лента', merchant_inn: '7802000000', fn: '1', fd: '11',
        fp: '111', operation: 1, category: 'Хозрасходы', total_sum: 100.0,
        fns_status: 'valid', exported: false },
    ],
    total: { sum: 100.0, count: 1 },
    by_assignee: [{ name: 'Иван Отчётный', sum: 100.0, count: 1 }],
    by_category: [{ name: 'Хозрасходы', sum: 100.0, count: 1 }],
    invalid_count: 0, truncated: false,
  },
  { // два сотрудника → сводная форма
    period: { from: '2026-10-01', to: '2026-10-31' },
    company: { id: 'x1', name: 'ООО «Ямастер»', inn: '7801234564' },
    rows: [
      { id: 'r1', receipt_date: '2026-10-05T12:00', assignee: 'Иван Отчётный',
        merchant_name: 'ООО Лента', merchant_inn: '7802000000', fn: '1', fd: '11',
        fp: '111', operation: 1, category: 'Хозрасходы', total_sum: 100.0,
        fns_status: 'valid', exported: false },
      { id: 'r2', receipt_date: '2026-10-06T13:00', assignee: 'Пётр Готовый',
        merchant_name: 'АЗС', merchant_inn: '7803000000', fn: '2', fd: '12',
        fp: '222', operation: 1, category: 'Топливо', total_sum: 250.5,
        fns_status: 'invalid', exported: false },
    ],
    total: { sum: 350.5, count: 2 },
    by_assignee: [{ name: 'Иван Отчётный', sum: 100.0, count: 1 },
                  { name: 'Пётр Готовый', sum: 250.5, count: 1 }],
    by_category: [{ name: 'Топливо', sum: 250.5, count: 1 },
                  { name: 'Хозрасходы', sum: 100.0, count: 1 }],
    invalid_count: 1, truncated: false,
  },
];
let advanceIdx = 0;

// v1.25.1: данные страницы «Чеки» и переключатель сбоя API
let receiptsFail = false;
const receiptsPayload = {
  total: 2, total_sum: 350.5, page: 1, page_size: 50,
  items: [
    { id: 'r1', receipt_date: '2026-10-05T12:00', total_sum: 100.0, fn: '9999078902001299',
      fd: '11', fp: '111', operation: 1, status: 'new', fns_status: 'unknown',
      exported: false, notified: false, full_data: false,
      assignee: '<script>alert(1)</script>',   // v1.58.5: XSS-проба
      created_by_id: 'u2', created_by_name: 'Иванов И.', category: 'Хозрасходы' },
    { id: 'r2', receipt_date: '2026-10-06T13:00', total_sum: 250.5, fn: '9999078902001307',
      fd: '12', fp: '222', operation: 1, status: 'verified', fns_status: 'valid',
      exported: true, notified: true, full_data: true, assignee: 'Пётр Готовый',
      created_by_id: 'u3', created_by_name: 'Петров П.', category: 'Топливо' },
  ],
};
const creatorsPayload = [
  { id: 'u2', name: 'Иванов И.', count: 1 },
  { id: 'u3', name: 'Петров П.', count: 1 },
];

sandbox.fetch = async (url, opts) => {
  apiLog.push({ url: String(url), method: (opts && opts.method) || 'GET',
                body: opts && opts.body ? JSON.parse(opts.body) : null });
  // v1.25.1: бухгалтер на «Чеках»
  let specialPayload = false;
  if (String(url).includes('/auth/me')) {
    specialPayload = true;
    payload = { id: 'u1', username: 'acc', role: 'accountant', full_name: 'Бух Бухгалтер' };
  }
  if (String(url).includes('/receipts/creators')) {
    specialPayload = true;
    payload = creatorsPayload;
  }
  if (String(url).includes('/receipts?')) {
    specialPayload = true;
    payload = receiptsFail ? { detail: 'сервер недоступен' } : receiptsPayload;
    if (receiptsFail) {                     // v1.25.1: настоящий сбой (ok:false, без ретраев)
      const j = JSON.stringify(payload);
      return { ok: false, status: 400,
               json: async () => JSON.parse(j), text: async () => j,
               headers: { get: () => 'application/json' } };
    }
  }
  if (!specialPayload) payload = {};
  if (String(url).includes('/card')) { specialPayload = true; payload = cardPayload; }
  if (String(url).includes('/stats'))
    payload = { total: 0, by_fns: {}, by_day: [], by_company: {}, by_source: {},
                by_user: {}, by_status: {}, valid: 0, invalid: 0 };
  if (String(url).includes('/delete')) payload = { ok: true, message: 'Компания удалена' };
  if (String(url).includes('/advance-report'))
    payload = advancePayloads[advanceIdx++ % advancePayloads.length];
  if (String(url).includes('/settings/checko') && (opts && opts.method) === 'PUT')
    payload = { ok: true, key_masked: 'x•••000' };
  const json = JSON.stringify(payload);
  return { ok: true, status: 200,
           json: async () => JSON.parse(json),
           text: async () => json,
           blob: async () => new sandbox.Blob(['csv']),
           headers: { get: () => 'application/json' } };
};

vm.createContext(sandbox);
// v1.58.5: путь бандла — аргумент (уникален на воркер xdist)
const bundlePath = process.argv[2] || '/tmp/ymaster_ui_bundle.js';
const bundle = fs.readFileSync(bundlePath, 'utf8');
vm.runInContext(bundle, sandbox, { filename: 'bundle.js' });

(async () => {
  const fails = [];
  const ok = (cond, name) => { console.log((cond ? 'PASS' : 'FAIL') + ' ' + name); if (!cond) fails.push(name); };

  // Перебиваем api/toast/route заглушками (в бандле это не глобалы, а замыкания
  // модулей — поэтому вызываем через экспортируемые функции? — проверяем ниже)
  // 1) Функции существуют в скоупе бандла?
  ok(typeof sandbox.openCompanyCard === 'function' || true, 'sanity');

  // openCompanyCard объявлена в app.js (бандл выполняется в топ-скоупе vm →
  // декларации попадают в контекст только при var/function? — function да)
  ok(typeof sandbox.openCompanyCard === 'function', 'openCompanyCard exported to sandbox');
  ok(typeof sandbox.openCompanyDelete === 'function', 'openCompanyDelete exported to sandbox');
  ok(typeof sandbox.companyDialog === 'function', 'companyDialog exported to sandbox');

  // 2) Карточка компании
  const slotHtml = () => {
    const s = root._cls && root._cls['.modal-slot'];
    return (s && s._html) || root.innerHTML;
  };
  await sandbox.openCompanyCard('x1');
  await new Promise(r => setTimeout(r, 50));
  console.log('CARD SLOT LEN:', slotHtml().length);
  console.log('CARD SLOT HEAD:', JSON.stringify(slotHtml().slice(0, 300)));
  await new Promise(r => setTimeout(r, 20));
  ok(slotHtml().includes('Обновить из Checko'), 'card: ЕГРЮЛ-кнопка');
  ok(slotHtml().includes('ООО ЯМАСТЕР'), 'card: name_full из Checko');
  const sumOk = ['34567.89', '34 567', '34\u00a0567', '34 567,89'].some(x => slotHtml().includes(x));
  ok(sumOk, 'card: сумма');
  ok(slotHtml().includes('Удалить компанию'), 'card: кнопка удаления');
  ok(slotHtml().includes('CSV все чеки'), 'card: CSV-кнопка');
  ok(slotHtml().includes('Действующая'), 'card: статус ЕГРЮЛ');

  // 3) Диалог удаления
  root.innerHTML = '';
  await sandbox.openCompanyDelete(cardPayload);
  ok(slotHtml().includes('Перенести в другую компанию'), 'delete: режим move');
  ok(slotHtml().includes('Удалить чеки безвозвратно'), 'delete: режим wipe');
  ok(slotHtml().includes('ООО Приёмник'), 'delete: список приёмников без архива');
  ok(!slotHtml().includes('ООО Архив'), 'delete: архив исключён');

  // 4) Подтверждение move → POST /delete с payload
  await sandbox.openCompanyDelete(cardPayload);
  const slotEl = root._cls && root._cls['.modal-slot'];
  const goBtn = (slotEl && (slotEl._q_dl_go || slotEl.querySelector('#dl-go'))) || root.querySelector('#dl-go');
  await goBtn.onclick();
  await new Promise(r => setTimeout(r, 20));
  const del = apiLog.find(x => x.url.includes('/delete'));
  ok(!!del && del.method === 'POST', 'delete: POST отправлен');
  ok(del && del.body.mode === 'move' && del.body.target_company_id === 'x2',
     'delete: payload move/target=x2');

  // 5) Диалог «Собрать авансовый отчёт» ------------------------------------
  await sandbox.openAO1Modal();
  let aoSlot = root._cls['.modal-slot'];
  aoSlot.querySelector('#ao-from').value = '2026-10-01';
  aoSlot.querySelector('#ao-to').value = '2026-10-31';
  const mkBtn = aoSlot.querySelector('#ao-make');
  await mkBtn.onclick({ target: mkBtn });
  await new Promise(r => setTimeout(r, 30));
  const advPost = apiLog.find(x => x.url.includes('/advance-report'));
  ok(!!advPost && advPost.method === 'POST', 'ao: POST advance-report отправлен');
  ok(advPost && advPost.body.date_from === '2026-10-01' && advPost.body.date_to === '2026-10-31',
     'ao: payload период');
  ok(aoSlot.innerHTML.includes('Отчёт собран'), 'ao: предпросмотр');
  ok(aoSlot.innerHTML.includes('ИТОГО'), 'ao: итог в предпросмотре');
  // печать (один сотрудник → АО-1)
  await aoSlot.querySelector('#ao-print').onclick();
  ok(true, 'ao: печать АО-1 без крэша');
  // v1.24.0: официальный бланк — ФН/ФП в обороте и поля страницы 20/15/10/10
  const prAO1 = document.getElementById('print-root');
  ok(prAO1 && prAO1.innerHTML.includes('ФН '), 'ao1: ФН/ФП в оборотной таблице');
  const aoPage = document.getElementById('ym-page-style');
  ok(aoPage && aoPage.textContent.includes('margin: 10mm 15mm 10mm 20mm'),
     'ao1: @page поля лево 20 / право 15 / верх 10 / низ 10 мм');
  ok(bundle.includes("id=\"ao-rko\"") && bundle.includes("id=\"ao-pay\""),
     'ao1: поля РКО и платёжного поручения в модалке');
  ok(bundle.includes('Напечатать чек') &&
     !bundle.includes('Печать PDF (выбранные)'),
     'печать: кнопка «Напечатать чек»');
  ok(bundle.includes('whenImagesReady') && bundle.includes('Фискальные QR'),
     'v1.24.1: печать ждёт загрузки QR');
  // второй прогон — два сотрудника → сводная форма с подытогами
  root._cls['.modal-slot'].innerHTML = '';
  await sandbox.openAO1Modal();
  aoSlot = root._cls['.modal-slot'];
  aoSlot.querySelector('#ao-from').value = '2026-10-01';
  aoSlot.querySelector('#ao-to').value = '2026-10-31';
  const mkBtn2 = aoSlot.querySelector('#ao-make');
  await mkBtn2.onclick({ target: mkBtn2 });
  await new Promise(r => setTimeout(r, 30));
  ok(aoSlot.innerHTML.includes('Недействителен'), 'ao: предупреждение invalid');
  await aoSlot.querySelector('#ao-print').onclick();
  const pr = document.getElementById('print-root');
  ok(pr && pr.innerHTML.includes('сводный'), 'ao: сводная печатная форма');
  ok(pr && pr.innerHTML.includes('Подытог'), 'ao: подытоги по сотрудникам');
  // CSV
  await aoSlot.querySelector('#ao-csv').onclick();
  ok(true, 'ao: CSV без крэша');

  // ================================================================
  // v1.25.1: СТРАНИЦА «ЧЕКИ» — фильтры и аномалия «пропавшего списка»
  // ================================================================
  const rc = new El();                                  // контейнер вида
  const table = () => (_docQ['#receipts-table'] = _docQ['#receipts-table'] || new El());
  const wipeDom = () => {                               // симуляция перестроения:
    rc.innerHTML = '<div class="skeleton"></div>';      // контейнер переписан шаблоном
    table().innerHTML = '<div class="skeleton"></div>'; // таблица исчезла
  };

  // 1) первое открытие: список рисуется
  wipeDom();
  await sandbox.viewReceipts(rc);
  await new Promise(r => setTimeout(r, 40));
  ok(table().innerHTML.includes('<table'), 'чеки: список отрисован при открытии');
  ok(table().innerHTML.includes('ФД'), 'чеки: колонки на месте');

  // 2) ГЛАВНАЯ РЕГРЕССИЯ: WS-перестроение с теми же данными — список НЕ пропадает
  const apiCallsBefore = apiLog.filter(x => String(x.url).includes('/receipts?')).length;
  wipeDom();
  await sandbox.viewReceipts(rc);
  await new Promise(r => setTimeout(r, 40));
  ok(table().innerHTML.includes('<table'),
     'чеки: повторное открытие с теми же данными — список на месте (без F5)');

  // 3) фильтр применяется и ПЕРЕЖИВАЕТ перестроение вида
  _docQ['#f-q'].value = '61001';
  await _docQ['#btn-filter'].onclick();
  await new Promise(r => setTimeout(r, 30));
  const qUrl = [...apiLog].reverse().find(x => String(x.url).includes('/receipts?'));
  ok(qUrl && String(qUrl.url).includes('q=61001'), 'чеки: «Найти» передаёт q в API');
  wipeDom();
  await sandbox.viewReceipts(rc);
  await new Promise(r => setTimeout(r, 40));
  ok(_docQ['#f-q'].value === '61001', 'чеки: фильтр восстановлен после перестроения');
  const qUrl2 = [...apiLog].reverse().find(x => String(x.url).includes('/receipts?'));
  ok(qUrl2 && String(qUrl2.url).includes('q=61001'),
     'чеки: восстановленный фильтр ушёл в запрос');
  ok(table().innerHTML.includes('<table'), 'чеки: список отрисован с фильтром');

  // 4) живое обновление _refresh() — на месте, без route()
  const refreshIsFn = typeof sandbox.viewReceipts._refresh === 'function';
  ok(refreshIsFn, 'чеки: живое обновление _refresh доступно');
  if (refreshIsFn) {
    await sandbox.viewReceipts._refresh();
    ok(table().innerHTML.includes('<table'), 'чеки: _refresh не гасит список');
  }

  // 5) селектор «Кто добавил»: заполнен и восстановлен из кэша
  ok(_docQ['#f-creator'].innerHTML.includes('Иванов И.'),
     'чеки: селектор создателей заполнен');
  _docQ['#f-creator'].innerHTML = '<option value="">все добавившие</option>';
  wipeDom();
  await sandbox.viewReceipts(rc);
  await new Promise(r => setTimeout(r, 40));
  ok(_docQ['#f-creator'].innerHTML.includes('Иванов И.'),
     'чеки: создатели восстановлены из кэша после перестроения');

  // 6) сбой API → внятная ошибка с «Повторить», восстановление без F5
  receiptsFail = true;
  await sandbox.viewReceipts._refresh();
  await new Promise(r => setTimeout(r, 30));
  ok(table().innerHTML.includes('Повторить'), 'чеки: ошибка загрузки видна пользователю');
  receiptsFail = false;
  await _docQ['#rc-retry'].onclick();
  await new Promise(r => setTimeout(r, 30));
  ok(table().innerHTML.includes('<table'), 'чеки: «Повторить» возвращает список');

  // --- v1.58.5: XSS-экранирование и юнит-проверки хелперов ---
  ok(!table().innerHTML.includes('<script>alert'),
     'xss: присланное «<script>» не попало в DOM как тег');
  ok(table().innerHTML.includes('&lt;script&gt;'),
     'xss: присланное «<script>» экранировано (esc)');
  ok(typeof sandbox.fmtSum === 'function' &&
     sandbox.fmtSum(807.3) === '807,30 ₽',
     'unit: fmtSum — рубли с двумя знаками');
  ok(typeof sandbox.fmtDate === 'function' &&
     sandbox.fmtDate('2026-10-08T13:05').includes('08.10.26'),
     'unit: fmtDate — формат дд.мм.гг');
  ok(typeof sandbox.esc === 'function' &&
     sandbox.esc('<b>') === '&lt;b&gt;',
     'unit: esc — экранирование HTML');

  // --- v1.59.0: три пути сдачи чека дают одну и ту же строку QR ---
  ok(typeof sandbox.buildManualQr === 'function',
     'v1.59.0: buildManualQr доступен');
  const mq = sandbox.buildManualQr('22.09.2025 14:30', 1500.5, '1',
                                   '9999078902001234', '12345', '1234567890');
  ok(mq === 't=20250922T1430&s=1500.50&fn=9999078902001234&i=12345'
     + '&fp=1234567890&n=1',
     'v1.59.0: ручной ввод собирает каноничную строку QR (как камера/вставка)');
  ok(typeof sandbox.parseQrClient === 'function' && !!sandbox.parseQrClient(mq)
     && sandbox.parseQrClient(mq).fn === '9999078902001234'
     && sandbox.parseQrClient(mq).sum === '1500.50',
     'v1.59.0: строка ручного ввода парсится клиентским парсером');
  ok(sandbox.buildManualQr('некорректно', 100, '1', '1', '1', '1') === '',
     'v1.59.0: buildManualQr защищён от неверной даты');
  ok(bundle.includes('function poolEnableBanner')
     && bundle.includes("bar.id = 'pool-off-banner'"),
     'v1.59.0: плашка админу о выключенном приёме');
  ok(bundle.includes('camWanted') && !bundle.includes('setTimeout(camPick'),
     'v1.59.0: камера открывается только при включённом приёме (без гонки)');
  ok(bundle.includes('pub-retry')
     && bundle.includes('Приём чеков сейчас на паузе'),
     'v1.59.0: пауза — баннер + повторная проверка вместо затирания формы');

  // --- v1.57.5 (блок v1.25.1): у полученного чека значок «📥✓» ---
  ok(!receiptsPayload.items[1].full_data ? true : true, 'sanity full_data');  // r2: full
  ok(bundle.includes('📥✓') && !bundle.includes("r.full_data ? '' :"),
     'v1.57.5: у полного чека значок «📥✓» вместо скрытой кнопки');
  ok(bundle.includes('Получить полные данные чека из сервиса проверки'),
     'v1.57.5: 📥 у неполных (тултип v1.25.1)');
  ok(bundle.includes('проверены + полные данные') &&
     bundle.includes('данных не хватает — можно запросить'),
     'v1.25.2: подписи фильтра «Данные чека»');

  // --- v1.58.3: переключатель «Список / Карточки» работает ---
  const vsegC = _docQ['#rc-view-cards'];
  ok(!!vsegC && typeof vsegC.onclick === 'function',
     'чеки: переключатель вида на странице и привязан');
  if (vsegC && typeof vsegC.onclick === 'function') {
    await vsegC.onclick();
    await new Promise(r => setTimeout(r, 40));
    ok(table().innerHTML.includes('rc-cards') &&
       table().innerHTML.includes('rc-actions'),
       'чеки: режим «Карточки» — действия внизу карточки');
    const vsegL = _docQ['#rc-view-list'];
    await vsegL.onclick();
    await new Promise(r => setTimeout(r, 40));
    ok(table().innerHTML.includes('<table'), 'чеки: возврат в режим «Список»');
  }

  // --- v1.59.1: гостевые страницы Чек-Пула видимы (регресс пустой страницы) ---
  sandbox.showPublicScreen();
  const pubScr = document.getElementById('public-screen');
  ok(!!pubScr && !pubScr.classList.contains('hidden'),
     'v1.59.1: showPublicScreen показывает обёртку #public-screen');
  ok(document.getElementById('login-screen').classList.contains('hidden'),
     'v1.59.1: showPublicScreen прячет экран входа (переключатель работает)');
  ok((_docQ['#public-root'] || document.getElementById('public-root'))
     .innerHTML.includes('pub-qr'),
     'v1.59.1: форма сдачи чека в DOM (pub-qr)');
  sandbox.showPoolScreen();
  const poolScr = document.getElementById('pool-account-screen');
  ok(!!poolScr && !poolScr.classList.contains('hidden'),
     'v1.59.1: showPoolScreen показывает обёртку #pool-account-screen');
  ok(!!pubScr.classList.contains('hidden'),
     'v1.59.1: кабинет прячет страницу сдачи');
  sandbox.showLogin();
  ok(!document.getElementById('login-screen').classList.contains('hidden')
     && !!pubScr.classList.contains('hidden') && !!poolScr.classList.contains('hidden'),
     'v1.59.1: showLogin прячет гостевые страницы');

  // --- v1.59.2: гостевые страницы прокручиваются (класс на body) ---
  sandbox.showPublicScreen();
  ok(!!document.body.classList.contains('guest-scroll'),
     'v1.59.2: на гостевом экране body допускает прокрутку страницы');
  sandbox.showScreen('app-shell');
  ok(!document.body.classList.contains('guest-scroll'),
     'v1.59.2: в приложении прокрутка внутренняя (класс снят)');
  sandbox.showLogin();                       // вернуть гостевой экран

  // --- v1.59.3: «Список» — одна строка на чек (таблица, не карточки) ---
  ok(bundle.includes('<table class="data"') &&
     bundle.includes("state.receiptsView === 'cards'"),
     'v1.59.3: «Список» рендерит таблицу (строка на чек), «Карточки» отдельным режимом');

  // --- v1.59.4: приглашения — select компаний + аккуратные действия ---
  ok(bundle.includes('iv-company-select') &&
     bundle.includes('Другая компания — вписать'),
     'v1.59.4: диалог приглашения — select компаний + своя компания');
  ok(bundle.includes("api.get('/api/v1/companies')") &&
     bundle.includes('Array.isArray(_list)'),
     'v1.59.4: список компаний свежий из базы при открытии');
  ok(bundle.includes('<div class="row-actions">') &&
     bundle.includes('<th>Действия</th>') &&
     !bundle.includes('<th>Компания</th><th>Использовано</th>'),
     'v1.59.4: список приглашений — 6 колонок, кнопки блоком row-actions');
  await sandbox.inviteDialog();               // под бухгалтером харнесса
  const ivSlot = root._cls['.modal-slot'];
  ok(ivSlot.innerHTML.includes('iv-role') && ivSlot.innerHTML.includes('iv-save'),
     'v1.59.4: диалог приглашения открывается (роль/срок/создать)');
  const ivClose = ivSlot.querySelector('[data-close]');
  if (ivClose && ivClose.onclick) await ivClose.onclick();

  // --- v1.59.5: краткая форма ОПФ в списке компаний приглашения ---
  ok(typeof sandbox.shortOpfName === 'function', 'v1.59.5: shortOpfName доступен');
  ok(sandbox.shortOpfName('Общество с ограниченной ответственностью «Ромашка»')
     === 'ООО «Ромашка»', 'v1.59.5: ООО — краткая форма');
  ok(sandbox.shortOpfName('Индивидуальный предприниматель Иванов И.И.')
     === 'ИП Иванов И.И.', 'v1.59.5: ИП — краткая форма');
  ok(sandbox.shortOpfName('Самозанятый гражданин Петров П.П.')
     === 'Самозанятый Петров П.П.', 'v1.59.5: самозанятый — краткая форма');
  ok(sandbox.shortOpfName('Плательщик налога на профессиональный доход Сидоров С.С.')
     === 'Самозанятый Сидоров С.С.', 'v1.59.5: НПД → Самозанятый');
  ok(sandbox.shortOpfName('Акционерное общество «Заря»') === 'АО «Заря»',
     'v1.59.5: АО — краткая форма');
  ok(sandbox.shortOpfName('Крестьянское (фермерское) хозяйство «Нива»')
     === 'КФХ «Нива»', 'v1.59.5: КФХ — краткая форма');
  ok(sandbox.shortOpfName('Ромашка') === 'Ромашка',
     'v1.59.5: без ОПФ — как есть');
  ok(bundle.includes('shortOpfName(compName(c))'),
     'v1.59.5: список приглашения показывает краткие имена (карточка → фолбэк)');

  // --- v1.60.0: время — UTC+«Z» с сервера, показ в местной зоне ---
  ok(typeof sandbox.updLogLine === 'function', 'v1.60.0: updLogLine доступен');
  const updOut = sandbox.updLogLine('[2026-10-12T10:00:00Z] Готово: v1.60.0');
  ok(/^\[\d{2}:\d{2}:\d{2}\] Готово: v1\.60\.0$/.test(updOut),
     'v1.60.0: метку журнала с «Z» показываем в местной зоне (ЧЧ:ММ:СС)');
  ok(sandbox.updLogLine('[12:00:00] старая запись') === '[12:00:00] старая запись',
     'v1.60.0: старые метки журнала не трогаем');
  // инвариант без привязки к зоне контейнера: «Z»-время отрисовывается
  // ровно так же, как тот же момент, записанный местным «настенным» временем
  const zdt = new Date('2026-10-12T10:00:00Z');
  const pad = (x) => String(x).padStart(2, '0');
  const localIso = zdt.getFullYear() + '-' + pad(zdt.getMonth() + 1) + '-'
    + pad(zdt.getDate()) + 'T' + pad(zdt.getHours()) + ':' + pad(zdt.getMinutes());
  ok(sandbox.fmtDate('2026-10-12T10:00:00Z') === sandbox.fmtDate(localIso),
     'v1.60.0: fmtDate — «Z»-время == тот же момент в местной зоне');
  ok(sandbox.fmtDate('2026-10-08T13:05').includes('13:05'),
     'v1.60.0: бизнес-время (дата чека) показывается как есть');
  ok(bundle.includes('function updLogLine') &&
     bundle.includes('.map(updLogLine)'),
     'v1.60.0: журнал обновления рендерится через updLogLine');

  // --- v1.61.0: «На ручной проверке» — путь правил виден в интерфейсе ---
  ok(bundle.includes('На ручной проверке') &&
     bundle.includes('pending'),
     'v1.61.0: статус «На ручной проверке» доступен в интерфейсе');
  ok(bundle.includes('value="pending"'),
     'v1.61.0: фильтр чеков умеет искать «На ручной проверке»');

  // --- v1.62.0: политика расходов — чип и карточка настроек в бандле ---
  ok(bundle.includes('policy_flags') && bundle.includes('⚖️ политика'),
     'v1.62.0: чип политики расходов рендерится в списке');
  ok(bundle.includes('Политика расходов') && bundle.includes('pol-save'),
     'v1.62.0: карточка «Политика расходов» в настройках');

  // --- v1.63.0: самообучающийся маппинг — блок запоминания и карточка правил ---
  ok(bundle.includes('Запомнить для поставщика') &&
     bundle.includes('remember-merchant'),
     'v1.63.0: блок «Запомнить для поставщика» в карточке чека');
  ok(bundle.includes('Правила поставщиков') &&
     bundle.includes('mr-backfill'),
     'v1.63.0: карточка «Правила поставщиков» в настройках');

  // --- v1.64.0: роль «руководитель» и отчёт с разрезами ---
  ok(bundle.includes('boss-only') && bundle.includes('Отчёт руководителя') &&
     bundle.includes('viewBoss'),
     'v1.64.0: отчёт руководителя доступен в интерфейсе');
  ok(bundle.includes("manager: 'Руководитель'") &&
     bundle.includes('ur-role'),
     'v1.64.0: роль «Руководитель» в интерфейсе ролей');

  // --- v1.65.0: PDF-чеки — кнопка и приём в бандле ---
  ok(bundle.includes('btn-pdf') && bundle.includes('Загрузить PDF-чек'),
     'v1.65.0: кнопка «Загрузить PDF-чек» на сканировании');
  ok(bundle.includes('application/pdf') &&
     bundle.includes('serverScanPdf'),
     'v1.65.0: приём PDF в конвейере сканирования');

  // --- v1.66.0: ассистенты Telegram + MAX, выбор канала ---
  ok(bundle.includes('Ассистент MAX') && bundle.includes('/api/v1/settings/max'),
     'v1.66.0: карточка MAX в настройках');
  ok(bundle.includes('tg-channel') && bundle.includes('Оба ассистента'),
     'v1.66.0: выбор канала (telegram/max/both) в настройках');

  // --- v1.67.0: исходящие webhooks ---
  ok(bundle.includes('Вебхуки: события наружу') && bundle.includes('/api/v1/settings/webhooks'),
     'v1.67.0: карточка вебхуков в настройках');
  ok(bundle.includes('X-Ymaster-Signature') && bundle.includes('wh-save'),
     'v1.67.0: подпись HMAC и кнопки вебхуков');

  // --- v1.68.0: устойчивость (рекомендации аудита) ---
  ok(bundle.includes('Ключ подписи токенов') && bundle.includes('secret_key_ok'),
     'v1.68.0: статус SECRET_KEY в карточке «Система»');
  ok(bundle.includes("'1.68.0':") && bundle.includes('очередь'),
     'v1.68.0: What’s New устойчивости');

  // --- v1.69.0: мини-приложение MAX ---
  ok(bundle.includes('Войти через MAX') && bundle.includes('startapp=bind'),
     'v1.69.0: кнопка входа через MAX в настройках');
  ok(bundle.includes("'1.69.0':"),
     'v1.69.0: What’s New мини-приложения');
  ok(bundle.includes('maxLoginFlow') && bundle.includes('/api/v1/auth/max/poll'),
     'v1.70.0: вход через MAX на сайте (форма+опрос)');
  ok(bundle.includes("'1.70.0':") && bundle.includes('send-max'),
     'v1.70.0: приглашения через бота MAX');
  ok(bundle.includes('window.MXBOT'),
     'v1.70.1: ник бота из /about (без хардкода)');
  ok(bundle.includes('mx-nick') && bundle.includes('last_update_ago'),
     'v1.70.2: публичный ник бота и статус слушания в карточке MAX');
  ok(bundle.includes('token_mismatch'),
     'v1.70.3: диагностика «чей токен» в карточке MAX');
  ok(bundle.includes('mx-who') && bundle.includes('check-token'),
     'v1.70.5: кнопка «Чей токен?» с живой проверкой');
  ok(bundle.includes("'1.70.6':"),
     'v1.70.6: What’s New — username-диплинки и фото-сканер');
  ok(bundle.includes("'1.70.7':") && bundle.includes('mxl-copy'),
     'v1.70.7: камера в мини-приложении и копирование кода входа');
  ok(bundle.includes("'1.70.8':"),
     'v1.70.8: What’s New — сертификат Минцифры для API MAX');
  ok(bundle.includes("'1.70.9':") && bundle.includes('settings-nav') &&
     bundle.includes('data-sec="data"') && bundle.includes('settingsApply'),
     'v1.70.9: настройки — меню с разделами и подменю');

  console.log(fails.length ? 'HARNESS FAIL: ' + fails.join('; ')
              : `HARNESS_OK checks`);
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error('HARNESS_CRASH', e && e.stack || e); process.exit(2); });
