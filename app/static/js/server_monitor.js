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

function fmtDecimal(value) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return '—';
  return Number(value).toFixed(2).replace('.', ',');
}

function kpi(title, value, sub, cls) {
  return `<div class="glass kpi ${cls || ''}">
      <div class="kpi-label">${esc(title)}</div>
      <div class="kpi-value">${esc(value)}</div>
      <div class="kpi-sub">${esc(sub)}</div>
    </div>`;
}

function usageGauge(label, pct) {
  if (pct === null || pct === undefined || !Number.isFinite(Number(pct)) || Number(pct) < 0 || Number(pct) > 100) return '';
  const n = Number(pct);
  const level = n > 90 ? 'danger' : n >= 80 ? 'warning' : 'normal';
  const status = n > 90 ? 'высокая' : n >= 80 ? 'повышенная' : 'ниже порога';
  return `<p class="server-gauge-row">${esc(label)}: ${esc(fmtPct(n))} — ${status} нагрузка
    <progress class="server-gauge ${level}" max="100" value="${n}" aria-label="${esc(label)}"></progress></p>`;
}

let localGeneration = 0;

export function localServerCardHtml() {
  return `
      <div class="glass card" id="st-server-monitor" data-sec="integr" style="order:38">
        <div class="card-title">🖥️ Мониторинг сервера</div>
        <p class="form-hint" id="server-monitor-note">CPU и память читаются из Linux на сервере,
          где запущен Ямастер Чек, без подключения к облаку. Диск — файловая система
          каталога приложения; база и другие тома могут находиться отдельно. При контейнеризации
          или иной изоляции показания могут отличаться от всей ВМ. История производительности
          и настраиваемые пороги пока не собираются.</p>
        <button type="button" class="btn btn-sm" id="server-monitor-refresh">🔄 Показать / обновить показатели</button>
        <p class="form-hint" id="server-monitor-status" role="status">Нажмите кнопку для локального замера.</p>
        <div class="grid kpi-grid" id="server-monitor-kpi"></div>
        <div id="server-monitor-details"></div>
        <details style="margin-top:16px">
          <summary>Трафик интерфейса за месяц (оценка)</summary>
          <p class="form-hint">История доступна только при отдельно настроенном vnStat. Счётчики интерфейса могут отличаться от счёта провайдера.</p>
          <button type="button" class="btn btn-sm" id="server-traffic-refresh">Показать историю трафика</button>
          <div id="server-traffic" role="status"><p class="form-hint">Нажмите кнопку для чтения истории.</p></div>
        </details>
      </div>`;
}

// Десятичные ГБ применяются только к ориентиру тарифа 1 ТБ = 1000 ГБ.
function trafficGb(value) {
  return (value / 1e9).toFixed(2).replace('.', ',') + ' ГБ';
}

function trafficHtml(result) {
  const messages = {
    not_configured: 'Учёт не настроен: выберите интерфейс и установите vnStat по инструкции администратора.',
    invalid_interface: 'Некорректное имя интерфейса. Проверьте настройку администратора.',
    not_installed: 'vnStat не установлен или недоступен приложению.',
    unavailable: 'История vnStat недоступна: проверьте службу, интерфейс и права чтения.',
  };
  if (messages[result.status]) return `<p class="form-hint">${esc(messages[result.status])}</p>`;
  const data = result.data;
  if (!data || !Array.isArray(data.months) || !Array.isArray(data.days)) return '<p>История недоступна.</p>';
  const current = data.months.find(row => row.period === result.current_period);
  const amount = current ? current.rx_bytes + current.tx_bytes : null;
  const warning = amount === null ? '' : amount >= 950e9 ? 'Наблюдённый объём достиг 95 % от ориентира.'
    : amount >= 800e9 ? 'Наблюдённый объём достиг 80 % от ориентира.' : '';
  const stale = result.status === 'stale' ? '<p role="alert">Источник устарел; показатели могут быть неполными.</p>'
    : '<p role="alert">Свежесть источника не подтверждена: часовой пояс vnStat может отличаться.</p>';
  const months = data.months.map(row => `<li>${esc(row.period)}: вход ${esc(trafficGb(row.rx_bytes))}, выход ${esc(trafficGb(row.tx_bytes))}, сумма ${esc(trafficGb(row.rx_bytes + row.tx_bytes))}</li>`).join('');
  const maxDay = Math.max(1, ...data.days.map(row => row.rx_bytes + row.tx_bytes));
  const days = data.days.map(row => `<li>${esc(row.date)}: вход ${esc(trafficGb(row.rx_bytes))}, выход ${esc(trafficGb(row.tx_bytes))} <progress max="${maxDay}" value="${row.rx_bytes + row.tx_bytes}" aria-label="${esc('Суммарный трафик ' + row.date)}"></progress></li>`).join('');
  return `${stale}<p>Интерфейс: <strong>${esc(data.interface)}</strong>. Начало учёта: ${esc(data.created_date)}. Последнее обновление: ${esc(data.updated_at.replace('T', ' '))} (время vnStat, часовой пояс не подтверждён).</p>
    <p>${current ? `Календарный месяц приложения ${esc(current.period)}: <strong>${esc(trafficGb(amount))}</strong> из ориентира 1000 ГБ (вход + выход).` : 'За текущий месяц данных нет.'}</p>
    ${warning ? `<p role="alert">${esc(warning)}</p>` : ''}
    <p class="form-hint">Это оценка счётчиков интерфейса, не счёт провайдера. Возможны перерывы сбора, внутренний трафик и отличие расчётного периода или часового пояса vnStat. Полнота данных не подтверждена; остаток и прогноз не рассчитываются.</p>
    <p><strong>По месяцам</strong></p><ul>${months || '<li>Данных нет.</li>'}</ul>
    <details><summary>По дням (до 31 дня)</summary><ul>${days || '<li>Данных нет.</li>'}</ul></details>`;
}

export function localServerWire() {
  const button = document.getElementById('server-monitor-refresh');
  const trafficButton = document.getElementById('server-traffic-refresh');
  if (!button || !trafficButton) return;
  trafficButton.onclick = async () => {
    const generation = localGeneration;
    const panel = document.getElementById('server-traffic');
    trafficButton.disabled = true;
    panel.textContent = 'Чтение истории трафика…';
    try {
      const result = await api.get('/api/v1/admin/server-monitor/traffic');
      if (generation !== localGeneration || !trafficButton.isConnected) return;
      panel.innerHTML = trafficHtml(result);
    } catch (e) {
      if (generation !== localGeneration || !trafficButton.isConnected) return;
      panel.textContent = 'Не удалось прочитать историю: ' + (e.message || e);
    } finally {
      if (generation === localGeneration && trafficButton.isConnected) trafficButton.disabled = false;
    }
  };
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
      const inodes = disk.inodes;
      const io = data.disk_io;
      const taskInfo = data.tasks ? `${fmtInt(data.tasks.runnable)} выполняются или готовы из ${fmtInt(data.tasks.total)}` : 'недоступно';
      detailBox.innerHTML = `${usageGauge('Использование памяти', memory.pct)}${usageGauge('Занятость файловой системы приложения', disk.pct)}
        <p class="form-hint">Ориентиры памяти и диска: 80–90 % — повышенная нагрузка, выше 90 % — высокая. Показатели диска относятся только к каталогу приложения.</p>
        <p class="form-hint">CPU: пользователь ${esc(fmtPct(detail?.user_pct))}, система ${esc(fmtPct(detail?.system_pct))}, ожидание I/O ${esc(fmtPct(detail?.iowait_pct))}, steal ${esc(fmtPct(detail?.steal_pct))}.</p>
        <p class="form-hint">inode файловой системы приложения: ${inodes ? `занято ${esc(fmtInt(inodes.used))} из ${esc(fmtInt(inodes.total))} (${esc(fmtPct(inodes.pct))}), доступно ${esc(fmtInt(inodes.available))}` : 'недоступно'}.</p>
        <p class="form-hint">I/O устройства файловой системы приложения: ${io ? `${esc(io.device)}, чтение ${esc(fmtDecimal(io.read_iops))} и запись ${esc(fmtDecimal(io.write_iops))} операций/с; скорость ${esc(fmtBytes(io.read_bytes_per_sec))}/с и ${esc(fmtBytes(io.write_bytes_per_sec))}/с; ожидание ${esc(fmtDecimal(io.read_await_ms))} и ${esc(fmtDecimal(io.write_await_ms))} мс; занято ${esc(fmtPct(io.busy_pct))}` : 'недоступно'}. Короткий замер; виртуальное устройство может не совпадать с физическим диском.</p>
        <p class="form-hint">Задачи планировщика (включая потоки): ${esc(taskInfo)}. TCP-сокеты (включая слушающие): ${data.tcp_sockets == null ? 'недоступно' : esc(fmtInt(data.tcp_sockets))}.</p>
        <p class="form-hint">Переключения контекста: ${esc(fmtDecimal(data.context_switches_per_sec))}/с. Swap I/O (страниц/с): чтение ${esc(fmtDecimal(data.swap_io?.in_pages_per_sec))}, запись ${esc(fmtDecimal(data.swap_io?.out_pages_per_sec))}; короткий замер.</p>
        <p class="form-hint">Сеть: скорость и изменение ошибок за короткий замер, отдельно по интерфейсам. Виртуальные интерфейсы могут учитывать один и тот же трафик повторно.</p>
        <ul>${rows || '<li>Данные сетевых интерфейсов недоступны.</li>'}</ul>`;
      const note = document.getElementById('server-monitor-note');
      note.textContent = (data.container_hint ? 'Обнаружен контейнер: показатели могут не соответствовать всей ВМ. '
        : 'CPU и память читаются из Linux, где запущен Ямастер Чек, без подключения к облаку. '
          + 'При контейнеризации или иной изоляции показания могут отличаться от всей ВМ. ')
        + 'Диск — файловая система приложения; база и другие тома могут находиться отдельно. '
        + 'История производительности и настраиваемые пороги пока не собираются.';
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
