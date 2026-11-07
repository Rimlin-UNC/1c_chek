// Второе прочтение сканов и фото (T-77, v1.70.109, ADR-39): строка в панели распознавания, режим
// администратора и «чем распознано» в окне сверки. Чистые функции и экран в jsdom; API подменён.
import { beforeEach, describe, expect, it, vi } from 'vitest';

const calls = { get: [], post: [], put: [] };
const ready = (mode = 'auto', extra = {}) => ({
  ready: true, engine: 'tesserocr', missing: [], reason: '', last: { seconds: 1.7, peak_mb: 182 }, second_mode: mode,
  second: { ready: true, modules: true, missing: [], reason: '', model: 'PP-OCRv6 Tiny Cyrillic', license: 'Apache-2.0',
    last: { seconds: 2.5, peak_mb: 510 }, ...extra },
});
const state = { ocr: ready() };

vi.mock('../../app/static/js/api.js', () => ({
  api: {
    get: vi.fn(async (path) => {
      calls.get.push(path);
      if (path === '/api/v1/primary/ocr') return JSON.parse(JSON.stringify(state.ocr));
      return { items: [], total: 0, summary: { ok: 0, warnings: 0, errors: 0 } };
    }),
    post: vi.fn(async (path, body) => { calls.post.push({ path, body }); return { created: 0, results: [] }; }),
    put: vi.fn(async (path, body) => {
      calls.put.push({ path, body });
      state.ocr = ready(body.mode);
      return state.ocr;
    }),
    del: vi.fn(async () => ({ ok: true })),
  },
}));

const mod = await import('../../app/static/js/primary.js');
const { PRIM_SECOND_MODES, primOcrStatusHtml, primOcrUsedHtml, primSecondHtml, viewPrimary } = mod;
const flush = () => new Promise((r) => setTimeout(r, 0));
const settle = async (n = 6) => { for (let i = 0; i < n; i += 1) await flush(); };

describe('второе прочтение — чистые функции', () => {
  it('готово: режим словами, последний замер, список только администратору', () => {
    const admin = primSecondHtml(ready(), true);
    expect(admin).toContain('🔁');
    expect(admin).toContain('Второе прочтение (PP-OCRv6, Apache-2.0): для черновиков, где Tesseract не нашёл поле или сомневается.');
    expect(admin).toContain('Последнее: 2,5 с, память 510 МБ.');
    expect(admin).toContain('id="prim-ocr-second"');
    expect(admin).toContain('<option value="auto" selected>по сомнению</option>');
    expect(Object.keys(PRIM_SECOND_MODES)).toEqual(['auto', 'always', 'off']);
    expect(primSecondHtml(ready(), false)).not.toContain('prim-ocr-second');
    expect(primSecondHtml(ready('always'), false)).toContain('для каждого скана');
    expect(primSecondHtml(ready('off'), true)).toContain('выключено.');
    expect(primSecondHtml(ready('странный'), true)).toContain('<option value="auto" selected>');
  });

  it('не настроено: причина, кнопка модели — администратору и только когда модули есть', () => {
    const noModel = ready('auto', { ready: false, missing: ['inference.onnx', 'rec_keys.txt'], reason: 'нет <модели>' });
    expect(primSecondHtml(noModel, true)).toContain('Второе прочтение: не настроено — нет &lt;модели&gt;.');
    expect(primSecondHtml(noModel, true)).toContain('Скачать модель (2,5 МБ)');
    expect(primSecondHtml(noModel, false)).not.toContain('Скачать модель');
    const noModules = ready('auto', { ready: false, modules: false, missing: ['inference.onnx'], reason: 'не установлены модули' });
    expect(primSecondHtml(noModules, true)).not.toContain('Скачать модель');
    expect(primSecondHtml({ ready: true }, true)).toBe('');
  });

  it('панель целиком: строка второго прочтения под Tesseract; одна кнопка на обе модели', () => {
    const html = primOcrStatusHtml(ready(), true);
    expect(html).toContain('Распознавание сканов и фото: готово. Последний скан: 1,7 с, память 182 МБ.');
    expect(html).toContain('Второе прочтение (PP-OCRv6');
    const off = { ready: false, engine: 'tesserocr', missing: ['rus'], reason: 'нет языковых моделей: rus' };
    expect(primOcrStatusHtml(off, true)).toContain('Скачать модели (8 МБ)');
    const both = { ...off, second: { ready: false, modules: true, missing: ['inference.onnx'], reason: 'нет модели' } };
    expect(primOcrStatusHtml(both, true)).toContain('Скачать модели (11 МБ)');
    expect(primOcrStatusHtml(both, true).match(/id="prim-ocr-models"/g)).toHaveLength(1);
  });

  it('окно сверки: чем распознано — Tesseract, второе прочтение или причина', () => {
    expect(primOcrUsedHtml(null)).toBe('');
    expect(primOcrUsedHtml({ engine: 'Tesseract 5.5.3', seconds: 1.7 })).toContain('Распознано на сервере: Tesseract 5.5.3, 1,7 с.');
    const both = primOcrUsedHtml({ engine: 'Tesseract 5.5.3', seconds: 1.7, second: { engine: 'PP-OCRv6', seconds: 2.5 } });
    expect(both).toContain('второе прочтение (PP-OCRv6) — 2,5 с, поля сведены из двух прочтений.');
    const failed = primOcrUsedHtml({ engine: 'Tesseract', seconds: 1.7, second: { error: 'Сервер <занят>' } });
    expect(failed).toContain('Второе прочтение не выполнено: Сервер &lt;занят&gt;.');
  });
});

describe('второе прочтение — экран', () => {
  beforeEach(() => {
    calls.get.length = 0;
    calls.post.length = 0;
    calls.put.length = 0;
    state.ocr = ready();
    document.body.innerHTML = `<div id="toast-container"></div><div id="view"></div>
      <div id="modal-root" class="hidden"><div class="modal-backdrop"></div><div class="modal-slot"></div></div>`;
  });

  it('администратор меняет режим — запрос PUT, панель перерисована', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, { isAdmin: true, companies: [] });
    await settle();
    const select = view.querySelector('#prim-ocr-second');
    expect(select).not.toBeNull();
    select.value = 'always';
    await select.onchange();
    await settle();
    expect(calls.put).toEqual([{ path: '/api/v1/primary/ocr/second', body: { mode: 'always' } }]);
    expect(view.querySelector('#prim-ocr').innerHTML).toContain('для каждого скана');
    expect(view.querySelector('#prim-ocr-second').value).toBe('always');
  });

  it('бухгалтер видит режим, но не меняет его', async () => {
    const view = document.getElementById('view');
    await viewPrimary(view, { isAdmin: false, companies: [] });
    await settle();
    expect(view.querySelector('#prim-ocr').innerHTML).toContain('Второе прочтение (PP-OCRv6');
    expect(view.querySelector('#prim-ocr-second')).toBeNull();
  });
});
