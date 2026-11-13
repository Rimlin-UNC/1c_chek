import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../app/static/js/api.js', () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock('../../app/static/js/ui.js', () => ({
  esc: value => String(value ?? ''), fmtDate: value => String(value || ''),
  fmtInt: value => String(value), toast: vi.fn(),
}));
import { api } from '../../app/static/js/api.js';
import { localServerCardHtml, localServerWire, localServerStop } from '../../app/static/js/server_monitor.js';

const flush = () => new Promise(resolve => setTimeout(resolve, 0));

describe('мониторинг сервера без облака', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });

  it('показывает показатели по запросу без настроек и запросов OpenStack', async () => {
    api.get.mockResolvedValue({ cpu_pct: 25, logical_cpus: 4, container_hint: false,
      memory: { total_bytes: 8388608, used_bytes: 5242880, available_bytes: 3145728, pct: 62.5 },
      disk: { total_bytes: 409600, used_bytes: 245760, available_bytes: 122880, pct: 60 } });
    localServerWire();
    expect(api.get).not.toHaveBeenCalled();
    document.querySelector('#server-monitor-refresh').click(); await flush();
    const local = document.querySelector('#server-monitor-kpi');
    expect(local.textContent).toContain('25,00 %');
    expect(local.textContent).toContain('62,50 %');
    expect(local.textContent).toContain('60,00 %');
    expect(local.textContent).toContain('4');
    expect(document.querySelector('#server-monitor-note').textContent).toMatch(/истори/i);
    expect(api.get).toHaveBeenCalledTimes(1);
    expect(api.get).toHaveBeenCalledWith('/api/v1/admin/server-monitor');
    localServerStop();
  });

  it('не перерисовывает новый экран опоздавшим ответом и не дублирует запрос', async () => {
    let resolve;
    api.get.mockReturnValue(new Promise(r => { resolve = r; }));
    localServerWire();
    document.querySelector('#server-monitor-refresh').click();
    document.querySelector('#server-monitor-refresh').click();
    expect(api.get).toHaveBeenCalledTimes(1);
    localServerStop();
    document.body.innerHTML = localServerCardHtml();
    resolve({ cpu_pct: 42 }); await flush();
    expect(document.querySelector('#server-monitor-kpi').textContent).not.toContain('42');
  });

  it('показывает прочерк вместо нуля, объясняет контейнер и сообщает ошибки', async () => {
    api.get.mockResolvedValueOnce({ cpu_pct: null, memory: null, disk: null, container_hint: true });
    localServerWire(); document.querySelector('#server-monitor-refresh').click(); await flush();
    expect(document.querySelector('#server-monitor-kpi').textContent).toContain('—');
    expect(document.querySelector('#server-monitor-kpi').textContent).not.toContain('0,00 %');
    expect(document.querySelector('#server-monitor-note').textContent).toMatch(/контейнер/i);
    api.get.mockRejectedValueOnce(new Error('нет связи'));
    document.querySelector('#server-monitor-refresh').click(); await flush();
    expect(document.querySelector('#server-monitor-status').textContent).toMatch(/нет связи/i);
    localServerStop();
  });
});

describe('расширенный локальный снимок', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  it('показывает load, swap и сетевые интерфейсы без ложного месячного итога', async () => {
    api.get.mockResolvedValue({ cpu_pct: 20, logical_cpus: 2, load_average: [0.1, 0.2, 0.3], uptime_sec: 7200,
      cpu_detail: { user_pct: 10, system_pct: 5, iowait_pct: 2, steal_pct: 1 },
      memory: null, disk: null, swap: { total_bytes: 1024, used_bytes: 512, free_bytes: 512, pct: 50 },
      network: { ens3: { rx_bytes_per_sec: 1024, tx_bytes_per_sec: 2048, rx_packets_per_sec: 1, tx_packets_per_sec: 2,
        rx_errors_delta: 0, tx_errors_delta: 0, rx_drops_delta: 0, tx_drops_delta: 0 } } });
    localServerWire(); document.querySelector('#server-monitor-refresh').click(); await flush();
    const card = document.querySelector('#st-server-monitor').textContent;
    expect(card).toMatch(/0,10.*0,20.*0,30/);
    expect(card).toContain('50,00 %');
    expect(card).toContain('ens3');
    expect(card).toContain('1,0 КиБ/с');
    expect(card).toMatch(/месячн/i);
    expect(card).toMatch(/не.*собира/i);
    localServerStop();
  });
});
