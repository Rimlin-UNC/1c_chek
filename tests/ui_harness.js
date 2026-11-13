// ======================================================================
// Ямастер Чек — UI-harness v1.13.0/v1.14.0 (Node vm DOM-песочница): карточка
// компании (ЕГРЮЛ/Checko), диалог умного удаления (move/wipe).
// Запуск: node tests/ui_harness.js  (бандл строит tests/test_v*.py)
// ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
// ======================================================================
// ======================================================================
// UI-harness v1.13.0/v1.14.0: карточка компании + диалог удаления (DOM-песочница)
// ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
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
  // v1.70.101: обработчик запоминается — сценарий может вызвать его сам (делегирование кликов)
  addEventListener(type, fn) { (this._ev = this._ev || {})[type] = fn; }
  contains() { return true; }
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
  crypto: { randomUUID: () => 'abc00000-0000-4000-8000-000000000000' },
  document, window: { addEventListener: () => {}, dispatchEvent: () => {}, print: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }), location: { reload() {}, pathname: '/' } },
  CustomEvent: class { constructor(type, init = {}) { this.type = type; this.detail = init.detail; } },
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
      { id: 'r1', company_id: 'x1', receipt_date: '2026-10-05T12:00', assignee: 'Иван Отчётный',
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
      { id: 'r1', company_id: 'x1', receipt_date: '2026-10-05T12:00', assignee: 'Иван Отчётный',
        merchant_name: 'ООО Лента', merchant_inn: '7802000000', fn: '1', fd: '11',
        fp: '111', operation: 1, category: 'Хозрасходы', total_sum: 100.0,
        fns_status: 'valid', exported: false },
      { id: 'r2', company_id: 'x1', receipt_date: '2026-10-06T13:00', assignee: 'Пётр Готовый',
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

// v1.70.87 (#25): «шлюзы» ответов API. Ответ на адрес, содержащий ключ шлюза,
// приходит только после вызова функции, которую вернул gate(): раздел ждёт
// ответа, а пользователь тем временем уходит в другой раздел — гонка отрисовки
// воспроизводится детерминированно. fail — ответ «500», как при сбое сервера.
const gates = new Map();
// v1.70.108: sticky — держать ВСЕ совпавшие запросы до отпускания, а не только первый
// (второй запрос того же адреса от фонового route(true) иначе проходил мимо шлюза)
function gate(key, fail = false, sticky = false) {
  let release;
  const opened = new Promise((resolve) => { release = resolve; });
  gates.set(key, { opened, fail, sticky });
  return () => { if (sticky) gates.delete(key); release(); };
}

sandbox.fetch = async (url, opts) => {
  apiLog.push({ url: String(url), method: (opts && opts.method) || 'GET',
                body: opts && opts.body ? JSON.parse(opts.body) : null,
                headers: (opts && opts.headers) || {} });   // v1.70.100: заголовок выбранного клиента
  const held = [...gates.keys()].find((key) => String(url).includes(key));
  if (held) {
    const { opened, fail, sticky } = gates.get(held);
    if (!sticky) gates.delete(held);
    await opened;
    if (fail) {
      const j = JSON.stringify({ detail: 'сбой сервера (стенд)' });
      return { ok: false, status: 500, json: async () => JSON.parse(j), text: async () => j,
               headers: { get: () => 'application/json' } };
    }
  }
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
  // v1.70.95: первичка — состояние распознавания сканов и сохранение ручного ввода (T-61)
  if (String(url).endsWith('/api/v1/primary/ocr')) {
    specialPayload = true;
    payload = { ready: false, engine: '', missing: ['rus', 'eng'], reason: 'не установлен модуль tesserocr', last: {} };
  }
  if (String(url).endsWith('/api/v1/primary/confirm')) {
    specialPayload = true;
    payload = { result: 'created', document: { kind: 'УПД', doc_number: 'Б-1', doc_date: '2026-10-20' }, issues: [] };
  }
  // v1.70.97: «что нужно от клиента» — запросы, журнал, поиск недостающего (T-58)
  if (String(url).includes('/api/v1/doc-requests/journal')) {
    specialPayload = true;
    payload = { items: [{ id: 'e1', kind: 'created', slot: '', channel: '', recipient: '', actor: 'программа', ok: true,
                          at: '2026-11-01T06:00:00', text: 'ожидается по истории' }] };
  } else if (String(url).includes('/api/v1/doc-requests/detect')) {
    specialPayload = true;
    payload = { created: 1, closed: 0 };
  } else if (String(url).includes('/api/v1/doc-requests?')) {
    specialPayload = true;
    payload = { company: { id: 'c1', name: 'Ромашка' }, period: '2026-10', period_title: 'октябрь 2026',
                deadline_human: '5 ноября 2026, 18:00', overdue: false,
                settings: { enabled: false, remind_days: [3, 1, 0], channels: [], roles: ['manager'] }, channels: {},
                items: [{ id: 'r1', kind: 'expected', title: 'Документ от ООО «Арендодатель» за октябрь 2026',
                          amount: 51000, status: 'open', assignee: '' }],
                summary: { open: 1, received: 0, cancelled: 0 }, schedule: [], discipline: { share: null } };
  }
  // v1.70.98: банковская выписка — платёж без документов и итоги (T-45)
  if (String(url).includes('/api/v1/bank?')) {
    specialPayload = true;
    payload = { company: { id: 'c1', name: 'Ромашка' }, status: 'open', statements: [], staff: [],
                operations: [{ id: 'op1', date: '2026-10-12', number: '14', amount: 15000, kind: 'transfer', card_tail: '',
                               card_owner: '', counterparty_name: 'ООО «Песок»', counterparty_inn: '7702345678',
                               purpose: 'Оплата по счёту № 45', status: 'open', match_note: '', decided_by: '' }],
                totals: { open: { count: 1, amount: 15000 }, matched: { count: 2, amount: 12430 }, ignored: { count: 0, amount: 0 } },
                reimburse: { pending_total: 1700, pending_count: 1 }, settings: { retention_days: 1095, cards: [] } };
  }
  // v1.70.99 (ADR-29): банк по API — подключение Точки (секретов в ответе нет)
  if (String(url).includes('/api/v1/bank/connection')) {
    specialPayload = true;
    payload = { banks: [{ id: 'tochka', title: 'Точка Банк' }], bank: 'tochka', method: 'oauth', status: 'active',
                client_id: 'app-synthetic', secret_set: true, jwt_set: false, server_app: false,
                redirect_uri: 'https://chek.ymaster.ru/api/v1/bank/tochka/callback', start_date: '2026-10-01',
                accounts: [{ tail: '0001', currency: 'RUB', loaded: true }], synced_until: '2026-11-04',
                last_sync_at: '2026-11-04T09:05:00', last_error: '', pending: 0, awaiting: false,
                last_result: { ok: true, operations: 9, saved: 3, matched: 2, open: 1 },
                // v1.70.102 (ADR-32): у компании два банка; ВТБ — без открытого API, выписка файлом
                connections: [{ bank: 'tochka', bank_title: 'Точка Банк', status: 'active', accounts: ['0001'], synced_until: '2026-11-04' },
                              { bank: 'tbank', bank_title: 'Т-Банк', status: 'active', accounts: ['0002'], synced_until: '2026-11-04' }],
                file_banks: [{ title: 'ВТБ', note: 'по договору' }] };
  }
  // v1.70.100 (T-50): рабочее место аутсорсера — сводка по клиентам за отчётный месяц
  if (String(url).startsWith('/api/v1/clients') && !String(url).includes('/access/')) {
    specialPayload = true;
    payload = { period: '2026-10', period_title: 'Октябрь 2026', periods: [{ period: '2026-10', title: 'Октябрь 2026' }],
                totals: { companies: 2, bad: 1, warn: 0, ok: 1, overdue: 2, open: 2, escalated: 0, fns_bad: 1,
                          upd_errors: 0, bank_open: 1, to_export: 3, upd_to_export: 0 },
                companies: [
                  { id: 'c2', name: 'ООО «Клиент»', inn: '7702000002', home: false, level: 'bad', score: 15,
                    receipts: { count: 5, sum: 12000, attention: 0, fns_bad: 1, policy: 0, to_export: 3 },
                    primary: { errors: 0, to_export: 0 },
                    docreq: { open: 2, overdue: 2, escalated: 0, deadline: '2026-11-05T18:00:00+03:00', days_left: -1 },
                    bank: { open: 1, status: 'active', last_sync_at: '2026-11-04T07:05:00Z', last_error: '' } },
                  { id: 'c1', name: 'Ромашка', inn: '7701000001', home: true, level: 'ok', score: 0,
                    receipts: { count: 0, sum: 0, attention: 0, fns_bad: 0, policy: 0, to_export: 0 },
                    primary: { errors: 0, to_export: 0 },
                    docreq: { open: 0, overdue: 0, escalated: 0, deadline: '2026-11-05T18:00:00+03:00', days_left: 2 },
                    bank: { open: 0, status: 'none', last_sync_at: '', last_error: '' } }] };
  }
  // v1.70.101 (T-69): понимание чеков — позиции с категориями, справочник, исправление, профиль и память
  if (String(url).endsWith('/api/v1/receipts/r-goods')) {
    specialPayload = true;
    payload = { id: 'r-goods', fn: '9999078900001234', fd: '1201', fp: '3399001122', total_sum: 1740, operation: 1,
                receipt_date: '2026-11-04T12:10:00', status: 'verified', fns_status: 'valid', exported: false,
                created_by_id: 'u1', qr_data: 't=20261104T1210&s=1740.00', category: '', merchant_name: 'Строймаркет',
                items: [
                  { id: 'i1', name: 'ЦЕМЕНТ М500 Д0 50КГ', display_name: 'Цемент М500 Д0, 50 кг', pack: '50 кг',
                    quantity: 2, price: 620, total: 1240, category: 'build_base', category_title: 'Стройматериалы',
                    category_icon: '🏗', category_source: 'dictionary', category_source_title: 'словарь',
                    category_note: 'по слову «цемент»', category_certain: true },
                  { id: 'i2', name: 'Смесь Абвгд 25кг', display_name: 'Смесь Абвгд, 25 кг', pack: '25 кг', quantity: 1,
                    price: 500, total: 500, category: 'build_base', category_title: 'Стройматериалы', category_icon: '🏗',
                    category_source: 'context', category_source_title: 'по чеку и продавцу',
                    category_note: 'по другим товарам чека', category_certain: false }],
                goods: { categories: [{ category: 'build_base', title: 'Стройматериалы', icon: '🏗', count: 2, sum: 1740 }],
                         article_hint: { article: 'Материалы', share: 1, reason: 'по товарам чека: 100 % суммы' } } };
  }
  if (String(url).endsWith('/api/v1/goods/catalog')) {
    specialPayload = true;
    payload = { categories: [{ code: 'build_base', title: 'Стройматериалы', group: 'Строительные материалы и инструмент', icon: '🏗' },
                             { code: 'build_finish', title: 'Отделочные материалы', group: 'Строительные материалы и инструмент', icon: '🎨' }] };
  }
  if (String(url).includes('/api/v1/goods/items/') && (opts && opts.method) === 'PUT') {
    specialPayload = true;
    payload = { ok: true, message: '«Смесь Абвгд, 25 кг» — Отделочные материалы; запомнено для компании', remembered: true,
                applied: 0, item: { category: 'build_finish', category_title: 'Отделочные материалы', category_icon: '🎨',
                                    category_source: 'manual', category_certain: true } };
  }
  if (String(url).startsWith('/api/v1/settings/policy')) {
    specialPayload = true;
    payload = { company_id: 'c1', company_name: 'Ромашка', limit_per_check: null, limit_per_month: null, forbidden_categories: [],
                forbidden_goods: ['Алкогольные напитки'], goods_groups: ['Алкогольные напитки', 'Продукты питания'],
                goods_profile: 'construction', goods_own: false, flagged: 0,
                goods_profiles: [{ code: 'general', title: 'Общий', hint: 'как раньше', forbidden: [] },
                                 { code: 'construction', title: 'Стройка и ремонт', hint: 'стройматериалы — «Материалы»', forbidden: [] }] };
  }
  if (String(url).startsWith('/api/v1/goods/memory')) {
    specialPayload = true;
    payload = { company_id: 'c1', items: [{ id: 'm1', name_key: 'смесь абвгд', sample_name: 'Смесь Абвгд, 25 кг',
                                            category: 'build_finish', category_title: 'Отделочные материалы', icon: '🎨', hits: 2 }] };
  }
  // v1.70.96: первичка — проверка выгрузки в 1С (T-60)
  if (String(url).endsWith('/api/v1/primary/export/check')) {
    specialPayload = true;
    payload = { count: 1, skipped: [], already_exported: 0, errors_excluded: 1, limit: 500, format: 'upd' };
  }
  // v1.70.94: первичка — реестр электронных УПД (T-53)
  if (String(url).includes('/api/v1/primary?')) {
    specialPayload = true;
    payload = { items: [{ id: 'p1', kind: 'УПД', doc_number: 'УТ-15', doc_date: '2026-03-10', correction_number: '',
                          seller: { name: 'ООО «СтройБаза»', inn: '5001007326', kpp: '500101001' },
                          total_with_vat: 7930, vat_total: 1430, status: 'warnings', errors: 0, warnings: 1 }],
                total: 1, summary: { ok: 0, warnings: 1, errors: 0 } };
  }
  if (!specialPayload) payload = {};
  if (String(url).includes('/card')) { specialPayload = true; payload = cardPayload; }
  // v1.70.91: полный чек для формы правки — в строке списка позиций нет (только items_count)
  if (String(url).endsWith('/api/v1/receipts/r-edit') && !(opts && opts.method && opts.method !== 'GET')) {
    payload = { id: 'r-edit', fn: '7281440701327430', fd: '11', fp: '22', total_sum: 100, operation: 1,
                receipt_date: '2026-11-01T10:00:00', exported: false, merchant_name: 'Строймаркет', cashier: '',
                items: [{ name: 'Цемент', quantity: 1, price: 60, total: 60 },
                        { name: 'Песок', quantity: 1, price: 40, total: 40 }],
                unified_fill: { fields: { cashier: 'Кассир Пулов' } } };
  }
  if (String(url).includes('/stats'))
    payload = { total: 0, by_fns: {}, by_day: [], by_company: {}, by_source: {},
                by_user: {}, by_status: {}, valid: 0, invalid: 0 };
  if (String(url).includes('/delete')) payload = { ok: true, message: 'Компания удалена' };
  if (String(url).includes('/advance-report/number')) payload = { number: 'АО-2026-10-01' };
  else if (String(url).includes('/advance-report'))
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
  // v1.70.99 (ADR-29): выбор банка и доступ по API — в карточке компании
  ok(slotHtml().includes('id="cc-bank"'), 'v1.70.99: в карточке компании есть блок «Банк по API»');
  ok(apiLog.some((c) => c.url === '/api/v1/bank/connection?company_id=x1'),
     'v1.70.99: карточка запросила подключение банка именно этой компании');

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
  aoSlot.querySelector('#ao-date').value = '2026-10-31';
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
  ok(bundle.includes('whenImagesReady') && bundle.includes('/qr.png'),
     'v1.24.1: печать ждёт загрузки QR');
  // второй прогон — два сотрудника → сводная форма с подытогами
  root._cls['.modal-slot'].innerHTML = '';
  await sandbox.openAO1Modal();
  aoSlot = root._cls['.modal-slot'];
  aoSlot.querySelector('#ao-from').value = '2026-10-01';
  aoSlot.querySelector('#ao-to').value = '2026-10-31';
  aoSlot.querySelector('#ao-date').value = '2026-10-31';
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
  ok(bundle.includes("'1.70.10':") &&
     bundle.includes("localStorage.setItem('settings_all', '')") &&
     bundle.includes("snav.scrollIntoView"),
     'v1.70.10: возврат из «Все настройки» всегда виден');
  ok(bundle.includes("'1.70.12':") && bundle.includes('tg-apibase'),
     'v1.70.12: Telegram — сменный адрес API (мост/шлюз)');
  // v1.70.13 ставил в карточку Telegram кнопку-пресет; с v1.70.28 управление
  // живёт в собственном блоке, поэтому проверяем только след в истории.
  ok(bundle.includes("'1.70.13':") && bundle.includes('deploy/adguard-vpn'),
     'v1.70.13: пресет AdGuard VPN для Telegram (с 1.70.28 — отдельный блок)');
  ok(bundle.includes("'1.70.14':") && bundle.includes('tg-ping') &&
     bundle.includes('settings/telegram/ping'),
     'v1.70.14: пинг пути Telegram в карточке (3 запроса, среднее)');
  ok(bundle.includes('/telegram/adguard/setup') && bundle.includes('ag-setup-log') &&
     bundle.includes('adguard/setup/preflight') && bundle.includes("'1.70.15':"),
     'v1.70.15: автоустановка AdGuard из карточки (ссылка входа + живой журнал)');
  ok(bundle.includes("'1.70.16':") && bundle.includes('ag-cmd') &&
     bundle.includes('ag-copy-cmd') && bundle.includes('ag-recheck') &&
     bundle.includes('one_time_cmd') && bundle.includes('ymaster-adguard-setup') &&
     bundle.includes('/etc/sudoers.d/ymaster-adguard'),
     'v1.70.16: мастер AdGuard без пароля — одна команда для терминала и «Проверить снова»');
  // Пароль окна обновления (своя механика, v1.17.0) остаётся — проверяем,
  // что из панели AdGuard исчезли ЕЁ идентификаторы пароля.
  ok(!bundle.includes('ag-sudo-pw') && !bundle.includes('ag-sudo-row') &&
     !bundle.includes('agNeedPassword') &&
     !/agLaunch\([^)]*sudo_password/.test(bundle),
     'v1.70.16: поле пароля сервера (sudo) из панели AdGuard удалено');
  ok(bundle.includes("'1.70.17':") && bundle.includes('refspec') &&
     bundle.includes('Источник обновлений') &&
     bundle.includes('ambiguous argument'),
     'v1.70.17: обновление на другую ветку — fetch явным refspec (без «ambiguous argument»)');
  ok(bundle.includes("'1.70.18':") && bundle.includes('placeholder="main"') &&
     !bundle.includes('placeholder="arena/01a0caaa-1c-chek"'),
     'v1.70.18: канал обновлений — main (подсказка поля «Ветка»)');
  ok(bundle.includes("'1.70.19':") && bundle.includes('OPT_BRANCH') &&
     bundle.includes('deploy.conf') && bundle.includes('Ветка задана явно'),
     'v1.70.19: deploy.sh — приоритет --branch= > BRANCH= > deploy.conf > main');
  ok(bundle.includes("'1.70.20':") && bundle.includes('unbound variable') &&
     bundle.includes('install-rule') && bundle.includes('причина'),
     'v1.70.20: помощник AdGuard ставится (install-rule), причина отказа видна в логе');
  ok(bundle.includes("'1.70.21':") && bundle.includes('двух воркеров') &&
     bundle.includes('сирот') && bundle.includes('причина'),
     'v1.70.21: атомарный старт мастера, отмена убивает группу, причина таймаута видна');
  ok(bundle.includes("'1.70.22':") &&
     bundle.includes('помечается прерванным') && bundle.includes('pipefail') &&
     bundle.includes('последней команде'),
     'v1.70.22: панель не застревает после рестарта, отказ curl не выглядит успехом');
  ok(bundle.includes("'1.70.23':") && bundle.includes('.nosymlink') &&
     bundle.includes('ExecStart') && bundle.includes('tg-health.sh'),
     'v1.70.23: установщику AdGuard передаётся -a y, ссылка /usr/local/bin создаётся безусловно');
  // v1.70.24: SOCKS5 → TUN + selective. Прокси в карточке больше не
  // подставляется (в TUN нет слушателя 1080), а шаг режима его очищает.
  ok(bundle.includes("'1.70.24':") && bundle.includes('selective') &&
     bundle.includes('socks5h'),
     'v1.70.24: AdGuard переведён на TUN + selective — через VPN только Telegram');
  ok(!bundle.includes("prx.value = 'socks5://127.0.0.1:1080'"),
     'v1.70.24: после установки прокси socks5://127.0.0.1:1080 не подставляется');
  // v1.70.25: одна кнопка-переключатель (зелёная = работает, красная = выключен)
  ok(bundle.includes("'1.70.25':") && bundle.includes('agPaint') &&
     bundle.includes('agTurnOff') && bundle.includes("action: 'off'"),
     'v1.70.25: AdGuard VPN — кнопка-переключатель включить/выключить');
  ok(bundle.includes('agVpnOn') && bundle.includes("sw.setAttribute('data-pos',") &&
     bundle.includes("busy ? 'mid'"),
     'v1.70.25: состояние берётся с сервера (с 1.70.28 — положение переключателя)');
  // v1.70.26: успех доказывается по факту, а не по коду возврата. Режим
  // исключений перечитывается (general = смысл списка наоборот), поддержка TUN
  // проверяется до установки CLI и до ссылки входа.
  ok(bundle.includes("'1.70.26':") && bundle.includes('/dev/net/tun') &&
     bundle.includes('selective'),
     'v1.70.26: режим «через VPN только Telegram» перечитывается, а не предполагается');
  ok(bundle.includes('1080') && bundle.includes('КРОМЕ Telegram'),
     'v1.70.26: владельцу объяснены инверсия general-режима и остаточный SOCKS-порт');
  // v1.70.27: проверки вне документации AdGuard больше не прерывают установку —
  // только предупреждения. Фатальны коды возврата документированных команд.
  ok(bundle.includes("'1.70.27':") && bundle.includes('документаци') &&
     bundle.includes('60 секунд'),
     'v1.70.27: AdGuard VPN — документированная последовательность, ожидание туннеля 60 с');
  ok(bundle.includes('предупреждени') && bundle.includes('selective'),
     'v1.70.27: расхождение показаний CLI — предупреждение, а не отказ');

  // v1.70.28: AdGuard VPN — собственный блок в «Устройство и служебное» с
  // трёхпозиционным переключателем; из карточки Telegram-бота удалён. Ссылка
  // регистрации приходит В БЛОК и не прячется при остановке задания: условие
  // «login_url && running» и было причиной «ссылка не пришла».
  ok(bundle.includes("'1.70.28':") && bundle.includes('st-adguard') &&
     bundle.includes('data-sec="device"') && bundle.includes('ag-switch') &&
     bundle.includes('ag-sw-off') && bundle.includes('ag-sw-on') &&
     bundle.includes('ag-sw-knob'),
     'v1.70.28: собственный блок AdGuard VPN с трёхпозиционным переключателем');
  ok(!bundle.includes('tg-adguard'),
     'v1.70.28: из карточки Telegram-бота AdGuard VPN удалён');
  ok(bundle.includes('ag-link-box') &&
     bundle.includes('Ссылка для регистрации сервера') &&
     !/st\.login_url && st\.running/.test(bundle),
     'v1.70.28: ссылка регистрации приходит в блок и не скрывается при остановке');
  ok(bundle.includes('ag-sw-left') && bundle.includes('ag-sw-right') &&
     bundle.includes("busy ? 'mid' : (known ? (agVpnOn ? 'right' : 'left') : 'mid')"),
     'v1.70.28: влево — выключено, вправо — включено, середина — неизвестно/идёт');

  // v1.70.29: «вход уже выполнен» — не ошибка (CLI на привязанном аккаунте
  // может вернуть ненулевой код); обе ловушки ручной инструкции описаны.
  ok(bundle.includes("'1.70.29':") && bundle.includes('вход уже выполнен') &&
     bundle.includes('Unit file does not exist'),
     'v1.70.29: привязанный аккаунт не останавливает мастер; ловушки инструкции закрыты');
  // Сам заполнитель в бандле остаётся — но только внутри объяснения ловушки
  // («что нового»). Отсутствие его в КОМАНДАХ инструкции проверяет
  // tests/test_v17029.py::test_no_placeholder_domain_command по README.
  ok(bundle.includes('заполнитель') && bundle.includes('это не команда'),
     'v1.70.29: владельцу объяснено, что заполнитель в инструкции — не команда');

  // v1.70.30: ссылка входа со сменяемого домена AdGuard принимается — ответ
  // разработчика в #188: в странах, блокирующих VPN, это штатный механизм.
  // Белый список доменов молча отбрасывал рабочую ссылку.
  ok(bundle.includes("'1.70.30':") && bundle.includes('guidedeficit.blog') &&
     bundle.includes('сменяемые домены'),
     'v1.70.30: сменяемые домены авторизации AdGuard — штатно, ссылка не отбрасывается');
  ok(bundle.includes('set-protocol http2') && bundle.includes('#176') &&
     bundle.includes('Unable to fetch locations'),
     'v1.70.30: отказ туннеля объяснён журналом и документированными причинами');

  // v1.70.31: отказ туннеля откатывается — сервер не остаётся без сети, а
  // настоящая причина (CLI v1.7.12, TUN auto, собственный трафик в tun0)
  // названа по открытому обращению AdGuard #161.
  ok(bundle.includes("'1.70.31':") && bundle.includes('#161') &&
     bundle.includes('Failed to start the VPN service in the background'),
     'v1.70.31: причина отказа туннеля названа по обращению AdGuard #161');
  ok(bundle.includes('disconnect') && bundle.includes('чёрной дырой'),
     'v1.70.31: отказ туннеля откатывается, сервер не остаётся без сети');

  // v1.70.32: вёрстка и дизайн — мелкий текст читаем (WCAG AA), остатки
  // тёмной темы убраны, необъявленные токены не ломают правила молча.
  ok(bundle.includes("'1.70.32':") && bundle.includes('1.69:1') &&
     bundle.includes('#fca5a5') && bundle.includes('WCAG AA'),
     'v1.70.32: нечитаемый мелкий текст исправлен, измерение в описании есть');
  ok(bundle.includes('--ok-text') && bundle.includes('--bad-text') &&
     bundle.includes('ui_journal.md'),
     'v1.70.32: текст-токены и журнал интерфейса');

  // v1.70.33: вёрстка — валидные форматы и структура под устройства.
  // Разметку целиком проверяет tests/test_v17033.py и внешний html-validate;
  // здесь — то, что видно в клиентском коде: явные type у кнопок шаблонов,
  // последствия превращения #user-chip в нативную кнопку и описание выпуска.
  const btnTags = bundle.match(/<button\b[^>]*>/g) || [];
  const btnNoType = btnTags.filter(t => !/\btype\s*=/.test(t));
  ok(btnTags.length > 250 && btnNoType.length === 0,
     `v1.70.33: все ${btnTags.length} кнопок шаблонов с явным type ` +
     `(без type: ${btnNoType.length}) — иначе кнопка в форме означает submit`);
  ok(bundle.includes("chip.disabled = !chipActive") &&
     !bundle.includes('chip.onkeydown') &&
     !bundle.includes("closest('#btn-logout')"),
     'v1.70.33: #user-chip — нативная кнопка; убран двойной keydown и мёртвая ветка');
  ok(bundle.includes(".user-area"),
     'v1.70.33: «Выйти» вынесена из переключателя профиля в соседнюю обёртку');
  ok(bundle.includes("'1.70.33':") && bundle.includes('1.4.11') &&
     bundle.includes('Вложенные интерактивные элементы невалидны'),
     'v1.70.33: выпуск описан, порог контраста кольца фокуса назван');

  // v1.70.34: кнопок чёрного цвета в программе нет. Основная кнопка была
  // графитовой (#333333) из-за остатка отменённой в v1.25.0 тёмной темы:
  // правило стояло ниже фирменного и перебивало его.
  ok(!bundle.includes("'1.70.34':") === false &&
     bundle.includes('#333333') && bundle.includes('графит'),
     'v1.70.34: выпуск описан, причина чёрной кнопки названа');
  ok(bundle.includes('#1a1a1a') && bundle.includes('6.66:1'),
     'v1.70.34: тёмный текст на корпоративном оранжевом и его контраст указаны');

  // v1.70.35: текст на сайте читаем — цвета графиков и шаблонов переведены на
  // токены темы, остатки тёмной темы в charts.js убраны.
  // Проверяется именно модуль графиков, а не весь бандл: описание выпуска в
  // WHATS_NEW законно цитирует прежние значения, иначе проверка ловила бы свой
  // же текст (та же ошибка, что с литералами в комментариях CSS).
  const chartsFrom = bundle.indexOf('лёгкие SVG-графики');
  const chartsTo = bundle.indexOf('Ямастер Чек —', chartsFrom + 10);
  const charts = bundle.slice(chartsFrom, chartsTo > chartsFrom ? chartsTo : undefined);
  ok(chartsFrom > 0 && charts.length > 500,
     'v1.70.35: модуль графиков найден в бандле (иначе проверка пуста)');
  ok(!charts.includes('#e8ecf8') && !charts.includes('#6b7494') &&
     !charts.includes('rgba(255,255,255,.0') && !charts.includes('#6366f1'),
     'v1.70.35: в графиках не осталось цветов отменённой тёмной темы');
  ok(bundle.includes('chart-center') && bundle.includes('chart-grid') &&
     bundle.includes('#4b0082') && bundle.includes('#2e8b57'),
     'v1.70.35: цвета графиков вынесены в классы CSS, палитра серий фирменная');
  ok(bundle.includes("var(--ok-text)") && !/color:\s*var\(--ok\)/.test(bundle),
     'v1.70.35: зелёный как текст — через --ok-text (4.25:1 у --ok не проходит)');
  ok(bundle.includes('1.18:1') && bundle.includes('1.67:1') && bundle.includes('stylelint'),
     'v1.70.35: в описании выпуска названы измеренные значения и валидатор');

  // v1.70.87 (#25): поздний ответ раздела не подменяет открытый раздел.
  // Дашборд ждёт /dashboard/stats, пользователь уходит в «Чеки», ответ приходит
  // позже — в #view-container остаётся реестр. Проверяется настоящий route()
  // и настоящие разделы бандла, а не их копии.
  {
    // route() останавливает автообновление мониторинга OpenStack и знает раздел
    // Supabase-демо — модули osm.js и supabase_demo.js в бандл стенда не входят,
    // вместо них заглушки
    if (typeof sandbox.localServerStop !== 'function') sandbox.localServerStop = () => {};
    if (typeof sandbox.osmStop !== 'function') sandbox.osmStop = () => {};
    if (typeof sandbox.viewSupabaseDemo !== 'function') sandbox.viewSupabaseDemo = async () => {};
    const vc = document.querySelector('#view-container');
    const tick = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));
    // v1.70.108: шаг не зависит от хвостов прежних шагов. Под нагрузкой ворот (1 раз из ~5400)
    // кнопка «Умный фокус» успевала появиться не из-за этого перехода: стартовая отрисовка
    // дашборда при загрузке стенда дописывалась в контейнер позже, а отложенное обновление
    // раздела (scheduleRouteRefresh → route(true)) слало второй запрос статистики мимо шлюза.
    // Поэтому — чистый контейнер и шлюз, который держит все запросы статистики до отпускания.
    vc.innerHTML = '';
    const releaseStats = gate('/dashboard/stats?days=14', false, true);
    sandbox.location.hash = '#/dashboard';
    sandbox.route();
    await tick();
    ok(!vc.innerHTML.includes('btn-focus'), 'v1.70.87: дашборд ждёт ответа API');
    sandbox.location.hash = '#/receipts';
    sandbox.route();
    await tick();
    const receiptsHtml = vc.innerHTML;
    ok(receiptsHtml.includes('rc-view-list'), 'v1.70.87: открыт реестр чеков');
    releaseStats();
    await tick(80);
    ok(vc.innerHTML === receiptsHtml && !vc.innerHTML.includes('btn-focus'),
       'v1.70.87: поздний ответ дашборда не подменил реестр (#25)');
    // сбой устаревшей отрисовки не уводит из открытого раздела на дашборд
    const releaseExport = gate('/dashboard/stats?days=90', true);
    sandbox.location.hash = '#/export';
    sandbox.route();
    await tick();
    sandbox.location.hash = '#/receipts';
    sandbox.route();
    await tick();
    releaseExport();
    await tick(80);
    ok(sandbox.location.hash === '#/receipts',
       'v1.70.87: сбой устаревшего раздела «Выгрузка» не увёл на дашборд');
    // без гонки разделы рисуются как прежде
    sandbox.location.hash = '#/dashboard';
    sandbox.route();
    await tick(80);
    ok(vc.innerHTML.includes('btn-focus'), 'v1.70.87: дашборд без гонки рисуется как прежде');
  }

  // v1.70.91: форма правки из строки списка (без позиций) грузит полный чек и не
  // стирает позиции при сохранении; «Подставить» переносит данные единой записи
  // в пустые поля (Q-28 — вариант «в»).
  {
    const listRow = { id: 'r-edit', fn: '7281440701327430', fd: '11', fp: '22', items_count: 2,
                      total_sum: 100, operation: 1, exported: false };
    const from = apiLog.length;
    await sandbox.openEditReceipt(listRow, () => {});
    const calls = apiLog.slice(from);
    ok(calls.some((c) => c.method === 'GET' && c.url.endsWith('/api/v1/receipts/r-edit')),
       'v1.70.91: форма правки загрузила полный чек с сервера');
    const editSlot = root._cls['.modal-slot'];
    ok(editSlot.innerHTML.includes('Позиции чека (2)'), 'v1.70.91: в форме правки — позиции чека');
    ok(editSlot.innerHTML.includes('id="er-uf-apply"') && editSlot.innerHTML.includes('кассир'),
       'v1.70.91: подсказка «Подставить» перечисляет данные единой записи');
    editSlot.querySelector('#er-uf-apply').onclick();
    ok(editSlot.querySelector('#er-cashier').value === 'Кассир Пулов',
       'v1.70.91: «Подставить» заполнило пустое поле кассира');
    await editSlot.querySelector('#er-save').onclick();
    const patch = apiLog.slice(from).find((c) => c.method === 'PATCH' && c.url.endsWith('/api/v1/receipts/r-edit'));
    ok(patch && !('items' in patch.body), 'v1.70.91: сохранение без правки позиций не отправляет items — позиции целы');
    ok(patch && patch.body.cashier === 'Кассир Пулов', 'v1.70.91: подставленное значение ушло в чек компании');
  }

  // v1.70.94: экран «Первичка» (T-53) — бухгалтеру доступен, реестр УПД с итогом проверки
  {
    const vcP = document.querySelector('#view-container');
    vcP.isConnected = true;                // мини-DOM: контейнер раздела «в документе»
    const tickP = (ms = 30) => new Promise((resolve) => setTimeout(resolve, ms));
    const from = apiLog.length;
    sandbox.location.hash = '#/primary';
    sandbox.route();
    await tickP(80);
    ok(sandbox.location.hash === '#/primary', 'v1.70.94: бухгалтер открыл «Первичку» — раздел в его пространстве');
    ok(vcP.innerHTML.includes('Первичка: электронные УПД'), 'v1.70.94: экран «Первичка» нарисован');
    ok(apiLog.slice(from).some((c) => c.url.startsWith('/api/v1/primary?')), 'v1.70.94: реестр запрошен у сервера');
    const outP = vcP.querySelector('#prim-out');
    ok(outP.innerHTML.includes('УТ-15') && outP.innerHTML.includes('Есть замечания'),
       'v1.70.94: в реестре документ и итог проверки');
    // v1.70.95 (T-61): состояние распознавания и ручной ввод бумажного УПД
    ok(apiLog.slice(from).some((c) => c.url === '/api/v1/primary/ocr'), 'v1.70.95: запрошено состояние распознавания сканов');
    ok(vcP.querySelector('#prim-ocr').innerHTML.includes('не настроено'),
       'v1.70.95: бухгалтер видит, что распознавание сканов не настроено, и почему');
    vcP.querySelector('#prim-manual').onclick();
    await tickP(20);
    const slotP = root.querySelector('.modal-slot');
    ok(slotP.innerHTML.includes('Документ вручную'), 'v1.70.95: открылось окно ручного ввода');
    ok(slotP.querySelector('#prim-save').disabled === true, 'v1.70.95: без номера и продавца сохранить нельзя');
    slotP.querySelector('#prim-f-number').value = 'Б-1';
    slotP.querySelector('#prim-f-date').value = '2026-10-20';
    slotP.querySelector('#prim-f-seller-name').value = 'ООО «Песок»';
    slotP.querySelector('#prim-f-seller-inn').value = '5001007329';
    slotP.oninput();
    ok(slotP.querySelector('#prim-save').disabled === false, 'v1.70.95: обязательные поля заполнены — можно сохранить');
    const fromSave = apiLog.length;
    await slotP.querySelector('#prim-save').onclick();
    const conf = apiLog.slice(fromSave).find((c) => c.method === 'POST' && c.url === '/api/v1/primary/confirm');
    ok(conf && conf.body.source === 'manual' && conf.body.draft.number === 'Б-1' && conf.body.draft.seller.inn === '5001007329',
       'v1.70.95: ручной ввод ушёл на сервер с реквизитами');
    // v1.70.96 (T-60): «В 1С» — сначала проверка (что уйдёт, сколько с ошибками), затем скачивание
    const fromExp = apiLog.length;
    await vcP.querySelector('#prim-export').onclick();
    await tickP(20);
    const chk = apiLog.slice(fromExp).find((c) => c.method === 'POST' && c.url === '/api/v1/primary/export/check');
    ok(chk && chk.body.format === 'upd' && chk.body.include_errors === false && !chk.body.ids,
       'v1.70.96: окно выгрузки спросило у сервера, что уйдёт в 1С (новые, без ошибок, файлы УПД)');
    const slotE = root.querySelector('.modal-slot');
    ok(slotE.querySelector('#prim-exp-check').innerHTML.includes('Будет выгружено: 1')
       && slotE.querySelector('#prim-exp-check').innerHTML.includes('С ошибками проверки не выгружаются: 1'),
       'v1.70.96: бухгалтер видит, сколько уйдёт и сколько документов с ошибками останется');
    ok(slotE.querySelector('#prim-exp-go').disabled === false, 'v1.70.96: есть что выгружать — «Скачать» доступна');
    const fromGo = apiLog.length;
    await slotE.querySelector('#prim-exp-go').onclick();
    const exp = apiLog.slice(fromGo).find((c) => c.method === 'POST' && c.url === '/api/v1/primary/export');
    ok(exp && exp.body.format === 'upd', 'v1.70.96: «Скачать» запросила файл для 1С');
    // v1.70.97 (T-58): «Что нужно от клиента» — срок сдачи, запросы, журнал, поиск недостающего
    const fromD = apiLog.length;
    sandbox.location.hash = '#/docreq';
    sandbox.route();
    await tickP(80);
    ok(vcP.innerHTML.includes('Что нужно от клиента'), 'v1.70.97: бухгалтер открыл «Что нужно от клиента»');
    ok(apiLog.slice(fromD).some((c) => c.url.startsWith('/api/v1/doc-requests?')), 'v1.70.97: запросы месяца запрошены у сервера');
    ok(vcP.querySelector('#dreq-head').innerHTML.includes('5 ноября 2026, 18:00')
       && vcP.querySelector('#dreq-out').innerHTML.includes('Арендодатель'),
       'v1.70.97: виден срок сдачи и что нужно от клиента');
    ok(apiLog.slice(fromD).some((c) => c.url.startsWith('/api/v1/doc-requests/journal')), 'v1.70.97: журнал запрошен');
    const fromDet = apiLog.length;
    await vcP.querySelector('#dreq-detect').onclick();
    const det = apiLog.slice(fromDet).find((c) => c.method === 'POST' && c.url === '/api/v1/doc-requests/detect');
    ok(det && det.body.period === '2026-10', 'v1.70.97: «Найти недостающее» ушло на сервер за выбранный месяц');
    // v1.70.98 (T-45): «Банк: выписка» — платежи без документов и «к возмещению» в итоге
    const fromB = apiLog.length;
    vcP.querySelector('#bank-conn').isConnected = true;   // v1.70.99: мини-DOM — панель банка «в документе»
    sandbox.location.hash = '#/bank';
    sandbox.route();
    await tickP(80);
    ok(sandbox.location.hash === '#/bank', 'v1.70.98: бухгалтер открыл «Банк: выписка» — раздел в его пространстве');
    ok(vcP.innerHTML.includes('Банк: выписка'), 'v1.70.98: экран банковской выписки нарисован');
    ok(apiLog.slice(fromB).some((c) => c.url.startsWith('/api/v1/bank?')), 'v1.70.98: операции запрошены у сервера');
    ok(vcP.querySelector('#bank-out').innerHTML.includes('ООО «Песок»'), 'v1.70.98: виден платёж без документов');
    ok(vcP.querySelector('#bank-kpi').textContent.includes('без документов: 1')
       && vcP.querySelector('#bank-kpi').textContent.includes('к возмещению'),
       'v1.70.98: в итоге — платежи без документов и сумма к возмещению');
    // v1.70.99 (ADR-29): панель «Банк по API» — Точка подключена, счёт и итог последней загрузки
    const conn = vcP.querySelector('#bank-conn').innerHTML;
    ok(apiLog.slice(fromB).some((c) => c.url === '/api/v1/bank/connection'),
       'v1.70.99: бухгалтер — состояние подключения своей компании запрошено');
    ok(conn.includes('Банк по API') && conn.includes('*0001 RUB') && conn.includes('подключён'),
       'v1.70.99: панель «Банк по API»: подключён, счёт *0001');
    ok(conn.includes('Загрузить сейчас') && !conn.includes('s3cret'), 'v1.70.99: кнопка «Загрузить сейчас», секретов нет');
    // v1.70.102 (ADR-32): несколько банков у компании — список над настройками, другой банк открывается кнопкой
    const rowsB = conn.split('class="bank-conn-row').length - 1;      // мини-DOM без разбора HTML — считаем по разметке
    ok(rowsB === 2 && conn.includes('Т-Банк') && conn.includes('*0002'),
       'v1.70.102: панель «Банк по API» — список банков компании (Точка и Т-Банк)');
    ok(conn.includes('data-bank="tbank"') && !conn.includes('data-bank="tochka"') && conn.includes('Другие банки — выписка файлом'),
       'v1.70.102: Т-Банк открывается кнопкой; банки без API — выписка файлом');
    // v1.70.100 (T-50): «Клиенты» — все компании бухгалтера в одном окне; цифра открывает экран в этой компании
    const fromC = apiLog.length;
    sandbox.location.hash = '#/clients';
    sandbox.route();
    await tickP(80);
    ok(sandbox.location.hash === '#/clients' && vcP.innerHTML.includes('Все клиенты в одном окне'),
       'v1.70.100: бухгалтер открыл «Клиенты» — раздел в его пространстве');
    ok(apiLog.slice(fromC).some((c) => c.url === '/api/v1/clients'), 'v1.70.100: сводка по клиентам запрошена у сервера');
    const cards = vcP.querySelector('#cl-list').innerHTML;
    ok(cards.indexOf('ООО «Клиент»') >= 0 && cards.indexOf('ООО «Клиент»') < cards.indexOf('Ромашка'),
       'v1.70.100: сначала компания, где нужны действия');
    ok(cards.includes('просрочено на 1 день') && cards.includes('<b>2</b> просрочено'),
       'v1.70.100: срок сдачи и просроченные запросы видны');
    const fromSw = apiLog.length;
    vcP.querySelector('#cl-list').onclick({ target: { closest: (sel) => (sel === '[data-open]'
      ? { dataset: { company: 'c2', open: 'docreq' } } : null) } });
    await tickP(40);
    ok(storage['ymaster-client'] === 'c2', 'v1.70.100: переход в компанию клиента — клиент выбран');
    const meCall = apiLog.slice(fromSw).find((c) => c.url.startsWith('/api/v1/auth/me'));
    ok(meCall && meCall.headers['X-Ym-Company'] === 'c2', 'v1.70.100: запросы уходят с заголовком выбранного клиента');
    ok(sandbox.location.hash === '#/docreq', 'v1.70.100: открыт «Что нужно от клиента» этой компании');
    delete storage['ymaster-client'];
    // v1.70.100 (T-68): пакет для других учётных систем — на экране «Выгрузка в 1С»
    sandbox.location.hash = '#/export';
    sandbox.route();
    await tickP(80);
    ok(vcP.innerHTML.includes('📦 Пакет для других учётных систем') && vcP.innerHTML.includes('id="pkg-download"'),
       'v1.70.100: на экране выгрузки — пакет JSON, CSV и УПД за месяц');
    // v1.70.101 (T-69): карточка чека — читаемые позиции, чипы категорий, сводка и подсказка статьи
    const before101 = document.body.children.length;
    await sandbox.receiptDrawer('r-goods');
    await tickP(40);
    const drawer101 = document.body.children[document.body.children.length - 1];
    const dh = (document.body.children.length > before101 && drawer101.innerHTML) || '';
    ok(dh.includes('Цемент М500 Д0, 50 кг') && dh.includes('title="В чеке: ЦЕМЕНТ М500 Д0 50КГ"')
       && dh.includes('data-gd-item="i1"') && dh.includes('🏗 Стройматериалы · 2 · ') && dh.includes('Стройматериалы ?')
       && dh.includes('data-article="Материалы"'),
       'v1.70.101: позиции чека — читаемые названия и чипы категорий');
    const wrap101 = drawer101.querySelector('.gd-wrap');
    const slot101 = new El('span');
    const fromG = apiLog.length;
    await wrap101._ev.click({ target: { closest: (sel) => (sel === '.gd-edit-btn'
      ? { dataset: { gdItem: 'i2' }, parentElement: slot101 } : null) } });
    const pick101 = slot101.querySelector('.gd-pick');
    slot101.querySelector('.gd-remember-box').checked = true;          // в живом DOM галочка включена разметкой
    pick101.value = 'build_finish';
    await pick101.onchange();
    await tickP(20);
    const put101 = apiLog.slice(fromG).find((c) => c.method === 'PUT' && c.url === '/api/v1/goods/items/i2');
    ok(put101 && put101.body.category === 'build_finish' && put101.body.remember === true
       && slot101.innerHTML.includes('Отделочные материалы'),
       'v1.70.101: бухгалтер меняет категорию — запрос с «запомнить»');
    if (sandbox.receiptDrawer._close) sandbox.receiptDrawer._close();
    // v1.70.101: «Настройки» — профиль компании в политике и карточка «Память товаров»
    const fromS = apiLog.length;
    sandbox.location.hash = '#/settings';
    sandbox.route();
    await tickP(120);
    const sh = vcP.innerHTML;
    ok(apiLog.slice(fromS).some((c) => c.url.startsWith('/api/v1/goods/memory'))
       && sh.includes('Профиль компании') && sh.includes('Стройка и ремонт') && sh.includes('Память товаров')
       && sh.includes('ключ: смесь абвгд'),
       'v1.70.101: в настройках — профиль компании и «Память товаров»');
    vcP.isConnected = undefined;
  }

  console.log(fails.length ? 'HARNESS FAIL: ' + fails.join('; ')
              : `HARNESS_OK checks`);
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error('HARNESS_CRASH', e && e.stack || e); process.exit(2); });
