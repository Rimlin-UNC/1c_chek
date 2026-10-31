import { describe, it, expect, vi } from 'vitest';
import { readFileSync } from 'node:fs';

const source = readFileSync('app/static/js/app.js', 'utf8');
const begin = source.indexOf('const poolPk = {');
const end = source.indexOf('\nasync function poolApiKeysLoad()', begin);
const create = new Function('api', '$', 'state', 'isAdmin', 'compName', 'esc', 'fmtInt', 'fmtSum', 'toast',
  `${source.slice(begin, end)}; return { viewPoolPick, poolPk };`);

const tick = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };

describe('подбор и перенос чеков', () => {
  it('передаёт фильтры, привязывает и переносит выбранный чек', async () => {
    document.body.innerHTML = '<main id="main"></main>';
    const calls = [];
    const row = { id: 'p1', date: '22.09.2026', merchant: 'ТД Вимос', inn: '', sum: 5764,
      city: 'Приозерск', region: '', industry: '', items: 'Гипсокартон' };
    const api = {
      get: vi.fn(async url => {
        calls.push(url);
        if (url.includes('/dicts')) return { regions: [], industries: [] };
        if (url.includes('/quota')) return { company: { name: 'А' }, used: 0, limit: 100, mine_total: 0, tariff: '' };
        return { items: [row], page: 1, per_page: 20, total: 1 };
      }),
      post: vi.fn(async (url, body) => { calls.push([url, body]); return { message: 'Готово', unconfirmed: 1 }; }),
    };
    const $ = sel => document.querySelector(sel);
    const state = { companies: [] };
    const { viewPoolPick } = create(api, $, state, () => false, x => x.name, String, String, String, vi.fn());
    await viewPoolPick($('#main'));
    await tick();
    $('#pk-item').value = 'Гипсокартон';
    $('#pk-item-min').value = '4655';
    $('#pk-vat').value = '11';
    $('#pk-go').click();
    await tick();
    const search = new URL(calls.filter(x => typeof x === 'string' && x.includes('/search?')).at(-1), 'http://test');
    expect(search.searchParams.get('item_name')).toBe('Гипсокартон');
    expect(search.searchParams.get('item_total_min')).toBe('4655');
    expect(search.searchParams.get('vat_rate')).toBe('11');
    const checkbox = $('#poolpk-body input[type=checkbox]');
    checkbox.checked = true;
    checkbox.dispatchEvent(new Event('change'));
    $('#pk-assign').click();
    await tick();
    expect(api.post.mock.calls[0][0]).toContain('/assign');
    $('#poolpk-tab-mine').click();
    await tick();
    const mine = $('#poolpk-body input[type=checkbox]');
    mine.checked = true;
    mine.dispatchEvent(new Event('change'));
    $('#pk-transfer').click();
    await tick();
    expect(api.post.mock.calls[1]).toEqual(['/api/v1/pool-company/transfer', { receipt_ids: ['p1'] }]);
  });
});
