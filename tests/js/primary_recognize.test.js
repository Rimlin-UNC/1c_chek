// «Комбайн первички» (T-61, v1.70.95): распознавание PDF, сканов и фото, окно сверки, ручной ввод.
// Чистые функции и сам экран в jsdom; API подменён (только синтетика: ИНН с верной контрольной суммой).
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [] };
const state = { ocr: { ready: false, engine: 'tesserocr', missing: ['rus', 'eng'], reason: 'нет языковых моделей: rus, eng' },
  recognizeError: null };
const recognized = {
  source: 'pdf', file_name: 'upd.pdf', file_sha256: 'a'.repeat(64), token: '123.abc', company_id: 'c1',
  draft: { function: 'СЧФДОП', number: 'УТ-215', date: '2026-10-14', correction_number: '', correction_date: null,
    currency: '643', seller: { kind: 'ЮЛ', name: 'Строи <База>', inn: '5001007329', kpp: '500101001' },
    buyer: { kind: 'ЮЛ', name: 'ООО «Ромашка-Строй»', inn: '7704123450', kpp: '7770401091' },
    totals: { without_vat: 5000, vat: 1100, with_vat: 6100 },
    lines: [{ position: 1, name: 'Цемент М500', quantity: 10, unit_name: 'шт', price: 500, sum_without_vat: 5000,
      vat_rate: '22', vat_sum: 1100, sum_with_vat: 6100, item_code: 'CEM-500' }] },
  found: ['number', 'date', 'seller_name', 'seller_inn', 'seller_kpp', 'buyer_name', 'buyer_inn', 'buyer_kpp', 'lines', 'totals'],
  uncertain: ['buyer_kpp'], issues: [], duplicate: null,
  hints: { seller: { name: 'ООО «СтройБаза»', kpp: '500101001', from: 'прошлые документы' } }, ocr: null, preview: null,
};

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path === '/api/v1/primary/ocr') return state.ocr;
      return { items: [], total: 0, summary: { ok: 0, warnings: 0, errors: 0 } };
    }),
    post: vi.fn(async (path, body, opts) => {
      calls.post.push({ path, body, opts });
      if (path.endsWith('/recognize')) {
        if (state.recognizeError) throw new Error(state.recognizeError);
        return JSON.parse(JSON.stringify(recognized));
      }
      if (path.endsWith('/ocr/models')) {
        state.ocr = { ready: true, engine: 'tesserocr', missing: [], reason: '', last: {} };
        return state.ocr;
      }
      if (path.endsWith('/confirm')) {
        return { result: 'created', document: { kind: 'УПД', doc_number: body.draft.number, doc_date: body.draft.date } };
      }
      return { created: 0, duplicates: 0, rejected: 0, results: [] };
    }),
    del: vi.fn(async () => ({ ok: true })),
  },
}));

const mod = await import('../../app/static/js/primary.js');
const { primDraftChecks, primInnValid, primIssuesHtml, primKppValid, primMoneyField, primOcrStatusHtml, primParseMoney,
  primReadDraft, primReviewHtml, primSourceLabel, primSplitFiles, primRowsHtml, viewPrimary } = mod;

const flush = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 6) => { for (let i = 0; i < n; i += 1) await flush(); };

describe('первичка: распознавание — чистые функции', () => {
  it('ИНН и КПП проверяются как на сервере', () => {
    expect(primInnValid('5001007329')).toBe(true);
    expect(primInnValid('5001007320')).toBe(false);
    expect(primInnValid('500100732')).toBe(false);
    expect(primInnValid('')).toBe(false);
    expect(primKppValid('770401001')).toBe(true);
    expect(primKppValid('7770401091')).toBe(false);
    expect(primKppValid('7704AB001')).toBe(true);
  });

  it('суммы: бумажный вид в число и обратно', () => {
    expect(primParseMoney('1 100,00')).toBe(1100);
    expect(primParseMoney('105\u00a0530,5')).toBe(105530.5);
    expect(primParseMoney('')).toBeNull();
    expect(Number.isNaN(primParseMoney('сто'))).toBe(true);
    expect(primMoneyField(1100)).toBe('1100,00');
    expect(primMoneyField(null)).toBe('');
  });

  it('файлы: XML и ZIP — в реестр, PDF и картинки — на распознавание', () => {
    const f = (name) => ({ name });
    const { structured, printed } = primSplitFiles([f('a.xml'), f('b.ZIP'), f('c.pdf'), f('d.JPG'), f('e.png'),
      f('g.tiff'), f('h.txt')]);
    expect(structured.map((x) => x.name)).toEqual(['a.xml', 'b.ZIP', 'h.txt']);
    expect(printed.map((x) => x.name)).toEqual(['c.pdf', 'd.JPG', 'e.png', 'g.tiff']);
    expect(primSourceLabel('scan')).toBe('скан или фото');
    expect(primSourceLabel(undefined)).toBe('файл ЭДО');
  });

  it('быстрые проверки: обязательные поля, ИНН, арифметика', () => {
    const empty = { number: '', date: null, seller: { name: '', inn: '', kpp: '' }, buyer: { name: '', inn: '', kpp: '' },
      totals: { without_vat: null, vat: null, with_vat: null }, lines: [] };
    expect(primDraftChecks(empty).map((c) => c.message)).toEqual(['Укажите номер документа', 'Укажите дату документа',
      'Укажите продавца', 'Укажите ИНН продавца']);
    const bad = { ...empty, number: '1', date: '2026-10-01', seller: { name: 'А', inn: '5001007320', kpp: '' },
      buyer: { name: 'Б', inn: '7704123450', kpp: '77040100' }, totals: { without_vat: 100, vat: 22, with_vat: 120 } };
    const messages = primDraftChecks(bad).map((c) => `${c.level}: ${c.message}`);
    expect(messages).toContain('error: ИНН продавца 5001007320 — неверная контрольная сумма');
    expect(messages).toContain('warning: КПП покупателя 77040100 — нужно 9 знаков');
    expect(messages.some((m) => m.startsWith('warning: Без НДС'))).toBe(true);
  });

  it('состояние распознавания: готово, не настроено, кнопка только администратору', () => {
    expect(primOcrStatusHtml({ ready: true, last: { seconds: 1.6, peak_mb: 89 } }, false)).toContain('1,6 с, память 89 МБ');
    const off = { ready: false, engine: 'tesserocr', missing: ['rus'], reason: 'нет <моделей>' };
    expect(primOcrStatusHtml(off, true)).toContain('id="prim-ocr-models"');
    expect(primOcrStatusHtml(off, true)).toContain('нет &lt;моделей&gt;');
    expect(primOcrStatusHtml(off, false)).not.toContain('prim-ocr-models');
    expect(primOcrStatusHtml({ ...off, engine: '' }, true)).not.toContain('prim-ocr-models');
    expect(primOcrStatusHtml({ id: 'd1' }, true)).toBe('');
  });

  it('окно сверки: пометки «проверьте», подсказка, дубль, превью', () => {
    const html = primReviewHtml({ ...recognized, duplicate: { kind: 'УПД', doc_number: 'УТ-215', doc_date: '2026-10-14' } },
      { previewUrl: 'data:image/jpeg;base64,AAA' });
    expect(html).toContain('Проверьте внимательно: КПП покупателя');
    expect(html).toContain('class="field prim-uncertain"');
    expect(html).toContain('Строи &lt;База&gt;');
    expect(html).toContain('id="prim-hint-seller"');
    expect(html).toContain('Такой документ уже есть в реестре: УПД № УТ-215 от 14.10.2026');
    expect(html).toContain('<img src="data:image/jpeg;base64,AAA"');
    expect(html).toContain('Сохранить строки (1)');
    const partial = primReviewHtml({ ...recognized, found: ['number'], uncertain: [], hints: {} });
    expect(partial).toContain('Не найдено: дата, продавец');
    expect(partial).toContain('prim-missing');
    const manual = primReviewHtml({ source: 'manual', draft: { seller: {}, buyer: {}, totals: {}, lines: [] } });
    expect(manual).toContain('Документ вручную');
    expect(manual).not.toContain('prim-missing');
    expect(manual).not.toContain('prim-skip');
  });

  it('черновик из полей окна: цифры ИНН, суммы, строки по галочке', () => {
    const values = { 'prim-f-function': { value: 'ДОП' }, 'prim-f-number': { value: ' УТ-9 ' }, 'prim-f-date': { value: '2026-10-01' },
      'prim-f-seller-inn': { value: '5001 0073 29' }, 'prim-f-seller-kpp': { value: '5001ab001' }, 'prim-f-base': { value: '1 000,00' },
      'prim-f-vat': { value: '220' }, 'prim-f-total': { value: '' }, 'prim-f-lines': { checked: false } };
    const d = primReadDraft((id) => values[id], recognized);
    expect(d.function).toBe('ДОП');
    expect(d.number).toBe('УТ-9');
    expect(d.seller.inn).toBe('5001007329');
    expect(d.seller.kpp).toBe('5001AB001');
    expect(d.totals).toEqual({ without_vat: 1000, vat: 220, with_vat: null });
    expect(d.lines).toEqual([]);
    values['prim-f-lines'].checked = true;
    expect(primReadDraft((id) => values[id], recognized).lines).toHaveLength(1);
  });

  it('замечания: только сведения — «замечаний нет», подробности схемы раскрываются', () => {
    const info = primIssuesHtml([{ level: 'info', code: 'xsd_ok', message: 'Файл соответствует схеме ФНС 5.03' }]);
    expect(info).toContain('Замечаний нет');
    expect(info).toContain('Файл соответствует схеме ФНС 5.03');
    const xsd = primIssuesHtml([{ level: 'warning', message: 'Не по схеме', details: ['Файл › Документ: нет <Подписант>'] }]);
    expect(xsd).not.toContain('Замечаний нет');
    expect(xsd).toContain('<summary>Подробно (1)</summary>');
    expect(xsd).toContain('нет &lt;Подписант&gt;');
    expect(primRowsHtml([{ id: 'x', doc_number: '1', source: 'scan', seller: {}, status: 'ok' }])).toContain('скан или фото');
  });
});

describe('первичка: распознавание — экран', () => {
  beforeEach(() => {
    calls.get.length = 0;
    calls.post.length = 0;
    state.recognizeError = null;
    state.ocr = { ready: false, engine: 'tesserocr', missing: ['rus', 'eng'], reason: 'нет языковых моделей: rus, eng' };
    document.body.innerHTML = `<div id="toast-container"></div><div id="view"></div>
      <div id="modal-root" class="hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div>`;
  });

  it('администратор скачивает модели — статус обновляется', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, { isAdmin: true, companies: [] });
    await settle();
    expect(calls.get[0]).toMatch(/^\/api\/v1\/primary\?/);
    const btn = view.querySelector('#prim-ocr-models');
    expect(btn).not.toBeNull();
    await btn.onclick();
    await settle();
    expect(calls.post.map((c) => c.path)).toEqual(['/api/v1/primary/ocr/models']);
    expect(view.querySelector('#prim-ocr').innerHTML).toContain('готово');
  });

  it('PDF: распознан, исправлен номер, подставлен продавец, сохранён со строками', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, { isAdmin: false });
    const input = view.querySelector('#prim-file');
    Object.defineProperty(input, 'files', { value: [new File(['%PDF'], 'upd.pdf', { type: 'application/pdf' })], configurable: true });
    input.onchange();
    await settle();
    const rec = calls.post.find((c) => c.path === '/api/v1/primary/recognize');
    expect(rec.body.get('file').name).toBe('upd.pdf');
    expect(rec.opts).toEqual({ retries: 1 });
    const slot = document.querySelector('.modal-slot');
    expect(slot.classList.contains('prim-modal')).toBe(true);
    expect(slot.querySelector('#prim-check').innerHTML).toContain('КПП покупателя 7770401091');
    slot.querySelector('#prim-hint-seller').onclick();
    expect(slot.querySelector('#prim-f-seller-name').value).toBe('ООО «СтройБаза»');
    slot.querySelector('#prim-f-buyer-kpp').value = '770401001';
    slot.querySelector('#prim-f-number').value = 'УТ-215/1';
    slot.oninput();
    expect(slot.querySelector('#prim-check').innerHTML).toContain('Обязательные реквизиты на месте');
    await slot.querySelector('#prim-save').onclick();
    await settle();
    const saved = calls.post.find((c) => c.path === '/api/v1/primary/confirm').body;
    expect(saved).toMatchObject({ source: 'pdf', token: '123.abc', file_sha256: 'a'.repeat(64), company_id: 'c1' });
    expect(saved.draft.number).toBe('УТ-215/1');
    expect(saved.draft.seller).toEqual({ name: 'ООО «СтройБаза»', inn: '5001007329', kpp: '500101001' });
    expect(saved.draft.lines).toHaveLength(1);
    expect(document.getElementById('modal-root').classList.contains('hidden')).toBe(true);
    expect(view.querySelector('#prim-results').textContent).toContain('«upd.pdf»: сохранён в реестр');
  });

  it('пропуск файла и ошибка распознавания — без сохранения', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, {});
    const input = view.querySelector('#prim-file');
    Object.defineProperty(input, 'files', { value: [new File(['x'], 'scan.jpg', { type: 'image/jpeg' })], configurable: true });
    input.onchange();
    await settle();
    document.querySelector('#prim-skip').onclick();
    await settle();
    expect(view.querySelector('#prim-results').textContent).toContain('«scan.jpg»: пропущен');
    state.recognizeError = 'Распознавание сканов не настроено: нет языковых моделей';
    input.onchange();
    await settle();
    expect(view.querySelector('#prim-results').innerHTML).toContain('не настроено');
    expect(calls.post.some((c) => c.path.endsWith('/confirm'))).toBe(false);
  });

  it('ручной ввод: «Сохранить» доступна после обязательных полей', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, {});
    view.querySelector('#prim-manual').onclick();
    await settle();
    const slot = document.querySelector('.modal-slot');
    expect(slot.querySelector('#prim-save').disabled).toBe(true);
    slot.querySelector('#prim-f-number').value = 'Б-7';
    slot.querySelector('#prim-f-date').value = '2026-10-20';
    slot.querySelector('#prim-f-seller-name').value = 'ООО «Песок»';
    slot.querySelector('#prim-f-seller-inn').value = '5001007329';
    slot.querySelector('#prim-f-base').value = '1000';
    slot.querySelector('#prim-f-vat').value = '220';
    slot.querySelector('#prim-f-total').value = '1220';
    slot.oninput();
    expect(slot.querySelector('#prim-save').disabled).toBe(false);
    await slot.querySelector('#prim-save').onclick();
    await settle();
    const saved = calls.post.find((c) => c.path === '/api/v1/primary/confirm').body;
    expect(saved.source).toBe('manual');
    expect(saved.draft).toMatchObject({ number: 'Б-7', date: '2026-10-20', totals: { without_vat: 1000, vat: 220, with_vat: 1220 },
      lines: [] });
  });
});
