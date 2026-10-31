import { beforeEach, afterEach, describe, expect, it, vi } from 'vitest';
import { readFileSync } from 'node:fs';
import { shopReceiptHTML } from '../../app/static/js/card.js';
import { esc } from '../../app/static/js/ui.js';
import { packCut, splitBlocks, COL_H, columnX, SCALE, RCPT_W, MARGIN_Y } from '../../app/static/js/printpack.js';

// Проверяем реальную функцию печати без запуска всего SPA. JSDOM не выполняет
// раскладку, поэтому высоты стабильно имитируются; геометрия проверяется отдельно.
const source = readFileSync('app/static/js/app.js', 'utf8');
const start = source.indexOf('async function printReceiptsPDF(ids, report = null)');
const end = source.indexOf('\nfunction dataCount', start);
const makePrint = new Function('api', 'openModal', 'toast', 'shopReceiptHTML', 'esc',
  'packCut', 'splitBlocks', 'COL_H', 'columnX', 'SCALE', 'RCPT_W', 'MARGIN_Y',
  'setPrintPageMargin', 'whenImagesReady', 'dataCount',
  `${source.slice(start, end)}; return printReceiptsPDF;`);

describe('печать карточек', () => {
  let api, toast, print;
  beforeEach(() => {
    document.body.innerHTML = '<p id="pp-status"></p>';
    vi.spyOn(Element.prototype, 'getBoundingClientRect').mockImplementation(function () {
      const height = this.classList.contains('ticket') ? this.children.length * 20 : 20;
      return { height };
    });
    URL.createObjectURL = vi.fn(() => 'blob:test');
    URL.revokeObjectURL = vi.fn();
    window.print = vi.fn();
    toast = vi.fn();
    api = {
      get: vi.fn(async () => ({ document: { seller: { name: 'Магазин' },
        money: { total: 120, prepaid: 120 }, kkt: { fn: '123', fd: '456', fp: '789' },
        items: [{ name: 'Товар', quantity: 2, price: 60, total: 120, nds_name: '20%' }] } })),
      download: vi.fn(async () => ({ blob: new Blob(['qr']) })),
    };
    print = makePrint(api, () => ({ close: vi.fn() }), toast, shopReceiptHTML, esc,
      packCut, splitBlocks, COL_H, columnX, SCALE, RCPT_W, MARGIN_Y,
      vi.fn(), vi.fn(async () => {}), n => `чеков: ${n}`);
  });
  afterEach(() => { vi.restoreAllMocks(); document.getElementById('print-root')?.remove(); });
  it('печатает все выбранные карточки с позициями, оплатой и фискальными реквизитами', async () => {
    await print(['one', 'two']);
    expect(api.get.mock.calls.map(c => c[0])).toEqual(['/api/v1/receipts/one/card', '/api/v1/receipts/two/card']);
    const root = document.getElementById('print-root');
    expect(root.textContent).toContain('Магазин');
    expect(root.textContent).toContain('Товар');
    expect(root.textContent).toContain('ПРЕДОПЛАТА');
    expect(root.textContent).toContain('456');
    expect(root.querySelectorAll('.print-piece')).toHaveLength(2);
    expect(root.querySelectorAll('img[src="blob:test"]')).toHaveLength(2);
    expect(window.print).toHaveBeenCalledOnce();
    window.dispatchEvent(new Event('afterprint'));
    expect(document.getElementById('print-root')).toBeNull();
  });
  it('предупреждает о QR, если он не получен', async () => {
    api.download.mockRejectedValue(new Error('нет QR'));
    await print(['one']);
    expect(document.getElementById('print-root').textContent).toContain('Фискальный QR не получен');
    expect(toast.mock.calls[0][0]).toContain('без QR: 1');
    window.dispatchEvent(new Event('afterprint'));
  });
  it('добавляет листы чеков после отчёта и нумерует приложения', async () => {
    const makeReport = vi.fn(count => {
      expect(count).toBe(1);
      const root = document.createElement('div');
      root.id = 'print-root';
      root.innerHTML = '<div class="print-page ao1-page">Отчёт</div>';
      document.body.appendChild(root);
      return root;
    });
    await print(['one', 'two'], makeReport);
    const root = document.getElementById('print-root');
    expect(root.firstElementChild.textContent).toBe('Отчёт');
    expect(root.textContent).toContain('Приложение № 1');
    expect(root.textContent).toContain('Приложение № 2');
    expect(window.print).toHaveBeenCalledOnce();
    window.dispatchEvent(new Event('afterprint'));
  });
  it('не печатает неполный набор, если карточка недоступна', async () => {
    api.get.mockResolvedValueOnce({ document: { items: [] } }).mockRejectedValueOnce(new Error('нет доступа'));
    await print(['one', 'two']);
    expect(window.print).not.toHaveBeenCalled();
    expect(toast.mock.calls[0][0]).toContain('нет доступа');
    expect(document.getElementById('print-root')).toBeNull();
  });
});
