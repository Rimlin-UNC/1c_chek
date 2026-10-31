// ======================================================================
// Ямастер Чек — карточка «Мониторинг OpenStack» (v1.70.51)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
//
// Техническое задание: docs/Monitor.md, разделы 3, 4, 5, 9 и 10.
// Четыре вкладки одной карточки настроек: «Подключение», «Пороги и опрос»,
// «Показатели» и «Журнал». Разметка вкладок честная — role=tablist/tab/
// tabpanel, переключение кликом и стрелками, неактивная панель скрыта.
//
// Графики строит Chart.js (вендорен в /js/vendor/chart.umd.js). Если
// библиотека не загрузилась, карточка показывает текст вместо графика и
// не падает: остальные вкладки продолжают работать.
//
// Автообновление раз в 30 секунд работает только на открытой вкладке
// «Показатели» и само останавливается, когда карточка исчезла из DOM —
// тот же приём, что в блоке истории версий, поэтому ушедший со страницы
// администратор не оставляет за собой опрос сервера.
// ======================================================================
import { api } from './api.js';
import { esc, fmtDate, fmtInt, toast } from './ui.js';

const PREFIX = '/api/v1/openstack';

// ТЗ, раздел 9: автообновление каждые 30 секунд.
const REFRESH_MS = 30000;

// Цвета серий — те же, что закреплены за графиками проекта в charts.js
// (tests/test_v17035 требует контраст не ниже 3:1 для графики).
const COLOR = {
  cpu: '#4b0082',
  ram: '#237a90',
  disk: '#2e8b57',
  netIn: '#9a4a00',
  netOut: '#27764a',
};

// Подписи осей и сетка берутся из токенов темы, а не из литералов: иначе
// новый цвет мог бы молча разойтись с --text-faint и --border, которые
// закреплены тестами контраста. Запасное значение — сам токен на случай,
// если вычисленные стили недоступны (например, вне браузера).
function cssToken(name, fallback) {
  try {
    const value = getComputedStyle(document.documentElement)
      .getPropertyValue(name).trim();
    return value || fallback;
  } catch (e) { return fallback; }
}

const tickColor = () => cssToken('--text-faint', '#6b6b6b');
const gridColor = () => cssToken('--border', 'rgba(51, 51, 51, .12)');

// ТЗ, раздел 9: выбор периода 1ч / 24ч / 7д / 30д.
const PERIODS = [
  { id: '1h', label: '1 ч' },
  { id: '24h', label: '24 ч' },
  { id: '7d', label: '7 д' },
  { id: '30d', label: '30 д' },
];

// ТЗ, раздел 10: фильтры журнала по уровню.
const LEVELS = [
  { id: '', label: 'Все' },
  { id: 'INFO', label: 'INFO' },
  { id: 'WARN', label: 'WARN' },
  { id: 'ERROR', label: 'ERROR' },
];

const AUTH_TYPE_LABELS = {
  password: 'Пароль',
  application_credential: 'Учётные данные приложения',
};

let timer = null;
let charts = [];
let period = '24h';
let logLevel = '';
let reload = null;

// --- Форматирование -------------------------------------------------------

function fmtBytes(value) {
  if (value === null || value === undefined || value === '') return '—';
  const n = Number(value) || 0;
  if (n >= 1073741824) return (n / 1073741824).toFixed(2) + ' ГБ';
  if (n >= 1048576) return (n / 1048576).toFixed(1) + ' МБ';
  if (n >= 1024) return (n / 1024).toFixed(1) + ' КБ';
  return fmtInt(n) + ' Б';
}

function fmtMb(value) {
  const n = Number(value) || 0;
  if (n >= 1024) return (n / 1024).toFixed(1) + ' ГБ';
  return fmtInt(n) + ' МБ';
}

function fmtGb(value) {
  const n = Number(value) || 0;
  if (n >= 1024) return (n / 1024).toFixed(2) + ' ТБ';
  return fmtInt(n) + ' ГБ';
}

function fmtPct(value) {
  const n = Number(value) || 0;
  return n.toFixed(2).replace('.', ',') + ' %';
}

// ТЗ, раздел 9: зелёный — норма, жёлтый от порога, красный от порога + 10 %.
function levelOf(pctValue, limit) {
  const limitNum = Number(limit) || 0;
  if (limitNum <= 0) return 'osm-ok';
  const value = Number(pctValue) || 0;
  if (value >= limitNum + 10) return 'osm-bad';
  if (value >= limitNum) return 'osm-warn';
  return 'osm-ok';
}

function levelClass(level) {
  const key = String(level || '').toUpperCase();
  if (key === 'ERROR') return 'error';
  if (key === 'WARN') return 'warn';
  return 'info';
}

function shortTime(iso) {
  if (!iso) return '—';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '—';
  return d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit',
    hour: '2-digit', minute: '2-digit' });
}

// --- Разбор openrc.sh (v1.70.52) ------------------------------------------
// Панели провайдеров отдают готовый файл openrc.sh со всеми параметрами
// Keystone, кроме секрета (а иногда и с ним). Разбор идёт СТРОГО в браузере:
// содержимое файла не отправляется на сервер, поэтому секрет не попадает ни
// в журнал запросов, ни в ответ API — в поле формы он ложится только в DOM.
export function parseOpenrc(text) {
  const values = {};
  const lines = String(text || '').split(/\r?\n/);
  for (const raw of lines) {
    const line = raw.trim();
    if (!line || line.charAt(0) === '#') continue;
    const m = /^\s*(?:export\s+)?(OS_[A-Z0-9_]+)\s*=\s*(.*)$/.exec(line);
    if (!m) continue;
    let value = m[2].trim();
    const quote = value.charAt(0);
    if ((quote === '"' || quote === "'") && value.length > 1
        && value.charAt(value.length - 1) === quote) {
      value = value.slice(1, -1);
    }
    values[m[1]] = value;
  }
  return values;
}

// Раскладка разобранных переменных по полям формы. Заполняются только те
// поля, значение для которых нашлось: отсутствующие переменные не должны
// стирать то, что администратор уже ввёл руками.
export function applyOpenrc(values) {
  const filled = [];
  const put = (id, value, label) => {
    const el = document.getElementById(id);
    if (!el || !value) return;
    el.value = value;
    filled.push(label);
  };
  const appCred = !!values.OS_APPLICATION_CREDENTIAL_ID;
  const authType = document.getElementById('osm-auth-type');
  if (authType && (appCred || values.OS_USERNAME)) {
    authType.value = appCred ? 'application_credential' : 'password';
    syncAuthTypeVisibility();
    filled.push('способ аутентификации');
  }
  put('osm-auth-url', values.OS_AUTH_URL, 'адрес Keystone');
  put('osm-username', values.OS_USERNAME, 'имя пользователя');
  put('osm-app-cred-id', values.OS_APPLICATION_CREDENTIAL_ID,
    'идентификатор учётных данных');
  put('osm-secret', values.OS_APPLICATION_CREDENTIAL_SECRET || values.OS_PASSWORD,
    'секрет');
  put('osm-project', values.OS_PROJECT_NAME || values.OS_TENANT_NAME, 'проект');
  put('osm-udn', values.OS_USER_DOMAIN_NAME, 'домен пользователя');
  put('osm-pdn', values.OS_PROJECT_DOMAIN_NAME, 'домен проекта');
  // v1.70.53: панели провайдеров отдают файл c OS_PROJECT_DOMAIN_ID —
  // домен идентификатором. У имени и идентификатора разные поля: файл
  // может содержать только одно из двух.
  put('osm-udn-id', values.OS_USER_DOMAIN_ID, 'домен пользователя по ID');
  put('osm-pdn-id', values.OS_PROJECT_DOMAIN_ID, 'домен проекта по ID');
  put('osm-region', values.OS_REGION_NAME, 'регион');
  const iface = document.getElementById('osm-interface');
  if (iface && ['public', 'internal', 'admin'].includes(values.OS_INTERFACE)) {
    iface.value = values.OS_INTERFACE;
    filled.push('интерфейс каталога');
  }
  return filled;
}

// --- Разметка -------------------------------------------------------------

function field(id, label, value, attrs = '', hint = '') {
  return `<label class="field"><span>${esc(label)}</span>`
    + `<input id="${id}" value="${esc(value ?? '')}" ${attrs}>`
    + (hint ? `<span class="form-hint">${esc(hint)}</span>` : '')
    + '</label>';
}

function selectField(id, label, options, current) {
  const items = options.map(([value, text]) =>
    `<option value="${esc(value)}"${String(value) === String(current) ? ' selected' : ''}>${esc(text)}</option>`).join('');
  return `<label class="field"><span>${esc(label)}</span>`
    + `<select id="${id}">${items}</select></label>`;
}

function checkbox(id, label, checked, extraClass = '') {
  return `<label class="${extraClass}" style="display:flex;gap:10px;align-items:center;`
    + `cursor:pointer;margin:6px 0 8px"><input type="checkbox" id="${id}"`
    + `${checked ? ' checked' : ''} style="width:auto"><span>${esc(label)}</span></label>`;
}

function tabsHtml(active) {
  const tabs = [
    ['conn', 'Подключение'],
    ['limits', 'Пороги и опрос'],
    ['metrics', 'Показатели'],
    ['log', 'Журнал'],
  ];
  return tabs.map(([id, label]) => {
    const on = id === active;
    return `<button type="button" role="tab" class="osm-tab" id="osm-tab-${id}"`
      + ` aria-controls="osm-panel-${id}" aria-selected="${on ? 'true' : 'false'}"`
      + ` tabindex="${on ? '0' : '-1'}">${esc(label)}</button>`;
  }).join('');
}

function connPanel(set) {
  const isAppCred = set.auth_type === 'application_credential';
  const secretHint = set.secret_set ? 'сохранён — пустое поле не меняет его'
    : 'не задан';
  return `
  <label class="field full"><span>Содержимое openrc.sh — заполняет поля само</span>
    <textarea id="osm-openrc" rows="5" placeholder="export OS_AUTH_URL=https://cloud.example.ru:5000/v3&#10;export OS_USERNAME=…&#10;export OS_PROJECT_NAME=…"></textarea>
    <span class="form-hint">Файл openrc.sh скачивается в панели провайдера на
      вкладке API рядом с логином и паролем. Разбирается в браузере и никуда
      не отправляется: секрет остаётся на вашем устройстве, пока вы не
      нажмёте «Сохранить». Переменные, которых в файле нет, не стирают то,
      что уже введено руками.</span></label>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin:0 0 12px">
    <button type="button" class="btn btn-sm" id="osm-openrc-apply">📋 Заполнить из openrc.sh</button>
  </div>
  <div class="form-grid">
    <label class="field full"><span>Адрес Keystone (auth_url)</span>
      <input id="osm-auth-url" value="${esc(set.auth_url || '')}"
        placeholder="https://cloud.example.ru/identity/v3"></label>
    ${selectField('osm-auth-type', 'Способ аутентификации (auth_type)',
      Object.entries(AUTH_TYPE_LABELS), set.auth_type)}
    ${selectField('osm-interface', 'Интерфейс каталога (interface)',
      [['public', 'public'], ['internal', 'internal'], ['admin', 'admin']],
      set.interface)}
    <label class="field" data-auth="password"${isAppCred ? ' hidden' : ''}><span>Имя пользователя (username)</span>
      <input id="osm-username" value="${esc(set.username || '')}" autocomplete="off"></label>
    <label class="field" data-auth="app_cred"${isAppCred ? '' : ' hidden'}><span>Идентификатор учётных данных (app_cred_id)</span>
      <input id="osm-app-cred-id" value="${esc(set.app_cred_id || '')}" autocomplete="off"></label>
    <label class="field full"><span>Секрет (${isAppCred ? 'app_cred_secret' : 'password'})</span>
      <input id="osm-secret" type="password" autocomplete="new-password"
        placeholder="пусто — не менять">
      <span class="form-hint">${esc(secretHint)}</span></label>
    ${field('osm-project', 'Проект (project_name)', set.project_name)}
    ${field('osm-udn', 'Домен пользователя (user_domain_name)', set.user_domain_name || 'Default')}
    ${field('osm-pdn', 'Домен проекта (project_domain_name)', set.project_domain_name || 'Default')}
    ${field('osm-udn-id', 'Домен пользователя по ID (user_domain_id)', set.user_domain_id || '', '', 'Обычно пусто. Идентификатор важнее имени: поле заполняется само, если файл провайдера содержит OS_USER_DOMAIN_ID.')}
    ${field('osm-pdn-id', 'Домен проекта по ID (project_domain_id)', set.project_domain_id || '', '', 'Панели провайдеров часто отдают файл с OS_PROJECT_DOMAIN_ID вместо имени — тогда заполняется это поле, и облако получает домен ровно так, как записано у провайдера.')}
    ${field('osm-region', 'Регион (region_name)', set.region_name, '', 'например, RegionOne')}
    <label class="field"><span>Таймаут, секунд (timeout_sec)</span>
      <input id="osm-timeout" type="number" min="1" max="300" step="1"
        value="${esc(set.timeout_sec ?? 30)}"></label>
    <label class="field full"><span>UUID провайдера ресурсов (resource_provider_uuid)</span>
      <input id="osm-rp-uuid" value="${esc(set.resource_provider_uuid || '')}"
        placeholder="пусто — инвентарь Placement не читается">
      <span class="form-hint">Необязательно: нужен для точного инвентаря Placement.</span></label>
  </div>
  ${checkbox('osm-verify-tls', 'Проверять сертификат TLS (verify_tls)', set.verify_tls !== false)}
  <p class="osm-tls-warn"${set.verify_tls === false ? '' : ' hidden'} id="osm-tls-warn">
    Проверка сертификата выключена: подключение к Keystone не защищено от
    подмены, а параметры передачи доступны всем, кто видит этот трафик.
    Отключать проверку стоит только в закрытом контуре и только осознанно.</p>
  ${checkbox('osm-enabled', 'Включить мониторинг: опрос облака по расписанию', !!set.enabled)}
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:4px">
    <button type="button" class="btn btn-primary btn-sm" id="osm-save">💾 Сохранить</button>
    <button type="button" class="btn btn-sm" id="osm-test">🔌 Проверить подключение</button>
    <button type="button" class="btn btn-sm" id="osm-disable">⏹ Отключить мониторинг</button>
  </div>
  <div id="osm-test-result" style="margin-top:10px"></div>`;
}

function limitsPanel(set) {
  const th = set.thresholds || {};
  return `
  <p class="form-hint" style="margin-bottom:10px">Интервал опроса — от 30 до
    3600 секунд. Глубина хранения задаёт, за какой период доступны графики;
    снимки старше этого срока удаляются автоматически. Порог 0 % отключает
    проверку по показателю.</p>
  <div class="form-grid">
    <label class="field"><span>Интервал опроса, секунд (interval_sec)</span>
      <input id="osm-interval" type="number" min="30" max="3600" step="1"
        value="${esc(set.interval_sec ?? 60)}"></label>
    <label class="field"><span>Глубина хранения, дней (history_days)</span>
      <input id="osm-history" type="number" min="1" max="365" step="1"
        value="${esc(set.history_days ?? 30)}"></label>
    <label class="field"><span>Порог процессора, %</span>
      <input id="osm-th-cpu" type="number" min="0" max="100" step="1"
        value="${esc(th.threshold_cpu_pct ?? 80)}"></label>
    <label class="field"><span>Порог оперативной памяти, %</span>
      <input id="osm-th-ram" type="number" min="0" max="100" step="1"
        value="${esc(th.threshold_ram_pct ?? 80)}"></label>
    <label class="field"><span>Порог диска, %</span>
      <input id="osm-th-disk" type="number" min="0" max="100" step="1"
        value="${esc(th.threshold_disk_pct ?? 80)}"></label>
    <label class="field"><span>Порог трафика, ГБ в сутки</span>
      <input id="osm-th-traffic" type="number" min="0" step="0.5"
        value="${esc(th.threshold_traffic_gb_day ?? 0)}">
      <span class="form-hint">0 — не проверять</span></label>
  </div>
  <div style="display:flex;gap:8px;flex-wrap:wrap;margin-top:6px">
    <button type="button" class="btn btn-primary btn-sm" id="osm-save-limits">💾 Сохранить пороги и опрос</button>
  </div>`;
}

function metricsPanel() {
  const periodButtons = PERIODS.map(p =>
    `<button type="button" data-period="${p.id}"${p.id === period ? ' class="active"' : ''}>${esc(p.label)}</button>`).join('');
  return `
  <div id="osm-warnings"></div>
  <div class="grid kpi-grid" id="osm-kpi"></div>
  <div class="osm-periods" style="margin-top:14px">
    <div class="segmented" role="group" aria-label="Период истории">${periodButtons}</div>
    <span class="spacer"></span>
    <button type="button" class="btn btn-sm" id="osm-refresh">🔄 Обновить</button>
  </div>
  <p class="form-hint" style="margin-top:10px">Загрузка процессора, оперативной
    памяти и диска, % от ёмкости</p>
  <div class="osm-chart"><canvas id="osm-chart-util"></canvas></div>
  <p class="form-hint">Сетевой трафик балансировщиков, байт в секунду периода</p>
  <div class="osm-chart"><canvas id="osm-chart-net"></canvas></div>
  <p class="form-hint" id="osm-metrics-note"></p>`;
}

function logPanel() {
  const levelButtons = LEVELS.map(l =>
    `<button type="button" data-level="${esc(l.id)}"${l.id === logLevel ? ' class="active"' : ''}>${esc(l.label)}</button>`).join('');
  return `
  <div class="osm-periods" style="margin-bottom:10px">
    <div class="segmented" role="group" aria-label="Фильтр по уровню">${levelButtons}</div>
    <span class="spacer"></span>
    <button type="button" class="btn btn-sm" id="osm-log-refresh">🔄 Обновить</button>
  </div>
  <div class="table-wrap">
    <table class="data" style="min-width:520px">
      <thead><tr><th>Время</th><th>Уровень</th><th>Сообщение</th><th>Код ошибки</th></tr></thead>
      <tbody id="osm-log-body"><tr><td colspan="4" class="form-hint">Загрузка…</td></tr></tbody>
    </table>
  </div>
  <p class="form-hint" style="margin-top:8px">Секреты и токены в журнал не
    попадают: значения учётных данных заменяются маской до записи.</p>`;
}

export function osmCardHtml(set, status) {
  const st = status || {};
  const chipClass = st.last_error ? 'failed' : (st.enabled ? 'verified' : 'unknown');
  const chipText = st.last_error ? 'ошибка синхронизации'
    : (st.enabled ? 'опрос включён' : 'мониторинг выключен');
  return `
      <div class="glass card" id="st-openstack" data-sec="integr" style="order:39">
        <div class="card-title">☁️ Мониторинг OpenStack
          <span class="form-hint">(v1.70.51)</span>
          <span class="spacer"></span>
          <span class="chip ${chipClass}"><span class="dot"></span>${esc(chipText)}</span>
        </div>
        <p class="form-hint" style="margin-bottom:10px">Показатели облака
          OpenStack: загрузка процессоров, оперативной памяти и дисков, квоты
          проекта и сетевой трафик. Программа подключается к Keystone по
          штатному openstacksdk, снимок пишется в базу и показывается здесь.
          Блок доступен администратору.</p>
        <div class="osm-tabs" role="tablist" aria-label="Мониторинг OpenStack">
          ${tabsHtml('conn')}
        </div>
        <div role="tabpanel" class="osm-panel" id="osm-panel-conn"
          aria-labelledby="osm-tab-conn" tabindex="0">${connPanel(set)}</div>
        <div role="tabpanel" class="osm-panel" id="osm-panel-limits" hidden
          aria-labelledby="osm-tab-limits" tabindex="0">${limitsPanel(set)}</div>
        <div role="tabpanel" class="osm-panel" id="osm-panel-metrics" hidden
          aria-labelledby="osm-tab-metrics" tabindex="0">${metricsPanel()}</div>
        <div role="tabpanel" class="osm-panel" id="osm-panel-log" hidden
          aria-labelledby="osm-tab-log" tabindex="0">${logPanel()}</div>
      </div>`;
}

// --- Вкладки --------------------------------------------------------------

function syncAuthTypeVisibility() {
  const authType = document.getElementById('osm-auth-type');
  if (!authType) return;
  const appCred = authType.value === 'application_credential';
  document.querySelectorAll('#osm-panel-conn [data-auth="password"]')
    .forEach(el => { el.hidden = appCred; });
  document.querySelectorAll('#osm-panel-conn [data-auth="app_cred"]')
    .forEach(el => { el.hidden = !appCred; });
  const secretLabel = document.querySelector('#osm-panel-conn #osm-secret')
    ?.parentElement?.querySelector('span');
  if (secretLabel) {
    secretLabel.textContent = appCred ? 'Секрет (app_cred_secret)'
      : 'Секрет (password)';
  }
}

function wireTabs() {
  const tablist = document.querySelector('#st-openstack [role="tablist"]');
  if (!tablist) return;
  const tabs = Array.from(tablist.querySelectorAll('[role="tab"]'));

  const activate = (tab, focus) => {
    tabs.forEach((t) => {
      const on = t === tab;
      t.setAttribute('aria-selected', on ? 'true' : 'false');
      t.tabIndex = on ? 0 : -1;
      const panel = document.getElementById(t.getAttribute('aria-controls'));
      if (panel) panel.hidden = !on;
    });
    if (focus) tab.focus();
    const id = tab.id.replace('osm-tab-', '');
    if (id === 'metrics') loadMetrics();
    if (id === 'log') loadLogs();
    if (id !== 'metrics') destroyCharts();
  };

  tabs.forEach((tab) => {
    tab.addEventListener('click', () => activate(tab, false));
    tab.addEventListener('keydown', (e) => {
      const i = tabs.indexOf(tab);
      let next = -1;
      if (e.key === 'ArrowRight') next = (i + 1) % tabs.length;
      else if (e.key === 'ArrowLeft') next = (i - 1 + tabs.length) % tabs.length;
      else if (e.key === 'Home') next = 0;
      else if (e.key === 'End') next = tabs.length - 1;
      if (next >= 0) { e.preventDefault(); activate(tabs[next], true); }
    });
  });
}

// --- Графики --------------------------------------------------------------

function destroyCharts() {
  charts.forEach((c) => { try { c.destroy(); } catch (e) { /* уже уничтожен */ } });
  charts = [];
}

function chartFallback(canvasId, text) {
  const canvas = document.getElementById(canvasId);
  if (!canvas) return;
  const box = canvas.parentElement;
  box.innerHTML = `<div class="osm-chart-empty">${esc(text)}</div>`;
}

function baseOptions(yLabel, yMax, yCallback) {
  return {
    responsive: true,
    maintainAspectRatio: false,
    animation: { duration: 240 },
    interaction: { mode: 'index', intersect: false },
    plugins: {
      legend: { labels: { color: tickColor(), boxWidth: 12, font: { size: 11 } } },
      tooltip: { enabled: true },
    },
    scales: {
      x: { ticks: { color: tickColor(), maxTicksLimit: 8, font: { size: 11 } },
        grid: { color: gridColor() } },
      y: { beginAtZero: true, max: yMax, title: { display: !!yLabel, text: yLabel,
        color: tickColor(), font: { size: 11 } },
        ticks: { color: tickColor(), font: { size: 11 }, callback: yCallback },
        grid: { color: gridColor() } },
    },
  };
}

function renderCharts(points) {
  destroyCharts();
  if (!window.Chart) {
    chartFallback('osm-chart-util', 'Библиотека графиков не загрузилась: '
      + 'числовые значения доступны в карточках выше.');
    chartFallback('osm-chart-net', 'Библиотека графиков не загрузилась.');
    return;
  }
  if (!points || !points.length) {
    chartFallback('osm-chart-util', 'За выбранный период снимков нет.');
    chartFallback('osm-chart-net', 'За выбранный период снимков нет.');
    return;
  }

  const labels = points.map(p => shortTime(p.collected_at));
  const share = (used, total) => points.map((p) => {
    const t = Number(p[total]) || 0;
    const u = Number(p[used]) || 0;
    return t > 0 ? Math.round((u / t) * 10000) / 100 : 0;
  });
  const line = (label, data, color) => ({
    label, data, borderColor: color, backgroundColor: color,
    borderWidth: 2, pointRadius: data.length > 60 ? 0 : 2,
    tension: 0.25, fill: false,
  });

  const utilCanvas = document.getElementById('osm-chart-util');
  if (utilCanvas) {
    charts.push(new window.Chart(utilCanvas.getContext('2d'), {
      type: 'line',
      data: { labels, datasets: [
        line('Процессор', share('vcpus_used', 'vcpus_total'), COLOR.cpu),
        line('Оперативная память', share('ram_used_mb', 'ram_total_mb'), COLOR.ram),
        line('Диск', share('disk_used_gb', 'disk_total_gb'), COLOR.disk),
      ] },
      options: baseOptions('%', 100, v => v + ' %'),
    }));
  }

  const netCanvas = document.getElementById('osm-chart-net');
  if (netCanvas) {
    const hasNet = points.some(p => p.traffic_in_bytes !== null
      && p.traffic_in_bytes !== undefined);
    if (!hasNet) {
      chartFallback('osm-chart-net', 'Трафик не собирается: в облаке нет '
        + 'балансировщиков Octavia или сервис недоступен.');
    } else {
      charts.push(new window.Chart(netCanvas.getContext('2d'), {
        type: 'line',
        data: { labels, datasets: [
          line('Входящий', points.map(p => Number(p.traffic_in_bytes) || 0), COLOR.netIn),
          line('Исходящий', points.map(p => Number(p.traffic_out_bytes) || 0), COLOR.netOut),
        ] },
        options: baseOptions('', undefined, v => fmtBytes(v)),
      }));
    }
  }
}

// --- Панель «Показатели» ---------------------------------------------------

function kpi(title, value, sub, cls) {
  return `<div class="glass kpi ${cls || ''}">
      <div class="kpi-label">${esc(title)}</div>
      <div class="kpi-value">${esc(value)}</div>
      <div class="kpi-sub">${esc(sub)}</div>
    </div>`;
}

function renderWarnings(box, view) {
  if (!box) return;
  const list = view.warnings || [];
  if (!list.length) {
    box.innerHTML = view.available === false
      ? '<div class="osm-alert warn">⚠<div>Снимков пока нет: опрос не выполнялся '
        + 'или подключение не настроено.</div></div>'
      : '';
    return;
  }
  box.innerHTML = `<div class="osm-alert bad">⚠<div><b>Превышены пороги</b>`
    + `<ul>${list.map(w => `<li>${esc(w)}</li>`).join('')}</ul></div></div>`;
}

function renderKpi(view, thresholds) {
  const box = document.getElementById('osm-kpi');
  if (!box) return;
  const th = thresholds || {};
  const cpu = view.cpu || {}, ram = view.ram || {}, disk = view.disk || {};
  const q = view.quotas || {}, net = view.traffic || {};
  const st = view.status || {};

  const parts = [
    kpi('Процессор', fmtPct(cpu.pct),
      `${fmtInt(cpu.used)} из ${fmtInt(cpu.total)} vCPU`,
      levelOf(cpu.pct, th.threshold_cpu_pct)),
    kpi('Оперативная память', fmtPct(ram.pct),
      `занято ${fmtMb(ram.used_mb)} из ${fmtMb(ram.total_mb)}, `
        + `свободно ${fmtMb(ram.free_mb)}`,
      levelOf(ram.pct, th.threshold_ram_pct)),
    kpi('Диск', fmtPct(disk.pct),
      `занято ${fmtGb(disk.used_gb)} из ${fmtGb(disk.total_gb)}, `
        + `свободно ${fmtGb(disk.free_gb)}`,
      levelOf(disk.pct, th.threshold_disk_pct)),
    kpi('Квоты проекта', `${fmtInt(q.used_cores ?? 0)} / ${fmtInt(q.max_cores ?? 0)}`,
      `ядер; память ${fmtMb(q.used_ram_mb)} из ${fmtMb(q.max_ram_mb)}; `
        + `инстансов ${fmtInt(q.used_instances ?? 0)} из ${fmtInt(q.max_instances ?? 0)}`),
    kpi('Сеть', net.in_bytes === null || net.in_bytes === undefined
      ? '—' : fmtBytes(net.in_bytes),
      net.in_bytes === null || net.in_bytes === undefined
        ? 'трафик не собирается'
        : `входящий; исходящий ${fmtBytes(net.out_bytes)}`),
    kpi('Статус', st.last_success_at ? fmtDate(st.last_success_at) : '—',
      st.last_error ? `ошибка: ${st.last_error}` : 'последний успешный сбор'),
  ];
  box.innerHTML = parts.join('');
}

async function loadMetrics() {
  const panel = document.getElementById('osm-panel-metrics');
  if (!panel || panel.hidden) return;
  try {
    const cur = await api.get(`${PREFIX}/metrics/current`);
    const hist = await api.get(`${PREFIX}/metrics/history?period=${encodeURIComponent(period)}`);
    renderWarnings(document.getElementById('osm-warnings'), cur);
    renderKpi({ ...cur, status: cur.status || {} }, hist.thresholds);
    renderCharts(hist.points);
    const note = document.getElementById('osm-metrics-note');
    if (note) {
      note.textContent = `Точек за период: ${hist.count}`
        + (hist.step_sec ? `, укрупнение ${hist.step_sec} с` : '')
        + `. Период: ${PERIODS.find(p => p.id === period)?.label || period}.`
        + ` Автообновление раз в ${REFRESH_MS / 1000} с.`;
    }
  } catch (e) {
    const note = document.getElementById('osm-metrics-note');
    if (note) note.textContent = 'Не удалось загрузить показатели: ' + (e.message || e);
  }
}

// --- Панель «Журнал» -------------------------------------------------------

async function loadLogs() {
  const body = document.getElementById('osm-log-body');
  if (!body) return;
  const suffix = logLevel ? `&level=${encodeURIComponent(logLevel)}` : '';
  try {
    const r = await api.get(`${PREFIX}/logs?limit=200${suffix}`);
    if (!r.logs || !r.logs.length) {
      body.innerHTML = '<tr><td colspan="4" class="form-hint">Записей нет.</td></tr>';
      return;
    }
    body.innerHTML = r.logs.map((row) => {
      const cls = levelClass(row.level);
      return `<tr style="cursor:default">
        <td style="white-space:nowrap">${esc(fmtDate(row.ts))}</td>
        <td><span class="osm-lvl ${cls}">${esc(row.level)}</span></td>
        <td>${esc(row.message)}</td>
        <td>${row.error_code ? `<span class="osm-code">${esc(row.error_code)}</span>` : '—'}</td>
      </tr>`;
    }).join('');
  } catch (e) {
    body.innerHTML = `<tr><td colspan="4" class="form-hint">Ошибка загрузки: ${esc(e.message || e)}</td></tr>`;
  }
}

// --- Действия --------------------------------------------------------------

function readConnectionForm() {
  const authType = document.getElementById('osm-auth-type')?.value || 'password';
  return {
    auth_url: document.getElementById('osm-auth-url')?.value.trim() || '',
    auth_type: authType,
    username: document.getElementById('osm-username')?.value.trim() || '',
    app_cred_id: document.getElementById('osm-app-cred-id')?.value.trim() || '',
    secret: document.getElementById('osm-secret')?.value || '',
    project_name: document.getElementById('osm-project')?.value.trim() || '',
    user_domain_name: document.getElementById('osm-udn')?.value.trim() || 'Default',
    project_domain_name: document.getElementById('osm-pdn')?.value.trim() || 'Default',
    user_domain_id: document.getElementById('osm-udn-id')?.value.trim() || '',
    project_domain_id: document.getElementById('osm-pdn-id')?.value.trim() || '',
    region_name: document.getElementById('osm-region')?.value.trim() || '',
    interface: document.getElementById('osm-interface')?.value || 'public',
    verify_tls: !!document.getElementById('osm-verify-tls')?.checked,
    timeout_sec: Number(document.getElementById('osm-timeout')?.value || 30),
    resource_provider_uuid: document.getElementById('osm-rp-uuid')?.value.trim() || '',
  };
}

function readLimitsForm() {
  return {
    interval_sec: Number(document.getElementById('osm-interval')?.value || 60),
    history_days: Number(document.getElementById('osm-history')?.value || 30),
    thresholds: {
      threshold_cpu_pct: Number(document.getElementById('osm-th-cpu')?.value || 0),
      threshold_ram_pct: Number(document.getElementById('osm-th-ram')?.value || 0),
      threshold_disk_pct: Number(document.getElementById('osm-th-disk')?.value || 0),
      threshold_traffic_gb_day: Number(document.getElementById('osm-th-traffic')?.value || 0),
    },
  };
}

function renderTestResult(result) {
  const box = document.getElementById('osm-test-result');
  if (!box) return;
  if (!result) { box.innerHTML = ''; return; }
  const cls = result.ok ? 'ok' : 'bad';
  const services = Object.entries(result.services || {});
  const chips = services.map(([name, state]) => {
    const chipCls = state === 'доступен' ? 'verified' : 'unknown';
    return `<span class="chip ${chipCls}"><span class="dot"></span>${esc(name)} — ${esc(state)}</span>`;
  }).join('');
  // v1.70.53: безликий HTTP 401 от провайдера администратор получил без
  // объяснений. Чек-лист по реальным причинам отказа — прямо под ошибкой.
  // v1.70.54: регион/интерфейс не совпали с каталогом облака — подсказка
  // ведёт администратора к строкам файла провайдера, а не к гаданию.
  const catalogIssue = !result.ok && (result.error_code === 'catalog'
    || /not present in the service catalog/i.test(result.error || ''));
  // v1.70.55: панель провайдера может противоречить собственному облаку —
  // каталог предложил одну пару, файл другую. Программа лишь ПРЕДЛАГАЕТ:
  // кнопка заполняет поля, решение и повтор проверки — за администратором.
  const suggestion = (result.catalog && result.catalog.suggestion) || null;
  const catalogHint = catalogIssue
    ? '<div class="form-hint">В каталоге облака нет сервиса для выбранных '
      + 'региона и интерфейса. Откройте файл провайдера: регион — строка '
      + 'OS_REGION_NAME, интерфейс — строка OS_INTERFACE. Перенесите эти '
      + 'значения в поля «Регион» и «Интерфейс каталога» и повторите '
      + 'проверку. Интерфейс для служебного доступа замените на тот, что '
      + 'указан в файле провайдера.'
      + (suggestion
        ? ` Если файл противоречит каталогу, верьте каталогу: он предлагает `
          + `регион «${esc(suggestion.region)}», интерфейс `
          + `«${esc(suggestion.interface)}». `
          + '<button type="button" class="btn btn-sm" id="osm-catalog-apply">'
          + 'Подставить из каталога</button>'
        : '')
      + '</div>'
    : '';
  // v1.70.57: если облако уже приняло учётные данные (authorized),
  // чек-лист про пароль бессмыслен — отказ владельца «Policy doesn't
  // allow …» при authorized:true показывал советы про секрет.
  const denied = !result.ok && result.authorized !== true
    && (result.error_code === 'auth_denied'
      || /HTTP 40[13]/.test(result.error || ''));
  const deniedHint = denied
    ? '<div class="form-hint">Облако не приняло учётные данные. Проверьте по '
      + 'порядку: 1) секрет — файл провайдера часто содержит заглушку из «x» '
      + 'вместо пароля: настоящее значение видно в панели провайдера за '
      + 'кнопкой «Показать», введите его в поле «Секрет» вручную; '
      + '2) при копировании секрета не должно быть лишних пробелов и перевода '
      + 'строки; 3) логин и проект — ровно как в файле провайдера; '
      + '4) домены — если в файле задан OS_PROJECT_DOMAIN_ID, поле «Домен '
      + 'проекта по ID» должно быть заполнено: идентификатор важнее имени.'
      + '</div>'
    : '';
  box.innerHTML = `<div class="osm-alert ${cls}">${result.ok ? '✓' : '✕'}<div>`
    + `<b>${result.ok ? 'Подключение прошло' : 'Подключение не прошло'}</b>`
    + (result.error ? `<div>${esc(result.error)}</div>` : '')
    + (result.error_code ? `<div>Код: <span class="osm-code">${esc(result.error_code)}</span></div>` : '')
    + (result.notice ? `<div class="form-hint">${esc(result.notice)}</div>` : '')
    + deniedHint
    + catalogHint
    + (chips ? `<div class="osm-services">${chips}</div>` : '')
    + `</div></div>`;
  // v1.70.55: подстановка предложения каталога в поля формы одним нажатием.
  const catalogApply = box.querySelector('#osm-catalog-apply');
  if (catalogApply && suggestion) {
    catalogApply.addEventListener('click', () => {
      const regionField = document.getElementById('osm-region');
      const ifaceField = document.getElementById('osm-interface');
      if (regionField) regionField.value = suggestion.region;
      if (ifaceField && ['public', 'internal', 'admin'].includes(suggestion.interface)) {
        ifaceField.value = suggestion.interface;
      }
      catalogApply.textContent = 'Подставлено';
      catalogApply.disabled = true;
      toast('Регион и интерфейс из каталога подставлены в поля — повторите '
        + 'проверку подключения', 'ok', '☁️');
    });
  }
}

async function saveSettings(withLimits) {
  const body = readConnectionForm();
  if (withLimits) Object.assign(body, readLimitsForm());
  body.enabled = !!document.getElementById('osm-enabled')?.checked;
  try {
    const r = await api.post(`${PREFIX}/settings`, body);
    toast('Настройки мониторинга сохранены', r.worker_alive ? 'ok' : 'warn', '☁️');
    if (typeof reload === 'function') reload();
  } catch (e) {
    toast(e.message || 'Не удалось сохранить', 'err');
  }
}

// --- Публичный интерфейс ---------------------------------------------------

export function osmWire(onReload) {
  reload = onReload || null;
  wireTabs();

  const authType = document.getElementById('osm-auth-type');
  if (authType) authType.onchange = () => syncAuthTypeVisibility();

  const openrcApply = document.getElementById('osm-openrc-apply');
  if (openrcApply) openrcApply.onclick = () => {
    const box = document.getElementById('osm-openrc');
    const filled = applyOpenrc(parseOpenrc(box ? box.value : ''));
    if (!filled.length) {
      toast('В тексте не найдено ни одной переменной OS_: вставьте содержимое '
        + 'файла openrc.sh целиком', 'err', '☁️');
      return;
    }
    toast(`По openrc.sh заполнено: ${filled.join(', ')}. Секрет и остальные `
      + 'поля проверьте и нажмите «Проверить подключение».', 'ok', '☁️');
  };

  const tls = document.getElementById('osm-verify-tls');
  if (tls) tls.onchange = () => {
    const warn = document.getElementById('osm-tls-warn');
    if (warn) warn.hidden = tls.checked;
  };

  const save = document.getElementById('osm-save');
  if (save) save.onclick = () => saveSettings(false);

  const saveLimits = document.getElementById('osm-save-limits');
  if (saveLimits) saveLimits.onclick = () => saveSettings(true);

  const test = document.getElementById('osm-test');
  if (test) test.onclick = async () => {
    test.disabled = true;
    try {
      const r = await api.post(`${PREFIX}/test`, readConnectionForm());
      renderTestResult(r);
      toast(r.ok ? 'Подключение проверено' : (r.error || 'Подключение не прошло'),
        r.ok ? 'ok' : 'err', '☁️');
    } catch (e) {
      renderTestResult({ ok: false, error: e.message || String(e), error_code: '' });
      toast(e.message || 'Проверка не выполнена', 'err');
    } finally {
      test.disabled = false;
    }
  };

  const disable = document.getElementById('osm-disable');
  if (disable) disable.onclick = async () => {
    if (!confirm('Отключить мониторинг OpenStack?\n\nОпрос облака прекратится, '
      + 'собранные данные и журнал сохранятся.')) return;
    try {
      await api.post(`${PREFIX}/disable`, {});
      toast('Мониторинг отключён', 'warn', '☁️');
      if (typeof reload === 'function') reload();
    } catch (e) {
      toast(e.message || 'Не удалось отключить', 'err');
    }
  };

  document.querySelectorAll('#osm-panel-metrics [data-period]').forEach((btn) => {
    btn.onclick = () => {
      period = btn.dataset.period;
      document.querySelectorAll('#osm-panel-metrics [data-period]')
        .forEach(b => b.classList.toggle('active', b === btn));
      loadMetrics();
    };
  });

  const refresh = document.getElementById('osm-refresh');
  if (refresh) refresh.onclick = () => loadMetrics();

  document.querySelectorAll('#osm-panel-log [data-level]').forEach((btn) => {
    btn.onclick = () => {
      logLevel = btn.dataset.level;
      document.querySelectorAll('#osm-panel-log [data-level]')
        .forEach(b => b.classList.toggle('active', b === btn));
      loadLogs();
    };
  });

  const logRefresh = document.getElementById('osm-log-refresh');
  if (logRefresh) logRefresh.onclick = () => loadLogs();

  // Автообновление раз в 30 секунд — только на открытой вкладке «Показатели».
  // Тик сам останавливается, когда карточка исчезла из DOM: тот же приём,
  // что в блоке истории версий, поэтому ушедший со страницы администратор
  // не оставляет за собой опрос сервера.
  osmStop();
  timer = setInterval(() => {
    if (!document.getElementById('st-openstack')) { osmStop(); return; }
    loadMetrics();
  }, REFRESH_MS);
}

export function osmStop() {
  if (timer) { clearInterval(timer); timer = null; }
  destroyCharts();
}
