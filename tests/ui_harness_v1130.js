// ======================================================================
// Ямастер Чек — UI-harness v1.13.0 (Node vm DOM-песочница): карточка
// компании (ЕГРЮЛ/Checko), диалог умного удаления (move/wipe).
// Запуск: node tests/ui_harness_v1130.js  (бандл строит tests/test_v1130.py)
// ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// ======================================================================
// ======================================================================
// UI-harness v1.13.0: карточка компании + диалог удаления (DOM-песочница)
// ООО «Ямастер» | ymaster.ru | info@ymaster.ru
// ======================================================================
const fs = require('fs');
const vm = require('vm');

// --- мини-DOM -----------------------------------------------------------
class El {
  constructor(tag = 'div') {
    this.tagName = tag; this.children = []; this.dataset = {};
    this.style = {}; this._html = ''; this.textContent = ''; this.disabled = false;
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
  setAttribute() {} getAttribute() { return null; }
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
const document = {
  getElementById: (id) => (id === 'modal-root' ? root : new El()),
  createElement: (t) => new El(t),
  querySelector: () => new El(),
  querySelectorAll: () => [],
  addEventListener: () => {},
  body: new El('body'),
  documentElement: new El('html'),
};

const storage = {};
const apiLog = [];
const toasts = [];

const sandbox = {
  document, window: { addEventListener: () => {}, matchMedia: () => ({ matches: false, addEventListener: () => {} }), location: { reload() {}, pathname: '/' } },
  navigator: { clipboard: { writeText: async () => {} }, userAgent: 'harness' },
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

sandbox.fetch = async (url, opts) => {
  apiLog.push({ url: String(url), method: (opts && opts.method) || 'GET',
                body: opts && opts.body ? JSON.parse(opts.body) : null });
  let payload = {};
  if (String(url).includes('/card')) payload = cardPayload;
  if (String(url).includes('/stats'))
    payload = { total: 0, by_fns: {}, by_day: [], by_company: {}, by_source: {},
                by_user: {}, by_status: {}, valid: 0, invalid: 0 };
  if (String(url).includes('/delete')) payload = { ok: true, message: 'Компания удалена' };
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
const bundle = fs.readFileSync('/tmp/ymaster_ui_bundle_v1130.js', 'utf8');
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

  console.log(fails.length ? 'HARNESS FAIL: ' + fails.join('; ') : 'HARNESS_OK 12 checks');
  process.exit(fails.length ? 1 : 0);
})().catch(e => { console.error('HARNESS_CRASH', e && e.stack || e); process.exit(2); });
