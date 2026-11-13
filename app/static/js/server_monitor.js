// Локальный мониторинг среды, где запущено приложение.
import { api } from './api.js';
import { esc, fmtDate, fmtInt } from './ui.js';

function fmtBytes(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2).replace('.', ',') + ' ГиБ';
  if (n >= 1048576) return (n / 1048576).toFixed(1).replace('.', ',') + ' МиБ';
  if (n >= 1024) return (n / 1024).toFixed(1).replace('.', ',') + ' КиБ';
  return fmtInt(n) + ' Б';
}

function fmtPct(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value) || 0;
  return n.toFixed(2).replace('.', ',') + ' %';
}

function kpi(title, value, sub, cls) {
  return `<div class="glass kpi ${cls || ''}">
      <div class="kpi-label">${esc(title)}</div>
      <div class="kpi-value">${esc(value)}</div>
      <div class="kpi-sub">${esc(sub)}</div>
    </div>`;
}

let localGeneration = 0;

export function localServerCardHtml() {
  return `
      <div class="glass card" id="st-server-monitor" data-sec="integr" style="order:38">
        <div class="card-title">🖥️ Мониторинг сервера</div>
        <p class="form-hint" id="server-monitor-note">CPU и память читаются из Linux на сервере,
          где запущен Ямастер Чек, без подключения к облаку. Диск — файловая система
          каталога приложения; база и другие тома могут находиться отдельно. При контейнеризации
          или иной изоляции показания могут отличаться от всей ВМ. История, пороги и месячный трафик
          пока не собираются.</p>
        <button type="button" class="btn btn-sm" id="server-monitor-refresh">🔄 Показать / обновить показатели</button>
        <p class="form-hint" id="server-monitor-status" role="status">Нажмите кнопку для локального замера.</p>
        <div class="grid kpi-grid" id="server-monitor-kpi"></div>
        <div id="server-monitor-details"></div>
      </div>`;
}

export function localServerWire() {
  const button = document.getElementById('server-monitor-refresh');
  if (!button) return;
  button.onclick = async () => {
    const generation = localGeneration;
    const status = document.getElementById('server-monitor-status');
    button.disabled = true;
    status.textContent = 'Получение локальных показателей…';
    try {
      const data = await api.get('/api/v1/admin/server-monitor');
      if (generation !== localGeneration || !button.isConnected) return;
      const memory = data.memory || {}, disk = data.disk || {};
      const cpus = data.logical_cpus === null || data.logical_cpus === undefined ? '—' : fmtInt(data.logical_cpus);
      const box = document.getElementById('server-monitor-kpi');
      box.innerHTML = [
        kpi('CPU сервера', fmtPct(data.cpu_pct), `${cpus} логических CPU видны системе; короткий замер, включая steal`),
        kpi('Память сервера', fmtPct(memory.pct),
          `занято ${fmtBytes(memory.used_bytes)} из ${fmtBytes(memory.total_bytes)}; доступно ${fmtBytes(memory.available_bytes)}`),
        kpi('Файловая система приложения', fmtPct(disk.pct),
          `занято ${fmtBytes(disk.used_bytes)} из ${fmtBytes(disk.total_bytes)}; доступно ${fmtBytes(disk.available_bytes)}`),
        kpi('Swap', data.swap?.total_bytes === 0 ? 'Не настроен' : fmtPct(data.swap?.pct),
          `занято ${fmtBytes(data.swap?.used_bytes)} из ${fmtBytes(data.swap?.total_bytes)}`),
        kpi('Load average', Array.isArray(data.load_average) && data.load_average.length === 3
          ? data.load_average.map(v => Number(v).toFixed(2).replace('.', ',')).join(' / ') : '—',
        'Средняя нагрузка за 1 / 5 / 15 минут; не процент CPU'),
        kpi('Время работы', data.uptime_sec == null ? '—' : `${Math.floor(data.uptime_sec / 3600)} ч`,
          'Время работы системы с момента запуска'),
      ].join('');
      const detail = data.cpu_detail;
      const detailBox = document.getElementById('server-monitor-details');
      const network = data.network;
      const rows = network ? Object.entries(network).map(([name, stats]) => `<li><strong>${esc(name)}</strong>: ${stats
        ? `вход ${esc(fmtBytes(stats.rx_bytes_per_sec))}/с, выход ${esc(fmtBytes(stats.tx_bytes_per_sec))}/с; ошибки вход/выход ${esc(stats.rx_errors_delta)} / ${esc(stats.tx_errors_delta)}, сбросы ${esc(stats.rx_drops_delta)} / ${esc(stats.tx_drops_delta)}`
        : 'первый замер или счётчики сброшены — скорость недоступна'}</li>`).join('') : '';
      detailBox.innerHTML = `<p class="form-hint">CPU: пользователь ${esc(fmtPct(detail?.user_pct))}, система ${esc(fmtPct(detail?.system_pct))}, ожидание I/O ${esc(fmtPct(detail?.iowait_pct))}, steal ${esc(fmtPct(detail?.steal_pct))}.</p>
        <p class="form-hint">Сеть: скорость и изменение ошибок за короткий замер, отдельно по интерфейсам. Виртуальные интерфейсы могут учитывать один и тот же трафик повторно.</p>
        <ul>${rows || '<li>Данные сетевых интерфейсов недоступны.</li>'}</ul>`;
      const note = document.getElementById('server-monitor-note');
      note.textContent = (data.container_hint ? 'Обнаружен контейнер: показатели могут не соответствовать всей ВМ. '
        : 'CPU и память читаются из Linux, где запущен Ямастер Чек, без подключения к облаку. '
          + 'При контейнеризации или иной изоляции показания могут отличаться от всей ВМ. ')
        + 'Диск — файловая система приложения; база и другие тома могут находиться отдельно. '
        + 'История, пороги и месячный трафик пока не собираются.';
      status.textContent = data.collected_at ? `Локальный замер: ${fmtDate(data.collected_at)}.` : 'Локальный замер получен.';
    } catch (e) {
      if (generation !== localGeneration || !button.isConnected) return;
      document.getElementById('server-monitor-kpi').innerHTML = '';
      document.getElementById('server-monitor-details').innerHTML = '';
      status.textContent = 'Не удалось получить локальные показатели: ' + (e.message || e);
    } finally {
      if (generation === localGeneration && button.isConnected) button.disabled = false;
    }
  };
}

export function localServerStop() {
  localGeneration += 1;
}
