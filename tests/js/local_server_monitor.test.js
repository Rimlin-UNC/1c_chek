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
    expect(card).toMatch(/трафик интерфейса за месяц/i);
    expect(card).toMatch(/не.*собира/i);
    localServerStop();
  });
});

describe('история трафика — оценка по одному интерфейсу', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  it('не запрашивает историю без нажатия и не объявляет остаток провайдера', async () => {
    api.get.mockResolvedValue({ status: 'freshness_unknown', current_period: '2026-11', data: {
      interface: 'ens3', created_date: '2026-10-17', updated_at: '2026-11-08T12:00:00',
      months: [{ period: '2026-11', rx_bytes: 400e9, tx_bytes: 450e9 }],
      days: [{ date: '2026-11-08', rx_bytes: 1e9, tx_bytes: 2e9 }], billing_verified: false } });
    localServerWire(); expect(api.get).not.toHaveBeenCalled();
    document.querySelector('#server-traffic-refresh').click(); await flush();
    const text = document.querySelector('#server-traffic').textContent;
    expect(text).toContain('ens3'); expect(text).toContain('850,00 ГБ');
    expect(text).toMatch(/оценк/i); expect(text).toMatch(/не.*счёт/i);
    expect(text).toContain('80 %');
    expect(text).toMatch(/свежесть источника не подтверждена/i);
    expect(text).toContain('2026-11-08');
    expect(text).not.toMatch(/остаток.*150/i);
    expect(api.get).toHaveBeenCalledWith('/api/v1/admin/server-monitor/traffic');
    localServerStop();
  });
  it('не выдаёт ноль, если сборщик отключён, не установлен или устарел', async () => {
    api.get.mockResolvedValueOnce({status: 'not_configured'}).mockResolvedValueOnce({status: 'not_installed'})
      .mockResolvedValueOnce({status: 'stale', current_period: '2026-11', data: {
        interface: 'ens3', created_date: '2026-11-01', updated_at: '2026-11-03T12:00:00', months: [], days: []}});
    localServerWire(); const button = document.querySelector('#server-traffic-refresh');
    button.click(); await flush(); expect(document.querySelector('#server-traffic').textContent).toMatch(/не настроен/i);
    button.click(); await flush(); expect(document.querySelector('#server-traffic').textContent).toMatch(/не установлен/i);
    button.click(); await flush(); expect(document.querySelector('#server-traffic').textContent).toMatch(/устарел/i);
    expect(document.querySelector('#server-traffic').textContent).not.toMatch(/остаток.*0/i);
    localServerStop();
  });
});
