/* v1.70.113 (T-79, #63): «Банк: выписка» → «Закрытие документами» — сводка, платежи с документами и способом
   подбора, неоплаченные УПД, расхождения по контрагентам, ручная привязка. API подменён, данные синтетические. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], download: [] };
const row = (extra = {}) => ({
  id: 'op1', side: 'expense', date: '2026-10-10', number: '1', amount: 97600, counterparty_name: 'ООО «S1»',
  counterparty_inn: '7700000001', purpose: 'Оплата по счёту 12 <материалы>', kind: 'transfer', closed: 97600, rest: 0,
  status: 'closed', manual: false, links: [{ doc_id: 'd1', title: 'УПД № 101 от 05.10.2026', date: '2026-10-05', amount: 61000, how: 'set' },
    { doc_id: 'd2', title: 'УПД № 102 от 15.10.2026', date: '2026-10-15', amount: 36600, how: 'set' }], ...extra,
});
const side = (rows, extra = {}) => ({
  summary: { count: 3, amount: 119600, closed_amount: 106750, open_amount: 12850, percent: 89.3, closed_count: 1,
    partial_count: 1, open_count: 1, not_needed_count: 0 },
  rows, unpaid: [], counterparties: [], ...extra,
});
const report = {
  company: { id: 'c-a', name: 'Альфа', inn: '7700000009' }, period: { from: '2026-01-01', to: '2026-10-31' }, has_incomes: true,
  expense: side([row(), row({ id: 'op2', number: '2', amount: 15000, closed: 9150, rest: 5850, status: 'partial',
    links: [{ doc_id: 'd3', title: 'УПД № 45 от 08.10.2026', date: '2026-10-08', amount: 9150, how: 'number' }] }),
  row({ id: 'op3', number: '3', amount: 7000, closed: 0, rest: 7000, status: 'open', links: [], counterparty_inn: '7700000003' })],
  { counterparties: [{ inn: '7700000002', name: 'ООО «S2»', payments: 15000, documents: 9150, balance: 5850 }] }),
  income: side([row({ id: 'in1', side: 'income', kind: 'income', amount: 48800, closed: 48800,
    links: [{ doc_id: 'd9', title: 'УПД № П-7 от 15.10.2026', date: '2026-10-15', amount: 48800, how: 'sum' }] })],
  { unpaid: [{ id: 'd8', title: 'УПД № Г-1 от 11.10.2026', counterparty_name: 'ООО «B»', counterparty_inn: '7700000008',
    amount: 12200, paid: 0, rest: 12200 }] }),
};

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path.startsWith('/api/v1/bank/closing/candidates')) {
        return { operation: { id: 'op3', date: '2026-10-14', amount: 7000, counterparty_name: 'ООО «S3»' },
          items: [{ id: 'd3', title: 'УПД № 45 от 08.10.2026', amount: 9150, rest: 0, linked: false }] };
      }
      if (path.startsWith('/api/v1/bank/closing')) return report;
      if (path.startsWith('/api/v1/bank/connection')) return { bank: 'tochka', status: 'none', banks: [] };
      return { statements: [], staff: [], operations: [], status: 'open', totals: {}, reimburse: {}, settings: { cards: [] } };
    }),
    post: vi.fn(async (path, body) => { calls.post.push({ path, body }); return { linked: 1, unlinked: 1 }; }),
    download: vi.fn(async (path) => { calls.download.push(path); return { blob: new Blob(['csv']), filename: 'zakrytie.csv' }; }),
  },
}));

const mod = await import('../../app/static/js/bank.js');
const tick = () => new Promise((r) => setTimeout(r, 0));
const norm = (text) => String(text).replace(/[\u00a0\u202f]/g, ' ');     // Intl: неразрывные пробелы в суммах

beforeEach(() => {
  calls.get = []; calls.post = []; calls.download = [];
  document.body.innerHTML = '<div id="toast-container"></div><div id="modal-root" class="modal-root hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div><div id="app"></div>';
  URL.createObjectURL = vi.fn(() => 'blob:x');
  URL.revokeObjectURL = vi.fn();
  vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
});

describe('«Закрытие документами»: отрисовка', () => {
  it('сводка: доля закрытого, частично и без документов', () => {
    expect(norm(mod.bankClosingSummary(report.expense.summary, 'expense'))).toBe(
      'Расходы: 3 на 119 600,00 ₽ — закрыто документами 106 750,00 ₽ (89,3 %); частично: 1, без документов: 1 на 12 850,00 ₽');
    expect(mod.bankClosingSummary({ count: 0 }, 'income')).toBe('Поступлений от контрагентов с ИНН за период нет.');
  });

  it('строки: документы с суммами и способом подбора, фильтр, кнопки привязки, экранирование', () => {
    const all = mod.bankClosingRowsHtml(report.expense.rows, 'all');
    expect(all).toContain('УПД № 101 от 05.10.2026 — 61');
    expect(all).toContain('· сумма нескольких документов');
    expect(all).toContain('осталось 5');
    expect(all).toContain('&lt;материалы&gt;');
    expect(all).not.toContain('<материалы>');
    expect((all.match(/bank-cl-link/g) || []).length).toBe(3);
    expect(all).not.toContain('bank-cl-unlink');
    const open = mod.bankClosingRowsHtml(report.expense.rows, 'open');
    expect(open).toContain('ИНН 7700000003');
    expect(open).not.toContain('УПД № 101');
    const manual = mod.bankClosingRowsHtml([row({ manual: true, links: [{ doc_id: 'd1', title: 'УПД № 7', amount: 10, how: 'manual' }] })]);
    expect(manual).toContain('bank-cl-unlink');
    expect(manual).toContain('привязал бухгалтер');
    const card = mod.bankClosingRowsHtml([row({ kind: 'card', status: 'closed', links: [{ doc_id: 'r1', title: 'чек на 1 200,00 ₽', amount: 1200, how: 'receipt' }] })]);
    expect(card).not.toContain('bank-cl-link');
    expect(card).toContain('· кассовый чек');
    expect(mod.bankClosingRowsHtml([], 'all')).toContain('Под фильтр ничего не попало');
  });

  it('неоплаченные документы и расхождения по контрагентам', () => {
    expect(mod.bankClosingUnpaidHtml([], 'income')).toBe('');
    const unpaid = mod.bankClosingUnpaidHtml(report.income.unpaid, 'income');
    expect(unpaid).toContain('Документы без оплаты в выписке (1)');
    expect(unpaid).toContain('<th>Покупатель</th>');
    const cps = mod.bankClosingCounterpartiesHtml(report.expense.counterparties, 'expense');
    expect(cps).toContain('Расхождения по контрагентам (1)');
    expect(cps).toContain('оплачено больше, чем документов');
    expect(mod.bankClosingCounterpartiesHtml([{ inn: '1', name: 'X', payments: 5, documents: 5, balance: 0 }], 'expense')).toBe('');
  });

  it('период по умолчанию — с начала года по сегодня', () => {
    expect(mod.bankClosingPeriod(new Date(2026, 9, 7))).toEqual({ from: '2026-01-01', to: '2026-10-07' });
  });
});

describe('«Закрытие документами»: экран', () => {
  it('вкладка: отчёт за период, расходы и поступления, привязка и CSV', async () => {
    const box = document.getElementById('app');
    await mod.viewBank(box, {});
    box.querySelector('#bank-tab-closing').click();
    await tick();
    await tick();
    expect(calls.get.some((p) => p.startsWith('/api/v1/bank/closing?date_from='))).toBe(true);
    expect(norm(box.querySelector('#bank-cl-sum').textContent)).toContain('закрыто документами 106 750,00 ₽');
    expect(box.querySelector('.bank-cl-bar').getAttribute('aria-valuenow')).toBe('89.3');
    box.querySelector('#bank-cl-filter [data-filter="open"]').click();
    expect(box.querySelectorAll('.bank-cl-table tbody tr')).toHaveLength(1);
    box.querySelector('#bank-cl-side [data-side="income"]').click();
    expect(box.querySelector('#bank-out').textContent).toContain('Документы без оплаты в выписке (1)');
    box.querySelector('#bank-cl-side [data-side="expense"]').click();
    box.querySelector('#bank-cl-filter [data-filter="all"]').click();
    box.querySelector('.bank-cl-link[data-id="op3"]').click();
    await tick();
    const slot = document.querySelector('.modal-slot');
    expect(slot.textContent).toContain('(уже закрыт другими платежами)');
    slot.querySelector('.bank-cl-cands input').checked = true;
    slot.querySelector('#bank-cl-save').click();
    await tick();
    expect(calls.post).toEqual([{ path: '/api/v1/bank/closing/link', body: { side: 'expense', operation_id: 'op3', document_ids: ['d3'] } }]);
    box.querySelector('#bank-cl-csv').click();
    await tick();
    expect(calls.download[0]).toMatch(/^\/api\/v1\/bank\/closing\.csv\?date_from=.*&side=expense$/);
  });
});
