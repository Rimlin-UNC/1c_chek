// Локальный мониторинг среды, где запущено приложение.
import { api } from './api.js';
import { esc, fmtDate, fmtInt } from './ui.js';

function fmtBytes(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' ГБ';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' МБ';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' КБ';
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
          или иной изоляции показания могут отличаться от всей ВМ. История, пороги и трафик ВМ
          пока не собираются.</p>
        <button type="button" class="btn btn-sm" id="server-monitor-refresh">🔄 Показать / обновить показатели</button>
        <p class="form-hint" id="server-monitor-status" role="status">Нажмите кнопку для локального замера.</p>
        <div class="grid kpi-grid" id="server-monitor-kpi"></div>
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
      ].join('');
      const note = document.getElementById('server-monitor-note');
      note.textContent = (data.container_hint ? 'Обнаружен контейнер: показатели могут не соответствовать всей ВМ. '
        : 'CPU и память читаются из Linux, где запущен Ямастер Чек, без подключения к облаку. '
          + 'При контейнеризации или иной изоляции показания могут отличаться от всей ВМ. ')
        + 'Диск — файловая система приложения; база и другие тома могут находиться отдельно. '
        + 'История, пороги и трафик ВМ пока не собираются.';
      status.textContent = data.collected_at ? `Локальный замер: ${fmtDate(data.collected_at)}.` : 'Локальный замер получен.';
    } catch (e) {
      if (generation !== localGeneration || !button.isConnected) return;
      document.getElementById('server-monitor-kpi').innerHTML = '';
      status.textContent = 'Не удалось получить локальные показатели: ' + (e.message || e);
    } finally {
      if (generation === localGeneration && button.isConnected) button.disabled = false;
    }
  };
}

export function localServerStop() {
  localGeneration += 1;
}
