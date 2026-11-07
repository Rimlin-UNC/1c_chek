/* v1.70.114 (T-62, #47, ADR-45): окно «Печатная форма УПД» — состав полей, подстановка значений
   документа и сохранённых правок, тело запроса PUT /print-form. API подменён, данные синтетические. */
import { describe, expect, it, vi } from 'vitest';

vi.mock('../../app/static/js/api.js', () => ({ api: { get: vi.fn(), put: vi.fn() } }));

const { PRIM_PRINT_FIELDS, primPrintBody, primPrintHtml } = await import('../../app/static/js/primary.js');

// Подделка окна: get(id) возвращает элемент с value, как DOM.
const fieldsFrom = (values) => {
  const nodes = {};
  for (const f of PRIM_PRINT_FIELDS) nodes[`prim-pf-${f.key}`] = { value: values[f.key] ?? '' };
  return (id) => nodes[id];
};

const SAMPLE = {
  title: 'УПД № УТ-215 от 14.10.2026',
  defaults: { status: '1', shipper: 'он же', consignee: 'ООО «Ромашка-Строй»', sheets: '1',
    shipment_doc: '№ п/п 1–2 № УТ-215 от 14 октября 2026 г.', currency: 'Российский рубль, 643',
    basis: '', seller_address: '', buyer_address: '', payment_doc: '—', contract_id: '',
    signer_chief_role: 'Руководитель организации или иное уполномоченное лицо', signer_chief: '',
    signer_accountant_role: 'Главный бухгалтер или иное уполномоченное лицо', signer_accountant: '',
    signer_entrepreneur: '' },
  saved: {},
  form: { status: '1', shipper: 'он же', consignee: 'ООО «Ромашка-Строй»', sheets: '1', basis: '',
    shipment_doc: '№ п/п 1–2 № УТ-215 от 14 октября 2026 г.', currency: 'Российский рубль, 643' },
};

describe('PRIM_PRINT_FIELDS', () => {
  it('включает все поля бланка и статус списком', () => {
    const keys = PRIM_PRINT_FIELDS.map((f) => f.key);
    expect(keys).toContain('status');
    expect(keys).toContain('basis');
    expect(keys).toContain('signer_chief');
    expect(keys).toContain('signer_entrepreneur');
    expect(keys).toHaveLength(16);
    expect(new Set(keys).size).toBe(keys.length);
    const status = PRIM_PRINT_FIELDS.find((f) => f.key === 'status');
    expect(status.options.map((o) => o.value)).toEqual(['1', '2']);
  });
});

describe('primPrintHtml', () => {
  it('подставляет значения документа и сохранённые правки', () => {
    const html = primPrintHtml(SAMPLE);
    expect(html).toContain('Печатная форма УПД: УПД № УТ-215 от 14.10.2026');
    expect(html).toContain('id="prim-pf-status"');
    expect(html).toContain('id="prim-pf-basis"');
    expect(html).toContain('value="№ п/п 1–2 № УТ-215 от 14 октября 2026 г."');
    expect(html).toContain('value="1"');                       // число листов
    expect(html).toContain('id="prim-print-go"');
    expect(html).toContain('«Печать / PDF»');
    expect(html).toContain('Сохранить как PDF');
  });

  it('помечает статус 2 и сохранённые правки', () => {
    const html = primPrintHtml({ ...SAMPLE, saved: { basis: 'Договор № 7' },
      form: { ...SAMPLE.form, status: '2', basis: 'Договор № 7' } });
    expect(html).toMatch(/<option value="2" selected>/);
    expect(html).toContain('value="Договор № 7"');
    expect(html).toContain('сохранённые правки подставлены');
  });

  it('экранирует значения из документа', () => {
    const html = primPrintHtml({ ...SAMPLE, form: { ...SAMPLE.form, basis: '<script>alert(1)</script>' } });
    expect(html).not.toContain('<script>alert(1)</script>');
    expect(html).toContain('&lt;script&gt;alert(1)&lt;/script&gt;');
  });

  it('не падает без данных', () => {
    const html = primPrintHtml(null);
    expect(html).toContain('Печатная форма УПД');
    expect(html).toContain('id="prim-pf-status"');
  });
});

describe('primPrintBody', () => {
  it('собирает все поля тела запроса и обрезает пробелы', () => {
    const values = {};
    for (const f of PRIM_PRINT_FIELDS) values[f.key] = f.key === 'status' ? '2' : ` ${f.key} `;
    const body = primPrintBody(fieldsFrom(values));
    expect(Object.keys(body).sort()).toEqual(PRIM_PRINT_FIELDS.map((f) => f.key).sort());
    expect(body.status).toBe('2');
    expect(body.basis).toBe('basis');
    expect(body.sheets).toBe('sheets');
  });

  it('пустой статус и число листов — по умолчанию', () => {
    const body = primPrintBody(fieldsFrom({ status: '', sheets: '' }));
    expect(body.status).toBe('1');
    expect(body.sheets).toBe('1');
    expect(body.basis).toBe('');
  });

  it('не падает, если в окне нет поля', () => {
    const body = primPrintBody(() => null);
    expect(body.status).toBe('1');
    expect(body.signer_chief).toBe('');
  });
});
