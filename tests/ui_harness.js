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
      toggle: (c) => this.classList._s.has(c) ? this.classList._s.delete(c) : this.classList._s.add(c),
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
      exported: false, notified: false, full_data: false, assignee: 'Иван Отчётный',
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
const bundle = fs.readFileSync('/tmp/ymaster_ui_bundle.js', 'utf8');
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

  console.log(fails.length ? 'HARNESS FAIL: ' + fails.join('; ')
              : `HARNESS_OK checks`);
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error('HARNESS_CRASH', e && e.stack || e); process.exit(2); });
