import { beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('../../app/static/js/api.js', () => ({ api: { get: vi.fn(), post: vi.fn() } }));
vi.mock('../../app/static/js/ui.js', async (importOriginal) => ({
  esc: (await importOriginal()).esc, fmtDate: value => String(value || ''),
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

describe('дополнительные показатели файловой системы и ядра', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  it('показывает inode и I/O лишь для устройства файловой системы приложения', async () => {
    api.get.mockResolvedValue({ disk: { pct: 60, used_bytes: 6e9, total_bytes: 10e9, available_bytes: 4e9,
      inodes: { total: 1000, used: 850, available: 100, pct: 85 } },
    disk_io: { device: 'sda1', read_iops: 5, write_iops: 10, read_bytes_per_sec: 10240,
      write_bytes_per_sec: 12800, read_await_ms: 2, write_await_ms: null, busy_pct: 5 },
    tasks: { runnable: 2, total: 254 }, tcp_sockets: 8, context_switches_per_sec: 123.5,
    swap_io: { in_pages_per_sec: 2.5, out_pages_per_sec: 3 } });
    localServerWire(); document.querySelector('#server-monitor-refresh').click(); await flush();
    const text = document.querySelector('#st-server-monitor').textContent;
    expect(text).toContain('85,00 %'); expect(text).toContain('850');
    expect(text).toContain('sda1'); expect(text).toMatch(/5,00.*10,00/);
    expect(text).toMatch(/2.*254/); expect(text).toMatch(/TCP.*8/);
    expect(text).toMatch(/включая потоки/i); expect(text).toMatch(/слуша/i);
    expect(text).toContain('123,50'); expect(text).toContain('2,50'); expect(text).toMatch(/страниц.*с/i);
    localServerStop();
  });
  it('сообщает об отсутствии inode и I/O вместо выдуманных нулей', async () => {
    api.get.mockResolvedValue({ disk: { pct: 50, inodes: null }, disk_io: null, tasks: null, tcp_sockets: null });
    localServerWire(); document.querySelector('#server-monitor-refresh').click(); await flush();
    const text = document.querySelector('#server-monitor-details').textContent;
    expect(text).toMatch(/inode.*недоступ/i); expect(text).toMatch(/I\/O.*недоступ/i);
    expect(text).not.toMatch(/inode.*0,00 %/);
    localServerStop();
  });
});

describe('пороговые индикаторы снимка', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  it('отдельно обозначает пороги памяти и диска и не рисует фиктивный ноль', async () => {
    api.get.mockResolvedValue({ memory: { pct: 85 }, disk: { pct: 95 } });
    localServerWire(); document.querySelector('#server-monitor-refresh').click(); await flush();
    expect(document.querySelector('[aria-label="Использование памяти"]').className).toMatch(/warning/);
    expect(document.querySelector('[aria-label="Занятость файловой системы приложения"]').className).toMatch(/danger/);
    expect(document.querySelector('#server-monitor-details').textContent).toMatch(/80.*90/);
    api.get.mockResolvedValue({ memory: null, disk: null });
    document.querySelector('#server-monitor-refresh').click(); await flush();
    expect(document.querySelectorAll('#server-monitor-details progress')).toHaveLength(0);
    localServerStop();
  });
});

describe('список процессов только по требованию', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  it('показывает отдельно CPU и память с оговоркой неполноты без опасных имён', async () => {
    api.get.mockResolvedValue({ status: 'available', visible: 2, zombies: 1, partial: true,
      top_cpu: [{ pid: 12, name: '<img src=x onerror=alert(1)>', state: 'R', cpu_pct: 120, rss_bytes: 1048576, memory_pct: 1 }],
      top_memory: [{ pid: 22, name: 'worker', state: 'Z', rss_bytes: 2097152, memory_pct: 2 }] });
    localServerWire(); expect(api.get).not.toHaveBeenCalled();
    document.querySelector('#server-processes-refresh').click(); await flush();
    const panel = document.querySelector('#server-processes');
    expect(api.get).toHaveBeenCalledWith('/api/v1/admin/server-monitor/processes');
    expect(panel.textContent).toMatch(/неполный/i); expect(panel.textContent).toMatch(/зомби.*1/i);
    expect(panel.textContent).toContain('120,00 %'); expect(panel.textContent).toContain('worker');
    expect(panel.querySelector('img')).toBeNull();
    localServerStop();
  });
  it('объясняет недоступность вместо пустого списка процессов', async () => {
    api.get.mockResolvedValue({ status: 'unavailable' });
    localServerWire(); document.querySelector('#server-processes-refresh').click(); await flush();
    expect(document.querySelector('#server-processes').textContent).toMatch(/недоступ/i);
    localServerStop();
  });
});

describe('необязательная история производительности', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  it('по кнопке рисует CPU и RAM и не соединяет точки через пропуски', async () => {
    const base = { expected: 1, cpu_samples: 1, memory_samples: 1, swap_samples: 0, rx_samples: 0, tx_samples: 0 };
    api.get.mockResolvedValue({ status: 'available', period: '1h', current_timestamp: 120, last_sample_at: 120,
      retention_paused: false, buckets: [
        { ...base, timestamp: 0, samples: 1, cpu_pct: 30, memory_pct: 50 },
        { ...base, timestamp: 60, samples: 0, cpu_pct: null, memory_pct: null, cpu_samples: 0, memory_samples: 0 },
        { ...base, timestamp: 120, samples: 1, cpu_pct: 60, memory_pct: 70 },
      ] });
    localServerWire(); expect(api.get).not.toHaveBeenCalled();
    document.querySelector('#server-history-refresh').click(); await flush();
    const panel = document.querySelector('#server-history');
    expect(api.get).toHaveBeenCalledWith('/api/v1/admin/server-monitor/history?period=1h');
    expect(panel.querySelector('svg[aria-label*="CPU"]')).not.toBeNull();
    expect(panel.querySelectorAll('svg line')).toHaveLength(0);
    expect(panel.textContent).toMatch(/пропуск/i);
    expect(panel.textContent).toContain('30,00 %');
    document.querySelector('#server-history-period').value = '24h';
    document.querySelector('#server-history-refresh').click(); await flush();
    expect(api.get).toHaveBeenCalledWith('/api/v1/admin/server-monitor/history?period=24h');
    localServerStop();
  });
  it('отсутствующий сборщик не превращает отсутствие истории в нулевую нагрузку', async () => {
    api.get.mockResolvedValue({ status: 'not_configured' });
    localServerWire(); document.querySelector('#server-history-refresh').click(); await flush();
    const panel = document.querySelector('#server-history');
    expect(panel.textContent).toMatch(/не настроен/i);
    expect(panel.querySelector('svg')).toBeNull();
    localServerStop();
  });
});

describe('монтирования сервера по запросу', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  it('не запрашивает автоматически, экранирует имена и показывает неопрошенные тома без нулей', async () => {
    api.get.mockResolvedValue({ status: 'available', partial: true, mounts: [
      { path: '/<img src=x onerror=alert(1)>', type: 'ext4', device: '8:1', status: 'available',
        disk: { pct: 91, available_bytes: 4096, total_bytes: 8192 }, inodes: { pct: 80 } },
      { path: '/net', type: 'nfs4', device: '0:42', status: 'not_probed', disk: null, inodes: null },
      { path: '/busy', type: 'ext4', device: '8:2', status: 'timeout', disk: null, inodes: null },
    ] });
    localServerWire(); expect(api.get).not.toHaveBeenCalled();
    document.querySelector('#server-mounts-refresh').click(); await flush();
    expect(api.get).toHaveBeenCalledWith('/api/v1/admin/server-monitor/mounts');
    const panel = document.querySelector('#server-mounts');
    expect(panel.querySelector('img')).toBeNull();
    expect(panel.textContent).toContain('/<img src=x onerror=alert(1)>');
    expect(panel.textContent).toContain('91,00 %');
    expect(panel.textContent).toMatch(/высокая занятость/i);
    expect(panel.textContent).toMatch(/не опраш/i);
    expect(panel.textContent).toMatch(/превышен.*срок/i);
    expect(panel.textContent).toMatch(/неполны/i);
    expect(panel.querySelectorAll('tr')).toHaveLength(4);
    localServerStop();
  });
  it('не обновляет другой раздел поздним ответом', async () => {
    let resolve;
    api.get.mockReturnValue(new Promise(r => { resolve = r; }));
    localServerWire(); document.querySelector('#server-mounts-refresh').click();
    localServerStop(); document.body.innerHTML = '<div id="server-mounts">другой раздел</div>';
    resolve({ status: 'available', mounts: [] }); await flush();
    expect(document.querySelector('#server-mounts').textContent).toBe('другой раздел');
  });
  it('корректно описывает необязательную историю производительности', () => {
    expect(document.querySelector('#server-monitor-note').textContent).toMatch(/история.*отдельно/i);
    expect(document.querySelector('#server-monitor-note').textContent).not.toMatch(/история производительности.*пока не собира/i);
  });
});

describe('условный сценарий месячного трафика', () => {
  beforeEach(() => { localServerStop(); vi.clearAllMocks(); document.body.innerHTML = localServerCardHtml(); });
  const data = { interface: 'ens3', created_date: '2026-10-01', updated_at: '2026-11-08T12:00:00',
    months: [{ period: '2026-11', rx_bytes: 400e9, tx_bytes: 450e9 }], days: [] };
  it('помечает расчёт предположением и не выдаёт его за подтверждённый счёт или остаток', async () => {
    api.get.mockResolvedValue({ status: 'freshness_unknown', current_period: '2026-11', data,
      scenario: { projected_bytes: 1200e9, sample_days: 7, as_of: '2026-11-08T12:00:00',
        remaining_minutes: 32400, billing_verified: false, completeness_verified: false } });
    localServerWire(); document.querySelector('#server-traffic-refresh').click(); await flush();
    const text = document.querySelector('#server-traffic').textContent;
    expect(text).toMatch(/условный сценарий/i); expect(text).toContain('1200,00 ГБ');
    expect(text).toMatch(/7.*дн/i); expect(text).toMatch(/не.*биллинг/i);
    expect(text).toMatch(/не подтвержден/i); expect(text).not.toMatch(/остаток.*150/i);
    localServerStop();
  });
  it('не показывает число прогноза без достаточного ряда', async () => {
    api.get.mockResolvedValue({ status: 'freshness_unknown', current_period: '2026-11', data, scenario: null });
    localServerWire(); document.querySelector('#server-traffic-refresh').click(); await flush();
    const text = document.querySelector('#server-traffic').textContent;
    expect(text).toMatch(/сценарий недоступен/i);
    expect(text).not.toMatch(/прогноз.*0,00/i);
    localServerStop();
  });
});
