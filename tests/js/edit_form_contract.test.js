// Контракт формы правки чека (T-41, v1.70.110): настоящий openEditReceipt из app.js в
// часовом поясе Челябинска (UTC+5) — какое тело уходит в PATCH. Набор полей и их
// обработку сервером (SQLite и PostgreSQL) проверяет tests/test_ui_api_contract.py;
// здесь — значения, которые кладёт сама форма, прежде всего время чека.
//
// До 1.70.110 форма слала время в UTC (toISOString): 15:56 по чеку уходило как
// 10:56Z, сервер сохранял 10:56, и каждое сохранение сдвигало время ещё на 5 часов.
import { describe, it, expect, vi, beforeEach } from 'vitest';
import { readFileSync } from 'node:fs';
import { parse } from 'acorn';
import { esc, fmtSum, openModal } from '../../app/static/js/ui.js';
import { applyFill, fillCallout } from '../../app/static/js/unified_fill.js';

process.env.TZ = 'Asia/Yekaterinburg';    // пояс браузера пользователя (Челябинск, UTC+5)

const source = readFileSync('app/static/js/app.js', 'utf8');
const ast = parse(source, { ecmaVersion: 'latest', sourceType: 'module' });
const fnSource = (name) => {
  const node = ast.body.find((n) => n.type === 'FunctionDeclaration' && n.id.name === name);
  if (!node) throw new Error(`в app.js нет функции ${name}`);
  return source.slice(node.start, node.end);
};
const makeEdit = new Function('api', 'toast', 'esc', 'fmtSum', 'openModal', 'fillCallout',
  'applyFill', 'isAdmin', 'state', 'viewReceipts', 'extBlockHTML',
  `${fnSource('_dtLocal')}\n${fnSource('openEditReceipt')}\nreturn openEditReceipt;`);

// Чек по QR после Чек-Пула: у копии нет продавца и позиций, единая запись их знает
const receipt = (extra = {}) => ({
  id: 'r-1', fn: '7000000000000001', fd: '3467', fp: '2882574203',
  receipt_date: '2026-09-22T15:56:00', total_sum: 100, personal_sum: 0, operation: 1,
  merchant_name: '', merchant_inn: '', merchant_address: '', cashier: '',
  assignee: '', category: '', comment: '', notified: false, exported: false, items: [],
  unified_fill: {
    fields: { merchant_name: 'Магазин', merchant_inn: '4703148343',
      merchant_address: 'г. Контракт, ул. Формы, 7', cashier: 'Кассир Контрактов' },
    items: [{ name: 'Товар', quantity: 1, price: 100, total: 100 }],
  },
  ...extra,
});

let sent;
const flush = () => new Promise((resolve) => setTimeout(resolve, 0));

async function openForm(r, { admin = false } = {}) {
  sent = [];
  const api = {
    get: vi.fn(async () => r),
    patch: vi.fn(async (url, body) => { sent.push({ url, body }); return {}; }),
    post: vi.fn(async () => ({})),
  };
  const open = makeEdit(api, vi.fn(), esc, fmtSum, openModal, fillCallout, applyFill,
    () => admin, { me: { role: admin ? 'admin' : 'accountant' } }, { _catsTop: [] }, () => '');
  await open(r, vi.fn());
  return (sel) => document.querySelector(sel);
}

async function save($) {
  $('#er-save').click();
  await flush();
  expect(sent).toHaveLength(1);
  expect(sent[0].url).toBe('/api/v1/receipts/r-1');
  return sent[0].body;
}

describe('форма правки чека: тело PATCH в поясе UTC+5', () => {
  beforeEach(() => {
    document.body.innerHTML = '<div id="modal-root" class="hidden">'
      + '<div class="modal-backdrop"></div><div class="modal-slot"></div></div>';
  });

  it('пояс теста действительно UTC+5 — иначе проверка ничего не доказывает', () => {
    expect(new Date('2026-09-22T15:56:00').toISOString()).toBe('2026-09-22T10:56:00.000Z');
  });

  it('сохранение без правки: время не уходит, пустые поля — пустой строкой', async () => {
    const $ = await openForm(receipt());
    expect($('#er-date').value).toBe('2026-09-22T15:56');      // поле показывает время кассы
    $('#er-comment').value = 'правка через форму';
    const body = await save($);
    expect(body).not.toHaveProperty('receipt_date');
    expect(body).not.toHaveProperty('items');                  // позиции — только после правки
    expect(body).toMatchObject({ merchant_name: '', merchant_inn: '', merchant_address: '',
      cashier: '', comment: 'правка через форму', total_sum: 100, operation: 1,
      fn: '7000000000000001', fd: '3467', fp: '2882574203' });
  });

  it('изменённое время уходит как введено — без пересчёта в UTC', async () => {
    const $ = await openForm(receipt());
    $('#er-date').value = '2026-09-22T16:10';
    const body = await save($);
    expect(body.receipt_date).toBe('2026-09-22T16:10');
  });

  it('время с секундами не теряется, если его не трогали', async () => {
    const $ = await openForm(receipt({ receipt_date: '2026-09-22T15:56:37' }));
    const body = await save($);
    expect(body).not.toHaveProperty('receipt_date');
  });

  it('«Подставить» переносит данные единой записи в тело', async () => {
    const $ = await openForm(receipt());
    $('#er-uf-apply').click();
    const body = await save($);
    expect(body).toMatchObject({ merchant_name: 'Магазин', merchant_inn: '4703148343',
      merchant_address: 'г. Контракт, ул. Формы, 7', cashier: 'Кассир Контрактов' });
    expect(body.items).toEqual([{ name: 'Товар', quantity: 1, price: 100, total: 100,
      vat_rate: 'none', vat_sum: 0 }]);
    expect(body).not.toHaveProperty('receipt_date');
  });

  it('выгруженный чек у бухгалтера: реквизиты и время не уходят', async () => {
    const $ = await openForm(receipt({ exported: true }));
    const body = await save($);
    for (const key of ['receipt_date', 'total_sum', 'operation', 'fn', 'fd', 'fp', 'items']) {
      expect(body).not.toHaveProperty(key);
    }
    expect(body).toHaveProperty('comment', '');
  });
});
