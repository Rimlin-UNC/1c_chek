// Первичка: выгрузка в 1С (T-60, v1.70.96) — отметки в реестре, окно выгрузки с проверкой,
// файл для 1С из карточки. Чистые функции и экран в jsdom; API подменён (только синтетика).
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], download: [] };
const listPayload = {
  items: [
    { id: 'd1', kind: 'УПД', doc_number: 'УТ-15', doc_date: '2026-03-10', correction_number: '',
      seller: { name: 'ООО «СтройБаза»', inn: '5001007326', kpp: '500101001' },
      total_with_vat: 7930, vat_total: 1430, status: 'ok', errors: 0, warnings: 0, exported_at: null },
    { id: 'd2', kind: 'УПД', doc_number: 'УТ-16', doc_date: '2026-03-11', correction_number: '',
      seller: { name: 'ООО «Песок»', inn: '5001007329', kpp: '' },
      total_with_vat: 100, vat_total: 18.03, status: 'ok', errors: 0, warnings: 0, exported_at: '2026-03-11T21:30:00' },
  ],
  total: 2,
  summary: { ok: 2, warnings: 0, errors: 0 },
};
const detailPayload = { ...listPayload.items[0], format_version: '5.03', function: 'СЧФДОП', file_name: 'upd.xml',
  buyer: { name: 'ООО «Покупатель»', inn: '7700000000', kpp: '770401001' }, total_without_vat: 6500, issues: [], lines: [] };
let checkPayload = { count: 1, skipped: [], already_exported: 0, errors_excluded: 0, limit: 500, format: 'upd' };

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => { calls.get.push(path); return path.includes('/primary?') ? listPayload : detailPayload; }),
    post: vi.fn(async (path, body) => { calls.post.push({ path, body }); return checkPayload; }),
    del: vi.fn(async () => ({ ok: true })),
    download: vi.fn(async (path, body) => {
      calls.download.push({ path, body });
      return { blob: new Blob(['<Файл/>']), filename: 'ON_NSCHFDOPPR_1.xml' };
    }),
  },
}));

const mod = await import('../../app/static/js/primary.js');
const { PRIM_EXPORT_FORMATS, primExportBody, primExportedLabel, primExportSummaryHtml, primRowsHtml, viewPrimary } = mod;

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('выгрузка в 1С: разметка', () => {
  it('дата выгрузки — из времени UTC без пометки зоны', () => {
    const label = primExportedLabel('2026-03-11T21:30:00');
    expect(label).toBe(`✓ ${new Date('2026-03-11T21:30:00Z').toLocaleDateString('ru-RU')}`);
    expect(primExportedLabel('2026-03-11T21:30:00+03:00')).toMatch(/^✓ /);
    expect(primExportedLabel(null)).toBe('');
    expect(primExportedLabel('мусор')).toBe('');
  });

  it('тело запроса: отмеченные — по id, иначе период и компания', () => {
    expect(primExportBody({ ids: ['a', 'b'], format: 'json', from: '2026-03-01' }))
      .toEqual({ format: 'json', include_errors: false, ids: ['a', 'b'] });
    expect(primExportBody({ from: '2026-03-01', to: '2026-03-31', companyId: 'c1', includeErrors: 1 }))
      .toEqual({ format: 'upd', include_errors: true, date_from: '2026-03-01', date_to: '2026-03-31', company_id: 'c1' });
    expect(primExportBody()).toEqual({ format: 'upd', include_errors: false });
  });

  it('итог проверки: сколько уйдёт, повторы, ошибки и причины пропуска (с экранированием)', () => {
    const html = primExportSummaryHtml({ count: 2, already_exported: 1, errors_excluded: 3, limit: 500,
      skipped: [{ title: 'УПД № <1>', reason: 'у продавца нет ИНН' }] });
    expect(html).toContain('Будет выгружено: 2');
    expect(html).toContain('выгрузятся повторно');
    expect(html).toContain('С ошибками проверки не выгружаются: 3');
    expect(html).toContain('УПД № &lt;1&gt;');
    expect(html).toContain('у продавца нет ИНН');
    expect(primExportSummaryHtml({ count: 500, limit: 500 })).toContain('не больше 500');
    expect(primExportSummaryHtml(null)).toBe('');
  });

  it('строки реестра: флажок отметки и колонка «1С»', () => {
    const html = primRowsHtml(listPayload.items, new Set(['d2']));
    expect(html).toContain('class="prim-pick" data-id="d1"');
    expect(html.match(/checked/g)).toHaveLength(1);
    expect(html).toContain('title="Выгружен в 1С"');
    expect(primRowsHtml([])).toContain('colspan="9"');
    expect(PRIM_EXPORT_FORMATS.map((f) => f.code)).toEqual(['upd', 'json', 'xml']);
    expect(PRIM_EXPORT_FORMATS[0].hint).toContain('Загрузить → Из файла');
  });
});

describe('выгрузка в 1С: экран', () => {
  beforeEach(() => {
    calls.get.length = 0; calls.post.length = 0; calls.download.length = 0;
    checkPayload = { count: 1, skipped: [], already_exported: 0, errors_excluded: 0, limit: 500, format: 'upd' };
    document.body.innerHTML = `<div id="toast-container"></div><div id="view"></div>
      <div id="modal-root" class="hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div>`;
    URL.createObjectURL = vi.fn(() => 'blob:x');
    URL.revokeObjectURL = vi.fn();
    // jsdom не умеет переходить по ссылке на blob — сам клик проверяем шпионом
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => {});
  });

  it('фильтр «Не выгружены» уходит параметром exported', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, {});
    const sel = view.querySelector('#prim-exported');
    sel.value = 'false';
    sel.onchange({ target: sel });
    await flush();
    expect(calls.get.at(-1)).toContain('exported=false');
  });

  it('отмеченные: проверка, смена формата, скачивание, отметки сброшены', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, {});
    const box = view.querySelector('.prim-pick[data-id="d2"]');
    box.checked = true;
    box.onchange();
    expect(view.querySelector('#prim-picked').textContent).toBe('Отмечено: 1');
    checkPayload = { ...checkPayload, already_exported: 1 };
    await view.querySelector('#prim-export').onclick();
    await flush();
    expect(calls.post[0]).toEqual({ path: '/api/v1/primary/export/check',
      body: { format: 'upd', include_errors: false, ids: ['d2'] } });
    const slot = document.querySelector('.modal-slot');
    expect(slot.innerHTML).toContain('Отмечено документов: 1');
    expect(slot.querySelector('#prim-exp-check').innerHTML).toContain('выгрузятся повторно');
    expect(slot.querySelector('#prim-exp-go').disabled).toBe(false);
    slot.querySelector('#prim-exp-json').checked = true;
    await slot.querySelector('#prim-exp-json').onchange();
    expect(calls.post.at(-1).body.format).toBe('json');
    await slot.querySelector('#prim-exp-go').onclick();
    expect(calls.download).toEqual([{ path: '/api/v1/primary/export',
      body: { format: 'json', include_errors: false, ids: ['d2'] } }]);
    expect(URL.createObjectURL).toHaveBeenCalled();
    expect(HTMLAnchorElement.prototype.click).toHaveBeenCalled();
    expect(document.getElementById('modal-root').classList.contains('hidden')).toBe(true);
    await flush();
    expect(view.querySelector('#prim-picked').textContent).toBe('');
  });

  it('без отметок — новые документы периода; нечего выгружать — «Скачать» недоступна', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, {});
    const from = view.querySelector('#prim-from');
    from.value = '2026-03-01';
    from.onchange({ target: from });
    await flush();
    checkPayload = { count: 0, skipped: [{ title: 'УПД № 7', reason: 'нет номера или даты документа' }],
      already_exported: 0, errors_excluded: 2, limit: 500, format: 'upd' };
    await view.querySelector('#prim-export').onclick();
    await flush();
    expect(calls.post.at(-1).body).toEqual({ format: 'upd', include_errors: false, date_from: '2026-03-01' });
    const slot = document.querySelector('.modal-slot');
    expect(slot.innerHTML).toContain('с 01.03.2026');
    expect(slot.querySelector('#prim-exp-check').innerHTML).toContain('нет номера или даты документа');
    expect(slot.querySelector('#prim-exp-go').disabled).toBe(true);
    slot.querySelector('#prim-exp-errors').checked = true;
    await slot.querySelector('#prim-exp-errors').onchange();
    expect(calls.post.at(-1).body.include_errors).toBe(true);
  });

  it('карточка: «Файл для 1С» — один документ, явный выбор', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, {});
    view.querySelector('.prim-open').onclick();
    await flush();
    expect(document.querySelector('.modal-slot').innerHTML).toContain('ещё не выгружен');
    await document.getElementById('prim-card-1c').onclick();
    expect(calls.download).toEqual([{ path: '/api/v1/primary/export',
      body: { format: 'upd', include_errors: true, ids: ['d1'] } }]);
  });
});
