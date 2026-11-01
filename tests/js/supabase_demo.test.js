import { describe, expect, it, vi } from 'vitest';
import { filterSnapshot, snapshotTotals, viewSupabaseDemo } from '../../app/static/js/supabase_demo.js';
import { api } from '../../app/static/js/api.js';

vi.mock('../../app/static/js/api.js', () => ({ api: { get: vi.fn() } }));

const snapshot = {
  mode: 'read_only_demo', generated_at: '2026-11-01T00:00:00Z',
  companies: [{ id: 'c1', name: 'Альфа' }, { id: 'c2', name: 'Бета' }],
  participants: [{ id: 'p1', company_id: 'c1', label: 'Участник A' }],
  receipts: [
    { id: 'r1', company_id: 'c1', participant_id: 'p1', receipt_no: 'D1', total: 100, status: 'verified', company_name: 'Альфа', participant_label: 'Участник A', receipt_date: '2026-10-20', merchant_name: 'Магазин' },
    { id: 'r2', company_id: 'c2', participant_id: null, receipt_no: 'D2', total: 200, status: 'new', company_name: 'Бета', participant_label: 'Без участника', receipt_date: '2026-10-21', merchant_name: 'Кафе' },
  ],
  items: [{ receipt_id: 'r1', name: 'Товар', quantity: 1 }],
};

describe('Supabase демо', () => {
  it('фильтры и суммы считаются по снимку', () => {
    expect(filterSnapshot(snapshot, { company: 'c1' })).toHaveLength(1);
    expect(filterSnapshot(snapshot, { participant: '' })).toHaveLength(2);
    expect(filterSnapshot(snapshot, { participant: 'p1' })[0].id).toBe('r1');
    expect(snapshotTotals(filterSnapshot(snapshot, {}))).toEqual({ count: 2, sum: 300 });
  });

  it('рендерит read-only снимок и не отправляет запись', async () => {
    api.get.mockResolvedValue({ enabled: true, url: 'https://example.supabase.co', publishable_key: 'sb_publishable_test', rpc: 'chek_demo_snapshot' });
    const fetchMock = vi.fn(async () => ({ ok: true, json: async () => snapshot }));
    vi.stubGlobal('fetch', fetchMock);
    document.body.innerHTML = '<main id="view"></main>';
    await viewSupabaseDemo(document.querySelector('#view'));
    expect(document.body.textContent).toContain('Supabase демо');
    expect(document.body.textContent).toContain('read-only');
    expect(document.body.textContent).toContain('D1');
    expect(fetchMock).toHaveBeenCalledTimes(1);
    const [url, opts] = fetchMock.mock.calls[0];
    expect(url).toBe('https://example.supabase.co/rest/v1/rpc/chek_demo_snapshot');
    expect(opts.method).toBe('POST');
    expect(opts.headers.apikey).toBe('sb_publishable_test');
    expect(opts.body).toBe('{}');
    document.querySelector('#sb-demo-company').value = 'c1';
    document.querySelector('#sb-demo-company').dispatchEvent(new Event('change'));
    expect(document.querySelectorAll('tbody tr')).toHaveLength(1);
    expect(document.body.textContent).toContain('100,00');
    vi.unstubAllGlobals();
  });

  it('показывает ошибку Supabase без падения приложения', async () => {
    api.get.mockResolvedValue({ enabled: true, url: 'https://example.supabase.co', publishable_key: 'sb_publishable_test', rpc: 'chek_demo_snapshot' });
    vi.stubGlobal('fetch', vi.fn(async () => ({ ok: false, status: 503 })));
    document.body.innerHTML = '<main id="view"></main>';
    await viewSupabaseDemo(document.querySelector('#view'));
    expect(document.body.textContent).toContain('Supabase ответил 503');
    vi.unstubAllGlobals();
  });
});
