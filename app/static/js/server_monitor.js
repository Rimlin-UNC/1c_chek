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
          или иной изоляции показания могут отличаться от всей ВМ. История производительности включается отдельно и автоматически не собирается;
          пороги не настраиваются.</p>
        <button type="button" class="btn btn-sm" id="server-monitor-refresh">🔄 Показать / обновить показатели</button>
        <p class="form-hint" id="server-monitor-status" role="status">Нажмите кнопку для локального замера.</p>
        <div class="grid kpi-grid" id="server-monitor-kpi"></div>
        <div id="server-monitor-details"></div>
        <details style="margin-top:16px">
          <summary>Монтирования (по запросу)</summary>
          <p class="form-hint">Ограниченный список из mountinfo. Сетевые, автоматические и прочие неподдерживаемые типы не опрашиваются.</p>
          <button type="button" class="btn btn-sm" id="server-mounts-refresh">Показать монтирования</button>
          <div id="server-mounts" role="status"><p class="form-hint">Нажмите кнопку для просмотра.</p></div>
        </details>
        <details style="margin-top:16px">
          <summary>История производительности (необязательно)</summary>
          <p class="form-hint">История доступна только при отдельно настроенном сборе; сама программа его не запускает.</p>
          <label class="field"><span>Интервал</span><select id="server-history-period">
            <option value="1h">1 час</option><option value="24h">24 часа</option>
            <option value="7d">7 дней</option><option value="30d">30 дней</option>
          </select></label>
          <button type="button" class="btn btn-sm" id="server-history-refresh">Показать историю</button>
          <div id="server-history" role="status"><p class="form-hint">Нажмите кнопку для чтения истории.</p></div>
        </details>
        <details style="margin-top:16px">
          <summary>Процессы (по запросу)</summary>
          <p class="form-hint">Видимость зависит от procfs и изоляции; командные строки не читаются.</p>
          <button type="button" class="btn btn-sm" id="server-processes-refresh">Показать процессы</button>
          <div id="server-processes" role="status"><p class="form-hint">Нажмите кнопку для короткого замера.</p></div>
        </details>
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
  const scenario = result.scenario;
  const projection = scenario && Number.isSafeInteger(scenario.projected_bytes) && scenario.projected_bytes >= 0 && scenario.sample_days === 7
    ? `<p><strong>Условный сценарий на конец календарного месяца: ${esc(trafficGb(scenario.projected_bytes))}</strong>${scenario.projected_bytes >= 1e12 ? ' — при этих предположениях превышает ориентир 1000 ГБ.' : '.'}</p>
      <p class="form-hint">Наблюдённый месяц плюс средний трафик за 7 предыдущих дней на оставшееся время от ${esc(String(scenario.as_of).replace('T', ' '))}. Предполагается постоянный темп, полный учёт, календарный месяц с первого числа и совпадение часов. Свежесть, полнота и тариф не подтверждены: это не биллинг и не надёжный прогноз; скачки и простои его изменят. Сверяйте с кабинетом провайдера.</p>`
    : '<p>Условный сценарий недоступен: недостаточно согласованных наблюдений или не совпадают даты. Отсутствие расчёта не означает нулевой трафик.</p>';
  const stale = result.status === 'stale' ? '<p role="alert">Источник устарел; показатели могут быть неполными.</p>'
    : '<p role="alert">Свежесть источника не подтверждена: часовой пояс vnStat может отличаться.</p>';
  const months = data.months.map(row => `<li>${esc(row.period)}: вход ${esc(trafficGb(row.rx_bytes))}, выход ${esc(trafficGb(row.tx_bytes))}, сумма ${esc(trafficGb(row.rx_bytes + row.tx_bytes))}</li>`).join('');
  const maxDay = Math.max(1, ...data.days.map(row => row.rx_bytes + row.tx_bytes));
  const days = data.days.map(row => `<li>${esc(row.date)}: вход ${esc(trafficGb(row.rx_bytes))}, выход ${esc(trafficGb(row.tx_bytes))} <progress max="${maxDay}" value="${row.rx_bytes + row.tx_bytes}" aria-label="${esc('Суммарный трафик ' + row.date)}"></progress></li>`).join('');
  return `${stale}<p>Интерфейс: <strong>${esc(data.interface)}</strong>. Начало учёта: ${esc(data.created_date)}. Последнее обновление: ${esc(data.updated_at.replace('T', ' '))} (время vnStat, часовой пояс не подтверждён).</p>
    <p>${current ? `Календарный месяц приложения ${esc(current.period)}: <strong>${esc(trafficGb(amount))}</strong> из ориентира 1000 ГБ (вход + выход).` : 'За текущий месяц данных нет.'}</p>
    ${warning ? `<p role="alert">${esc(warning)}</p>` : ''}
    ${projection}
    <p class="form-hint">Это оценка счётчиков интерфейса, не счёт провайдера. Возможны перерывы сбора, внутренний трафик и отличие расчётного периода или часового пояса vnStat. Полнота данных не подтверждена; подтверждённый остаток не рассчитывается.</p>
    <p><strong>По месяцам</strong></p><ul>${months || '<li>Данных нет.</li>'}</ul>
    <details><summary>По дням (до 31 дня)</summary><ul>${days || '<li>Данных нет.</li>'}</ul></details>`;
}

function mountsHtml(result) {
  if (result.status !== 'available' || !Array.isArray(result.mounts)) return '<p>Данные монтирований недоступны.</p>';
  const statuses = { not_probed: 'не опрашивается', timeout: 'превышен срок опроса', busy: 'лимит опросов', unavailable: 'недоступно' };
  const rows = result.mounts.map(item => {
    const disk = item.disk;
    const status = statuses[item.status] || (item.status === 'available' ? 'получено' : 'недоступно');
    const pct = disk?.pct;
    const warning = pct != null && Number.isFinite(Number(pct)) && Number(pct) <= 100
      ? Number(pct) > 90 ? ' — высокая занятость' : Number(pct) >= 80 ? ' — повышенная занятость' : '' : '';
    return `<tr><td>${esc(item.path)}</td><td>${esc(item.type)}</td><td>${esc(item.device)}</td>
      <td>${disk ? esc(fmtPct(disk.pct)) : '—'}</td><td>${disk ? esc(fmtBytes(disk.available_bytes)) : '—'}</td>
      <td>${esc(fmtPct(item.inodes?.pct))}</td><td>${esc(status + warning)}</td></tr>`;
  }).join('');
  return `${result.partial ? '<p role="alert">Данные неполны: список ограничен или содержит ошибки.</p>' : ''}
    <p>Снимок: ${result.collected_at ? esc(fmtDate(result.collected_at)) : 'время недоступно'}.</p>
    <p class="form-hint">Пространство имён приложения, не обязательно вся ВМ. Не более 64 записей; до 16 локальных томов за запрос. Сетевые, FUSE, автоматические, неизвестные и скрытые монтирования не опрашиваются. Зависший опрос ограничен и не считается нулём. Один и тот же том может встречаться несколько раз.</p>
    <div class="table-wrap"><table class="data"><thead><tr><th>Путь</th><th>Тип</th><th>Устройство</th><th>Занято</th><th>Доступно</th><th>Занято inode</th><th>Статус</th></tr></thead><tbody>${rows || '<tr><td colspan="7">Монтирования не видны.</td></tr>'}</tbody></table></div>`;
}

function historyTime(value) {
  const date = new Date(Number(value) * 1000);
  return Number.isFinite(date.getTime()) ? date.toLocaleString('ru-RU') : '—';
}

function historyGraph(rows, series, maximum, label) {
  const width = 480, height = 110;
  const x = i => rows.length < 2 ? 0 : i * width / (rows.length - 1);
  let elements = '';
  for (const item of series) {
    let previous = null;
    rows.forEach((row, i) => {
      const value = row[item.field];
      const n = Number(value);
      if (value == null || !Number.isFinite(n) || n < 0 || n > maximum) { previous = null; return; }
      const point = { x: x(i), y: height - n * height / maximum, key: item.deviceBound ? row.io_device : null,
        full: row.expected > 0 && row.samples >= row.expected && row[item.count] >= row.expected };
      if (previous && previous.full && point.full && (!item.deviceBound || (point.key && point.key === previous.key))) elements += `<line class="${item.className}" x1="${previous.x.toFixed(2)}" y1="${previous.y.toFixed(2)}" x2="${point.x.toFixed(2)}" y2="${point.y.toFixed(2)}"></line>`;
      elements += `<circle class="${item.className}${point.full ? '' : ' partial'}" cx="${point.x.toFixed(2)}" cy="${point.y.toFixed(2)}" r="2"><title>${esc(item.label)}: ${esc(item.format(n))}; ${esc(historyTime(row.timestamp))}; наблюдений ${esc(row[item.count])} из ${esc(row.expected)}</title></circle>`;
      previous = point;
    });
  }
  return `<svg class="server-history-chart" viewBox="-3 -3 486 116" role="img" aria-label="${esc(label)}">${elements}</svg>`;
}

function extraHistoryHtml(result, rows) {
  if (result.extra_status === 'not_enabled') return '<p>Дополнительная история нагрузки и дискового I/O не включена в этой базе; см. инструкцию администратора.</p>';
  if (result.extra_status !== 'available') return '<p>Дополнительная история нагрузки и дискового I/O недоступна.</p>';
  const maxFor = fields => Math.max(1, ...rows.flatMap(row => fields.map(field => row[field]))
    .filter(v => v != null && Number.isFinite(Number(v)) && Number(v) >= 0).map(Number));
  const decimal = n => fmtDecimal(n);
  const load = historyGraph(rows, [{ field: 'load_1', count: 'load_samples', className: 'cpu', label: 'Load за минуту', format: decimal }], maxFor(['load_1']), 'Load average за минуту');
  const iops = historyGraph(rows, [
    { field: 'read_iops', count: 'read_iops_samples', className: 'cpu', label: 'Чтение', format: n => decimal(n) + ' операций/с', deviceBound: true },
    { field: 'write_iops', count: 'write_iops_samples', className: 'memory', label: 'Запись', format: n => decimal(n) + ' операций/с', deviceBound: true },
  ], maxFor(['read_iops', 'write_iops']), 'IOPS выбранного устройства');
  const awaitGraph = historyGraph(rows, [
    { field: 'read_await_ms', count: 'read_await_samples', className: 'cpu', label: 'Чтение', format: n => decimal(n) + ' мс', deviceBound: true },
    { field: 'write_await_ms', count: 'write_await_samples', className: 'memory', label: 'Запись', format: n => decimal(n) + ' мс', deviceBound: true },
  ], maxFor(['read_await_ms', 'write_await_ms']), 'Ожидание await устройства');
  const table = rows.map(row => `<tr><td>${esc(historyTime(row.timestamp))}</td>
    <td>${esc(decimal(row.load_1))} (${esc(row.load_samples)}/${esc(row.expected)})</td>
    <td>${row.io_mixed ? 'смена устройств' : esc(row.io_device || '—')}</td>
    <td>${esc(decimal(row.read_iops))} (${esc(row.read_iops_samples)}/${esc(row.expected)}) / ${esc(decimal(row.write_iops))} (${esc(row.write_iops_samples)}/${esc(row.expected)})</td>
    <td>${esc(decimal(row.read_await_ms))} (${esc(row.read_await_samples)}/${esc(row.expected)}) / ${esc(decimal(row.write_await_ms))} (${esc(row.write_await_samples)}/${esc(row.expected)})</td>
    <td>${esc(fmtPct(row.io_busy_pct))} (${esc(row.io_busy_samples)}/${esc(row.expected)})</td></tr>`).join('');
  return `<details><summary>История нагрузки и дискового I/O</summary>
    ${result.extra_full ? '<p role="alert">Достигнут предел дополнительной истории; новые значения могут не записываться. Проверьте сбор и инструкцию.</p>' : ''}
    <p class="form-hint">Load — среднее за минуту, не процент CPU. Дисковый I/O доступен только при точном сопоставлении устройства файловой системы приложения; имя устройства не гарантирует неизменности физического диска. Смена устройств внутри интервала скрывает I/O; между устройствами график не соединяется. Значения — средние коротких замеров, не непрерывное измерение; await усредняется по замерам, а не по количеству операций. В таблице показано число значений из ожидаемого. При отсутствии операций await недоступен.</p>
    <p>Load average, шкала до ${esc(decimal(maxFor(['load_1'])))}</p>${load}
    <p>IOPS: чтение (синий), запись (зелёный), шкала до ${esc(decimal(maxFor(['read_iops', 'write_iops'])))} операций/с</p>${iops}
    <p>Ожидание await: чтение (синий), запись (зелёный), шкала до ${esc(decimal(maxFor(['read_await_ms', 'write_await_ms'])))} мс</p>${awaitGraph}
    <div class="table-wrap"><table class="data"><thead><tr><th>Время</th><th>Load 1 мин</th><th>Устройство</th><th>IOPS чт./зап.</th><th>Await чт./зап., мс</th><th>Занятость I/O</th></tr></thead><tbody>${table}</tbody></table></div></details>`;
}

function historyHtml(result) {
  if (result.status === 'not_configured') return '<p>Сбор истории не настроен; приложение его автоматически не запускает.</p>';
  if (result.status !== 'available' || !Array.isArray(result.buckets)) return '<p>История недоступна: проверьте отдельную базу и настройки.</p>';
  const rows = result.buckets;
  const last = result.last_sample_at;
  const age = last == null ? null : Number(result.current_timestamp) - Number(last);
  const notice = result.retention_paused ? '<p role="alert">Автоматическая очистка истории приостановлена после перерыва; проверьте часы и инструкцию.</p>' : result.collection_gap ? '<p role="alert">Обнаружен перерыв: очистка приостановится при следующем сборе; проверьте часы и инструкцию.</p>' : '';
  const clock = result.clock_backwards ? '<p role="alert">Время сервера раньше сохранённых снимков. Сбор остановлен до исправления часов.</p>' : '';
  const stale = age == null ? '<p>Снимков пока нет.</p>' : age < -60 || age > 180 ? '<p role="alert">Последний сбор устарел или время сервера изменилось.</p>' : '';
  const pct = n => fmtPct(n);
  const charts = historyGraph(rows, [
    { field: 'cpu_pct', count: 'cpu_samples', className: 'cpu', label: 'CPU', format: pct },
    { field: 'memory_pct', count: 'memory_samples', className: 'memory', label: 'RAM', format: pct },
  ], 100, 'CPU и RAM; пропуски не соединяются');
  const swap = historyGraph(rows, [{ field: 'swap_pct', count: 'swap_samples', className: 'cpu', label: 'Swap', format: pct }],
    100, 'Swap; пропуски не соединяются');
  const maxNetwork = Math.max(1, ...rows.flatMap(row => [row.rx_bytes_per_sec, row.tx_bytes_per_sec])
    .filter(v => v != null && Number.isFinite(Number(v)) && Number(v) >= 0).map(Number));
  const net = result.interface ? historyGraph(rows, [
    { field: 'rx_bytes_per_sec', count: 'rx_samples', className: 'cpu', label: 'Вход', format: n => fmtBytes(n) + '/с' },
    { field: 'tx_bytes_per_sec', count: 'tx_samples', className: 'memory', label: 'Выход', format: n => fmtBytes(n) + '/с' },
  ], maxNetwork, 'Скорости выбранного интерфейса; пропуски не соединяются') : '';
  const table = rows.map(row => `<tr><td>${esc(historyTime(row.timestamp))}</td><td>${esc(fmtPct(row.cpu_pct))} (${esc(row.cpu_samples)}/${esc(row.expected)})</td>
    <td>${esc(fmtPct(row.memory_pct))} (${esc(row.memory_samples)}/${esc(row.expected)})</td><td>${esc(fmtPct(row.swap_pct))}</td>
    <td>${row.rx_bytes_per_sec == null ? '—' : esc(fmtBytes(row.rx_bytes_per_sec)) + '/с'}</td><td>${row.tx_bytes_per_sec == null ? '—' : esc(fmtBytes(row.tx_bytes_per_sec)) + '/с'}</td></tr>`).join('');
  return `${notice}${clock}${stale}<p>Последний снимок: ${last == null ? '—' : esc(historyTime(last))} (время устройства). Интерфейс: ${esc(result.interface || 'не выбран')}.</p>
    <p class="form-hint">Точки — средние наблюдённых значений за интервал. Пропуски и неполные интервалы не соединяются; CPU и RAM подписаны в таблице числом наблюдений. График сети — скорость, не биллинг.</p>
    <p>CPU (синий) · RAM (зелёный)</p>${charts}
    <p>Swap — процент занятого от размера swap, отдельно от RAM</p>${swap}
    ${net ? `<p>Вход (синий) · выход (зелёный), шкала до ${esc(fmtBytes(maxNetwork))}/с</p>${net}` : '<p>История сети недоступна без выбранного интерфейса.</p>'}
    <details><summary>Таблица интервалов</summary><div class="table-wrap"><table class="data"><thead><tr><th>Время</th><th>CPU</th><th>RAM</th><th>Swap</th><th>Вход</th><th>Выход</th></tr></thead><tbody>${table}</tbody></table></div></details>
    ${extraHistoryHtml(result, rows)}`;
}

function processHtml(result) {
  if (result.status !== 'available' || !Array.isArray(result.top_cpu) || !Array.isArray(result.top_memory)) {
    return '<p>Снимок процессов недоступен.</p>';
  }
  const list = (rows, cpu) => rows.map(row => `<li>PID ${esc(row.pid)} · ${esc(row.name || '—')} · ${esc(row.state)}: ${cpu
    ? `CPU ${esc(fmtPct(row.cpu_pct))}` : `RAM ${esc(fmtBytes(row.rss_bytes))} (${esc(fmtPct(row.memory_pct))})`}</li>`).join('');
  const partial = result.partial ? 'Список неполный: часть процессов изменилась, недоступна или превышен предел 256.'
    : 'Показаны только процессы, видимые приложению; procfs может скрывать другие.';
  return `<p>${esc(partial)} Видимо: ${esc(fmtInt(result.visible))}; зомби среди видимых: ${esc(fmtInt(result.zombies))}.</p>
    <p class="form-hint">CPU за короткий интервал: 100 % — одно логическое ядро, больше 100 % возможно при нескольких потоках. Недавно появившиеся или завершившиеся процессы могут отсутствовать. RSS включает общую память; её нельзя суммировать как уникальную.</p>
    <p><strong>По CPU (до 5)</strong></p><ul>${list(result.top_cpu, true) || '<li>Нет достаточно данных.</li>'}</ul>
    <p><strong>По памяти (до 5)</strong></p><ul>${list(result.top_memory, false) || '<li>Нет данных.</li>'}</ul>`;
}

export function localServerWire() {
  const button = document.getElementById('server-monitor-refresh');
  const trafficButton = document.getElementById('server-traffic-refresh');
  const processButton = document.getElementById('server-processes-refresh');
  const historyButton = document.getElementById('server-history-refresh');
  const mountsButton = document.getElementById('server-mounts-refresh');
  if (!button || !trafficButton || !processButton || !historyButton || !mountsButton) return;
  mountsButton.onclick = async () => {
    const generation = localGeneration;
    const panel = document.getElementById('server-mounts');
    mountsButton.disabled = true;
    panel.textContent = 'Чтение монтирований…';
    try {
      const result = await api.get('/api/v1/admin/server-monitor/mounts');
      if (generation !== localGeneration || !mountsButton.isConnected) return;
      panel.innerHTML = mountsHtml(result);
    } catch (e) {
      if (generation !== localGeneration || !mountsButton.isConnected) return;
      panel.textContent = 'Не удалось получить монтирования: ' + (e.message || e);
    } finally {
      if (generation === localGeneration && mountsButton.isConnected) mountsButton.disabled = false;
    }
  };
  historyButton.onclick = async () => {
    const generation = localGeneration;
    const panel = document.getElementById('server-history');
    const period = document.getElementById('server-history-period').value;
    historyButton.disabled = true;
    panel.textContent = 'Чтение истории…';
    try {
      const result = await api.get('/api/v1/admin/server-monitor/history?period=' + encodeURIComponent(period));
      if (generation !== localGeneration || !historyButton.isConnected) return;
      panel.innerHTML = historyHtml(result);
    } catch (e) {
      if (generation !== localGeneration || !historyButton.isConnected) return;
      panel.textContent = 'Не удалось прочитать историю: ' + (e.message || e);
    } finally {
      if (generation === localGeneration && historyButton.isConnected) historyButton.disabled = false;
    }
  };
  processButton.onclick = async () => {
    const generation = localGeneration;
    const panel = document.getElementById('server-processes');
    processButton.disabled = true;
    panel.textContent = 'Замер процессов…';
    try {
      const result = await api.get('/api/v1/admin/server-monitor/processes');
      if (generation !== localGeneration || !processButton.isConnected) return;
      panel.innerHTML = processHtml(result);
    } catch (e) {
      if (generation !== localGeneration || !processButton.isConnected) return;
      panel.textContent = 'Не удалось получить процессы: ' + (e.message || e);
    } finally {
      if (generation === localGeneration && processButton.isConnected) processButton.disabled = false;
    }
  };
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
        + 'История производительности включается отдельно и автоматически не собирается; пороги не настраиваются.';
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
