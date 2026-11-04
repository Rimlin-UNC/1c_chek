// Первичка (T-53, v1.70.94): экран электронных УПД — реестр, загрузка, карточка.
// Чистые функции разметки и сам экран в jsdom; API подменён (только синтетика).
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], del: [] };
const listPayload = {
  items: [
    { id: 'd1', kind: 'УПД', doc_number: 'УТ-15', doc_date: '2026-03-10', correction_number: '',
      seller: { name: 'ООО «СтройБаза»', inn: '5001007326', kpp: '500101001' },
      total_with_vat: 7930, vat_total: 1430, status: 'ok', errors: 0, warnings: 0 },
    { id: 'd2', kind: 'УПД', doc_number: 'УТ-16', doc_date: '2026-03-11', correction_number: '1',
      seller: { name: 'ИП <Петров>', inn: '500100000000', kpp: '' },
      total_with_vat: 100, vat_total: 18.03, status: 'errors', errors: 1, warnings: 2 },
  ],
  total: 2,
  summary: { ok: 1, warnings: 0, errors: 1 },
};
const detailPayload = {
  ...listPayload.items[0], format_version: '5.03', function: 'СЧФДОП', file_name: 'upd.xml',
  buyer: { name: 'ООО «Покупатель»', inn: '7700000000', kpp: '770401001' },
  total_without_vat: 6500,
  issues: [{ level: 'warning', code: 'line_vat', message: 'Строка 1: НДС <не сходится>' }],
  lines: [{ position: 1, name: 'Цемент М500', quantity: 10, unit_name: 'шт', price: 500, sum_without_vat: 5000,
            vat_rate: '22', vat_sum: 1100, sum_with_vat: 6100, item_code: 'CEM-500' }],
};

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => { calls.get.push(path); return path.includes('/primary?') ? listPayload : detailPayload; }),
    post: vi.fn(async (path, body) => {
      calls.post.push({ path, body });
      return { created: 1, duplicates: 1, rejected: 1, results: [
        { file: 'a.xml', result: 'created', issues: [], document: detailPayload },
        { file: 'b.xml', result: 'duplicate', message: 'Этот документ уже загружен', document: detailPayload },
        { file: 'c.txt', result: 'rejected', message: 'Это не XML-документ ЭДО или файл повреждён', issues: [] },
      ] };
    }),
    del: vi.fn(async (path) => { calls.del.push(path); return { ok: true }; }),
  },
}));

const mod = await import('../../app/static/js/primary.js');
const { primDay, primIssuesHtml, primLinesHtml, primRowsHtml, primStatusChip, primUploadSummary, primVatLabel,
  viewPrimary } = mod;

const flush = () => new Promise((r) => setTimeout(r, 0));

describe('первичка: разметка', () => {
  it('статус проверки — цветом существующих чипов', () => {
    expect(primStatusChip('ok')).toContain('class="chip verified"');
    expect(primStatusChip('warnings')).toContain('Есть замечания');
    expect(primStatusChip('errors')).toContain('class="chip failed"');
    expect(primStatusChip('странный')).toContain('class="chip unknown"');
  });

  it('ставка НДС, дата и итог загрузки — по-русски', () => {
    expect(primVatLabel('22')).toBe('22 %');
    expect(primVatLabel('22/122')).toBe('22/122');
    expect(primVatLabel('none')).toBe('без НДС');
    expect(primVatLabel('')).toBe('—');
    expect(primDay('2026-03-10')).toBe('10.03.2026');
    expect(primDay(null)).toBe('—');
    expect(primUploadSummary({ created: 2, duplicates: 1, rejected: 0 })).toBe('загружено 2, уже были 1');
    expect(primUploadSummary({})).toBe('нет документов');
  });

  it('замечания и строки экранируются', () => {
    const issues = primIssuesHtml([{ level: 'error', message: '<script>x</script>' }]);
    expect(issues).toContain('&lt;script&gt;');
    expect(issues).not.toContain('<script>');
    expect(issues).toContain('data-level="error"');
    expect(primIssuesHtml([])).toContain('Замечаний нет');
    const lines = primLinesHtml([{ position: 1, name: 'Брус <50×50>', quantity: 2.5, unit_name: 'м3', price: 100,
      sum_without_vat: 250, vat_rate: 'none', vat_sum: 0, sum_with_vat: 250, item_code: '' }]);
    expect(lines).toContain('Брус &lt;50×50&gt;');
    expect(lines).toContain('без НДС');
    expect(primLinesHtml([])).toContain('нет строк');
  });

  it('строки реестра: исправление, счётчики, пустой реестр', () => {
    const html = primRowsHtml(listPayload.items);
    expect(html).toContain('испр. № 1');
    expect(html).toContain('ошибок 1, замечаний 2');
    expect(html).toContain('ИП &lt;Петров&gt;');
    expect(primRowsHtml([])).toContain('Документов пока нет');
  });
});

describe('первичка: экран', () => {
  beforeEach(() => {
    calls.get.length = 0; calls.post.length = 0; calls.del.length = 0;
    document.body.innerHTML = `<div id="toast-container"></div><div id="view"></div>
      <div id="modal-root" class="hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div>`;
  });

  it('бухгалтер: реестр и итог проверки, без выбора компании', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, { isAdmin: false });
    expect(calls.get[0]).toMatch(/^\/api\/v1\/primary\?limit=50&offset=0$/);
    expect(view.querySelector('#prim-company')).toBeNull();
    expect(view.querySelector('#prim-out').innerHTML).toContain('УТ-15');
    expect(view.querySelector('#prim-kpi').textContent).toContain('с ошибками: 1');
  });

  it('администратор: компания из фильтра, загрузка с company_id, итог по файлам', async () => {
    const view = document.getElementById('view');
    const companies = [{ id: 'c1', name: 'Альфа', is_active: true }, { id: 'c2', name: 'Старая', is_active: false }];
    await viewPrimary(view, { isAdmin: true, companies, companyFilter: 'c1' });
    expect(calls.get[0]).toContain('company_id=c1');
    const sel = view.querySelector('#prim-company');
    expect([...sel.options].map((o) => o.value)).toEqual(['', 'c1']);
    expect(sel.value).toBe('c1');
    const input = view.querySelector('#prim-file');
    Object.defineProperty(input, 'files', { value: [new File(['<Файл/>'], 'a.xml')], configurable: true });
    input.onchange();
    await flush(); await flush();
    expect(calls.post).toHaveLength(1);
    const form = calls.post[0].body;
    expect(form.get('company_id')).toBe('c1');
    expect(form.getAll('files')).toHaveLength(1);
    const box = view.querySelector('#prim-results').innerHTML;
    expect(box).toContain('загружено 1, уже были 1, не принято 1');
    expect(box).toContain('уже был загружен');
    expect(box).toContain('не принят');
  });

  it('карточка: реквизиты, замечания, строки; удаление после подтверждения', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, {});
    view.querySelector('.prim-open').onclick();
    await flush();
    const slot = document.querySelector('.modal-slot').innerHTML;
    expect(slot).toContain('УПД № УТ-15 от 10.03.2026');
    expect(slot).toContain('ООО «Покупатель»');
    expect(slot).toContain('НДС &lt;не сходится&gt;');
    expect(slot).toContain('код CEM-500');
    window.confirm = vi.fn(() => true);
    await document.getElementById('prim-del').onclick();
    expect(calls.del).toEqual(['/api/v1/primary/d1']);
    expect(document.getElementById('modal-root').classList.contains('hidden')).toBe(true);
  });
});
