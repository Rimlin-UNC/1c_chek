import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../app/static/js/api.js', () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock('../../app/static/js/ui.js', () => ({
  esc: value => String(value ?? '').replaceAll('&', '&amp;').replaceAll('<', '&lt;'),
  fmtDate: value => String(value || ''), fmtInt: value => String(value), toast: vi.fn(),
}));
import { api } from '../../app/static/js/api.js';
import { osmCardHtml, osmWire, osmStop } from '../../app/static/js/osm.js';

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

describe('мониторинг с ограниченными правами', () => {
  beforeEach(() => { osmStop(); vi.clearAllMocks(); document.body.innerHTML = osmCardHtml({}, {}); });

  it('не рисует недоступные показатели и квоты нулями и объясняет частичный сбор', async () => {
    api.get.mockImplementation(async path => path.includes('/current') ? {
      available: true, cpu: { total: null, used: null, pct: null },
      ram: { total_mb: null, used_mb: null, free_mb: null, pct: null },
      disk: { total_gb: null, used_gb: null, free_gb: null, pct: null },
      quotas: { used_cores: 0, max_cores: -1 },
      notes: { hypervisor_error: 'Недоступно' },
    } : { points: [], thresholds: {} });
    osmWire(); document.querySelector('#osm-tab-metrics').click(); await flush();
    const cards = document.querySelector('#osm-kpi').textContent;
    expect(cards).not.toContain('0,00 %');
    expect(cards).toContain('без лимита');
    expect(cards).toContain('0 / без лимита');
    expect(document.querySelector('#osm-warnings').textContent).toMatch(/гипервизор/i);
    osmStop();
  });

  it('отсутствующая точка истории остаётся пропуском, а график восстанавливается после пустой истории', async () => {
    let points = [];
    api.get.mockImplementation(async path => path.includes('/current') ? { available: true } : { points, thresholds: {} });
    const chart = vi.fn(function (_canvas, config) { this.config = config; this.destroy = vi.fn(); });
    window.Chart = chart;
    const getContext = vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({});
    try {
      osmWire(); document.querySelector('#osm-tab-metrics').click(); await flush();
      points = [{ collected_at: '2026-01-01T12:00:00Z', vcpus_total: null, vcpus_used: null }];
      document.querySelector('#osm-refresh').click(); await flush();
      expect(chart).toHaveBeenCalled();
      expect(chart.mock.calls[0][1].data.datasets[0].data).toEqual([null]);
    } finally { osmStop(); getContext.mockRestore(); delete window.Chart; }
  });
});
