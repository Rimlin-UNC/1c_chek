import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';

const source = readFileSync('app/static/js/app.js', 'utf8');
const start = source.indexOf('async function viewReceipts(container)');
const end = source.indexOf('\n// Печать чеков на A4:', start);
const makeView = new Function('api', '$', '$$', 'state', 'isAccountant', 'companyIdParam',
  'fmtInt', 'fmtSum', 'emptyState', 'esc', 'toast',
  `${source.slice(start, end)}; return viewReceipts;`);

describe('фильтры реестра: сумма позиции и место расчёта', () => {
  it('передаются на сервер, сохраняются при перестроении и сбрасываются', async () => {
    document.body.innerHTML = '<main id="view"></main>';
    const calls = [];
    const api = { get: vi.fn(async url => {
      calls.push(url);
      if (url.includes('/facets')) return { retail_place_coverage: { saved: 2, total: 5 } };
      return { items: [], total: 0, total_sum: 0, page: 1, page_size: 50 };
    }) };
    const $ = sel => document.querySelector(sel);
    const $$ = (sel, el = document) => [...el.querySelectorAll(sel)];
    const view = makeView(api, $, $$, { receiptsSelected: new Set(), me: {} },
      () => false, () => 'company-1', String, String, () => 'пусто', String, vi.fn());
    const container = $('#view');
    await view(container);
    expect($('#f-place-coverage').textContent).toContain('2 из 5');
    $('#f-retail-place').value = 'ТД Вимос';
    $('#f-item-total-min').value = '1109';
    $('#f-item-total-max').value = '4655';
    $('#btn-filter').click();
    await Promise.resolve();
    const query = new URL(calls.filter(x => x.includes('/receipts?')).at(-1), 'http://test').searchParams;
    expect(query.get('retail_place')).toBe('ТД Вимос');
    expect(query.get('item_total_min')).toBe('1109');
    expect(query.get('item_total_max')).toBe('4655');
    expect(query.get('company_id')).toBe('company-1');
    await view(container);
    expect($('#f-retail-place').value).toBe('ТД Вимос');
    expect($('#f-item-total-min').value).toBe('1109');
    expect($('#f-item-total-max').value).toBe('4655');
    $('#btn-filter-reset').click();
    await Promise.resolve();
    expect($('#f-retail-place').value).toBe('');
    expect($('#f-item-total-min').value).toBe('');
    expect($('#f-item-total-max').value).toBe('');
    const reset = new URL(calls.filter(x => x.includes('/receipts?')).at(-1), 'http://test').searchParams;
    expect(reset.has('retail_place')).toBe(false);
    expect(reset.has('item_total_min')).toBe(false);
    expect(reset.has('item_total_max')).toBe(false);
  });
});
