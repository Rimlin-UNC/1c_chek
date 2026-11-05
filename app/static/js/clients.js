/* v1.70.100 (T-50, ADR-30): рабочее место аутсорсера — экран «Клиенты». Все компании бухгалтера в одном
   окне: срок сдачи, недостающие документы и просрочки, непроверенные чеки, ошибки УПД, платежи без
   документов, готовность к выгрузке и последняя загрузка из банка. Компании — по убыванию «внимания»;
   любая цифра открывает нужный экран уже в этой компании (переключение клиента — без перевхода).
   Клиентов бухгалтеру назначает администратор («Пользователи» → «🏢 Клиенты»). Имена верхнего уровня —
   с приставкой clients (общая область модулей в сборке). */
import { api, setClient } from './api.js';
import { esc, fmtSum, openModal, toast } from './ui.js';

export const CLIENTS_LEVELS = {
  bad: ['chip failed', 'Нужны действия'], warn: ['chip unknown', 'Есть вопросы'], ok: ['chip verified', 'Всё в порядке'],
};
export const CLIENTS_CONN = {
  none: 'банк по API не подключён', new: 'банк по API не подключён', awaiting: 'ждём подтверждения в банке',
  active: 'банк подключён', reauth: 'банк: нужно подключить заново', error: 'банк: ошибка подключения',
};
const CLIENTS_MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября',
  'октября', 'ноября', 'декабря'];
const CLIENTS_FILTERS = [['all', 'Все'], ['bad', 'Нужны действия'], ['warn', 'Есть вопросы'], ['ok', 'Всё в порядке']];

/** «день / дня / дней» для числа n. */
export function clientsDays(n) {
  const a = Math.abs(Number(n) || 0) % 100, b = a % 10;
  if (a > 10 && a < 20) return 'дней';
  if (b === 1) return 'день';
  if (b >= 2 && b <= 4) return 'дня';
  return 'дней';
}

/** Срок сдачи: когда и сколько осталось. days_left: полные дни; 0 — меньше суток; минус — просрочено. */
export function clientsDeadline(d) {
  if (!d || !d.deadline) return { text: '', when: '', cls: '' };
  const m = /^(\d{4})-(\d{2})-(\d{2})T(\d{2}):(\d{2})/.exec(d.deadline);
  const when = m ? `${Number(m[3])} ${CLIENTS_MONTHS[Number(m[2]) - 1]}, ${m[4]}:${m[5]}` : '';
  const left = Number(d.days_left || 0);
  const open = Number(d.open || 0);
  if (left < 0) {
    if (!open) return { text: 'срок прошёл — всё сдано', when, cls: 'cl-done' };
    return { text: `просрочено на ${-left} ${clientsDays(left)}`, when, cls: 'cl-late' };
  }
  if (!open) return { text: 'открытых запросов нет', when, cls: 'cl-done' };   // торопить клиента не с чем
  if (left === 0) return { text: 'осталось меньше суток', when, cls: 'cl-soon' };
  return { text: `через ${left} ${clientsDays(left)}`, when, cls: left <= 2 ? 'cl-soon' : '' };
}

/** Когда банк последний раз отдал выписку: «5 ноября, 10:15» по времени устройства. */
export function clientsWhen(iso) {
  if (!iso) return '';
  const t = new Date(iso);
  if (Number.isNaN(t.getTime())) return '';
  return t.toLocaleString('ru-RU', { day: 'numeric', month: 'long', hour: '2-digit', minute: '2-digit' });
}

function clientsLine(value, label, tone = '') {
  const n = Number(value || 0);
  return n ? `<span class="cl-line${tone ? ' ' + tone : ''}"><b>${n}</b> ${esc(label)}</span>` : '';
}

/** Раздел карточки — кнопка: открыть экран в этой компании. */
function clientsTile(row, open, title, main, lines, tone = '') {
  const extra = lines.filter(Boolean).join('');
  return `<button type="button" class="cl-tile${tone ? ' ' + tone : ''}" data-company="${esc(row.id)}" data-open="${open}"
      title="${esc(title)} — открыть в компании «${esc(row.name)}»">
      <span class="cl-tile-title">${esc(title)}</span>
      <span class="cl-tile-main">${main}</span>
      ${extra ? `<span class="cl-tile-lines">${extra}</span>` : ''}
    </button>`;
}

/** Карточка компании: статус, срок сдачи и четыре раздела с цифрами. */
export function clientsCardHtml(row) {
  const [cls, label] = CLIENTS_LEVELS[row.level] || CLIENTS_LEVELS.ok;
  const r = row.receipts || {}, p = row.primary || {}, d = row.docreq || {}, b = row.bank || {};
  const dl = clientsDeadline(d);
  const conn = CLIENTS_CONN[b.status] || CLIENTS_CONN.none;
  const synced = b.last_sync_at ? `, загрузка ${esc(clientsWhen(b.last_sync_at))}` : '';
  const docreqMain = d.open ? `<b>${d.open}</b> нужно` : 'всё есть';
  const receiptsMain = r.count ? `<b>${r.count}</b> · ${fmtSum(r.sum)}` : 'нет чеков';
  const primaryMain = p.errors ? `<b>${p.errors}</b> с ошибками` : 'без ошибок';
  const bankMain = b.open ? `<b>${b.open}</b> без документов` : 'всё с документами';
  return `<article class="glass cl-card cl-${esc(row.level)}" data-company="${esc(row.id)}">
    <header class="cl-head">
      <div class="cl-title"><b>${esc(row.name)}</b>${row.home ? ' <span class="chip">своя</span>' : ''}
        <span class="form-hint">ИНН ${esc(row.inn || '—')}</span></div>
      <div class="cl-head-actions">
        <span class="${cls}">${label}</span>
        <button type="button" class="btn btn-sm" data-pack="${esc(row.id)}"
          title="JSON, CSV и УПД за месяц — для «Моего дела» и других учётных систем">📦 Пакет за месяц</button>
      </div>
    </header>
    <div class="cl-deadline ${dl.cls}">Срок сдачи: ${esc(dl.when || '—')}${dl.text ? ` — <b>${esc(dl.text)}</b>` : ''}</div>
    <div class="cl-tiles">
      ${clientsTile(row, 'docreq', 'Что нужно от клиента', docreqMain,
        [clientsLine(d.overdue, 'просрочено', 'cl-bad'), clientsLine(d.escalated, 'у руководителя', 'cl-warn')],
        d.overdue ? 'cl-bad' : (d.open ? 'cl-warn' : ''))}
      ${clientsTile(row, 'receipts', 'Чеки', receiptsMain,
        [clientsLine(r.fns_bad, 'ошибки ФНС', 'cl-bad'), clientsLine(r.attention, 'ждут проверки', 'cl-warn'),
          clientsLine(r.policy, 'нарушения политики', 'cl-warn'), clientsLine(r.to_export, 'к выгрузке')],
        r.fns_bad ? 'cl-bad' : (r.attention || r.policy ? 'cl-warn' : ''))}
      ${clientsTile(row, 'primary', 'Первичка (УПД)', primaryMain, [clientsLine(p.to_export, 'к выгрузке в 1С')],
        p.errors ? 'cl-bad' : '')}
      ${clientsTile(row, 'bank', 'Банк', bankMain,
        [`<span class="cl-line${b.status === 'reauth' || b.status === 'error' ? ' cl-bad' : ''}">${esc(conn)}${synced}</span>`],
        b.status === 'reauth' || b.status === 'error' ? 'cl-bad' : (b.open ? 'cl-warn' : ''))}
    </div>
  </article>`;
}

/** Итоги по всем клиентам — плитки над списком. */
export function clientsTotalsHtml(t) {
  const x = t || {};
  const kpi = (label, value, sub, tone = '') => `<div class="glass kpi${tone ? ' ' + tone : ''}">
      <div class="kpi-label">${esc(label)}</div><div class="kpi-value">${value}</div>
      <div class="kpi-sub">${sub}</div></div>`;
  return `<div class="grid kpi-grid cl-kpis">
    ${kpi('Компаний', Number(x.companies || 0),
      `нужны действия: ${x.bad || 0} · вопросы: ${x.warn || 0} · в порядке: ${x.ok || 0}`, x.bad ? 'cl-bad' : '')}
    ${kpi('Просрочено запросов', Number(x.overdue || 0), `открыто: ${x.open || 0} · у руководителя: ${x.escalated || 0}`,
      x.overdue ? 'cl-bad' : '')}
    ${kpi('Ошибки ФНС и УПД', Number(x.fns_bad || 0) + Number(x.upd_errors || 0),
      `чеки: ${x.fns_bad || 0} · УПД: ${x.upd_errors || 0}`, (x.fns_bad || x.upd_errors) ? 'cl-bad' : '')}
    ${kpi('Платежи без документов', Number(x.bank_open || 0), 'по выпискам за месяц', x.bank_open ? 'cl-warn' : '')}
    ${kpi('К выгрузке', Number(x.to_export || 0) + Number(x.upd_to_export || 0),
      `чеки: ${x.to_export || 0} · УПД: ${x.upd_to_export || 0}`)}
  </div>`;
}

/** Список карточек с фильтром по статусу и поиском по названию или ИНН. */
export function clientsListHtml(companies, filter = 'all', query = '') {
  const q = String(query || '').trim().toLowerCase();
  const rows = (companies || []).filter((c) => (filter === 'all' || c.level === filter)
    && (!q || String(c.name || '').toLowerCase().includes(q) || String(c.inn || '').includes(q)));
  if (!rows.length) {
    return `<div class="glass card cl-empty">${(companies || []).length
      ? 'Под фильтр не попала ни одна компания.'
      : 'Компаний пока нет. Администратор назначает бухгалтеру клиентов: «Пользователи» → бухгалтер → «🏢 Клиенты».'}</div>`;
  }
  return rows.map(clientsCardHtml).join('');
}

/** Экран «Клиенты». opts.open(companyId, view) — переключить компанию и открыть экран (из app.js). */
export async function viewClients(container, opts = {}) {
  const alive = opts.alive || (() => true);
  let period = opts.period || '';
  let filter = 'all';
  let query = '';
  let data = null;
  container.innerHTML = `<div class="glass card"><div class="skeleton"></div><div class="skeleton"></div></div>`;

  async function load() {
    try {
      data = await api.get('/api/v1/clients' + (period ? `?period=${encodeURIComponent(period)}` : ''));
    } catch (e) {
      if (alive()) container.innerHTML = `<div class="glass card">Не удалось загрузить клиентов: ${esc(e.message)}</div>`;
      return;
    }
    if (!alive()) return;
    period = data.period;
    render();
  }

  function paintList() {
    const list = container.querySelector('#cl-list');
    if (list) list.innerHTML = clientsListHtml(data.companies, filter, query);
  }

  function render() {
    const periods = data.periods || [];
    container.innerHTML = `
      <div class="glass card cl-top">
        <div class="cl-top-row">
          <div>
            <div class="card-title">🏢 Все клиенты в одном окне</div>
            <p class="form-hint">Отчётный месяц — ${esc(data.period_title || data.period)}. Сначала — компании, где
              нужны действия; любая цифра открывает экран уже в этой компании.</p>
          </div>
          <label class="field cl-period"><span>Месяц</span>
            <select id="cl-period">${periods.map((p) => `<option value="${esc(p.period)}"${p.period === data.period
              ? ' selected' : ''}>${esc(p.title)}</option>`).join('')}</select></label>
        </div>
        ${clientsTotalsHtml(data.totals)}
        <div class="cl-tools">
          <div class="segmented cl-filter" role="group" aria-label="Статус компании">${CLIENTS_FILTERS.map(([key, title]) =>
            `<button type="button" data-filter="${key}"${key === filter ? ' class="active"' : ''}>${esc(title)}</button>`).join('')}</div>
          <input type="search" class="cl-search" id="cl-search" placeholder="Поиск по названию или ИНН" value="${esc(query)}">
        </div>
      </div>
      <div class="cl-list" id="cl-list"></div>`;
    paintList();
    container.querySelector('#cl-period').onchange = (e) => { period = e.target.value; load(); };
    container.querySelectorAll('.cl-filter button').forEach((btn) => {
      btn.onclick = () => {
        filter = btn.dataset.filter;
        container.querySelectorAll('.cl-filter button').forEach((b) => b.classList.toggle('active', b === btn));
        paintList();
      };
    });
    container.querySelector('#cl-search').oninput = (e) => { query = e.target.value; paintList(); };
    container.querySelector('#cl-list').onclick = (e) => {
      const pack = e.target.closest('[data-pack]');
      if (pack) {
        clientsDownloadPackage(period, pack.dataset.pack, pack);
        return;
      }
      const tile = e.target.closest('[data-open]');
      if (!tile || typeof opts.open !== 'function') return;
      opts.open(tile.dataset.company, tile.dataset.open);
    };
  }

  await load();
}

// ------------------------------------------------------------------ пакет для других учётных систем (T-68)
const CLIENTS_MONTH_TITLES = ['Январь', 'Февраль', 'Март', 'Апрель', 'Май', 'Июнь', 'Июль', 'Август', 'Сентябрь',
  'Октябрь', 'Ноябрь', 'Декабрь'];

/** Месяцы для выбора: отчётный (прошлый) — первым, затем текущий и более ранние. */
export function clientsPeriods(now = new Date(), count = 6) {
  const key = (y, m) => `${y}-${String(m + 1).padStart(2, '0')}`;
  const shift = (k) => {
    const idx = now.getFullYear() * 12 + now.getMonth() + k;
    const y = Math.floor(idx / 12), m = idx % 12;
    return { period: key(y, m), title: `${CLIENTS_MONTH_TITLES[m]} ${y}` };
  };
  const out = [shift(-1), shift(0)];
  for (let k = 2; k <= count; k++) out.push(shift(-k));
  return out;
}

function clientsSave(blob, filename) {
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(url), 1500);
}

/** Скачать пакет за месяц: JSON, CSV, файлы УПД 5.03 и описание формата одним архивом. */
export async function clientsDownloadPackage(period, companyId, btn) {
  const qs = new URLSearchParams();
  if (period) qs.set('period', period);
  if (companyId) qs.set('company_id', companyId);
  if (btn) btn.disabled = true;
  try {
    const { blob, filename } = await api.download('/api/v1/export/package?' + qs.toString());
    clientsSave(blob, filename);
    toast(`${filename} — чеки, первичка, банк и возмещения в JSON и CSV, файлы УПД 5.03`, 'ok', 'Пакет готов');
  } catch (e) {
    toast(e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** Карточка «Пакет для других учётных систем» на экране «Выгрузка в 1С». */
export function clientsPackageHtml(opts = {}) {
  const periods = opts.periods || clientsPeriods();
  const needCompany = opts.isAdmin && !opts.companyId;
  return `<div class="glass card cl-pack-card" id="pkg-card">
    <div class="card-title">📦 Пакет для других учётных систем</div>
    <p class="form-hint">Всё за отчётный месяц одним архивом: чеки с позициями, первичка, банковские операции без
      документов и возмещения сотрудникам — в JSON и CSV (Excel открывает сразу), файлы УПД 5.03 по схеме ФНС для
      «Моего дела» и других систем, описание формата FORMAT.md и контрольные суммы. Чеки при этом не помечаются
      выгруженными.</p>
    <div class="dreq-tools">
      <label class="field"><span>Месяц</span><select id="pkg-period">${periods.map((p) =>
        `<option value="${esc(p.period)}">${esc(p.title)}</option>`).join('')}</select></label>
      <button type="button" class="btn btn-primary" id="pkg-download"${needCompany ? ' disabled' : ''}>⬇ Скачать пакет</button>
    </div>
    ${needCompany ? '<p class="form-hint">Выберите компанию в шапке — пакет собирается по одной компании.</p>' : ''}
  </div>`;
}

export function clientsPackageBind(root, opts = {}) {
  const btn = root.querySelector('#pkg-download');
  if (!btn) return;
  btn.onclick = () => clientsDownloadPackage(root.querySelector('#pkg-period').value, opts.companyId || '', btn);
}

/** Переключатель клиента в шапке — бухгалтеру, который ведёт больше одной компании. */
export function clientsSwitcher(sel, me, onSwitch) {
  if (!sel) return;
  const list = me && me.role === 'accountant' && Array.isArray(me.clients) ? me.clients : [];
  if (list.length < 2) {
    sel.classList.add('hidden');
    sel.innerHTML = '';
    return;
  }
  sel.innerHTML = list.map((c) => `<option value="${esc(c.id)}"${c.id === me.company_id ? ' selected' : ''}>`
    + `${esc(c.name)}${c.home ? ' · своя' : ''}</option>`).join('');
  sel.classList.remove('hidden');
  sel.onchange = () => {
    const pick = list.find((c) => c.id === sel.value);
    setClient(pick && !pick.home ? pick.id : '');
    if (typeof onSwitch === 'function') onSwitch(sel.value);
  };
}

/** Администратор: какие компании ведёт бухгалтер. user — { id, name }, companies — все компании. */
export async function clientsAccessDialog(user, companies) {
  let access;
  try {
    access = await api.get(`/api/v1/clients/access/${encodeURIComponent(user.id)}`);
  } catch (e) {
    toast(e.message, 'err');
    return;
  }
  const chosen = new Set(access.company_ids || []);
  const list = (companies || []).filter((c) => c.is_active !== false && c.id !== access.home_company_id);
  const label = (c) => c.display_name || c.short_name || c.name;
  const { slot, close } = openModal(`
    <h3>🏢 Клиенты бухгалтера ${esc(user.name)}</h3>
    <p class="form-hint">Своя компания бухгалтера доступна всегда. Отметьте компании-клиенты, которые он ведёт:
      он увидит их в «Клиентах» и будет переключаться между ними без перевхода. Данные других компаний ему не видны.</p>
    <input type="search" class="cl-search" id="cla-search" placeholder="Поиск по названию или ИНН">
    <div class="cl-access-list" id="cla-list">${list.length ? list.map((c) => `
      <label class="dreq-check" data-q="${esc((label(c) + ' ' + (c.inn || '')).toLowerCase())}">
        <input type="checkbox" value="${esc(c.id)}"${chosen.has(c.id) ? ' checked' : ''}>
        <span>${esc(label(c))} <span class="form-hint">ИНН ${esc(c.inn || '—')}</span></span></label>`).join('')
    : '<p class="form-hint">Других активных компаний нет.</p>'}</div>
    <p class="form-hint" id="cla-count"></p>
    <div class="modal-actions">
      <button type="button" class="btn" id="cla-cancel">Отмена</button>
      <button type="button" class="btn btn-primary" id="cla-save">Сохранить</button>
    </div>`);
  const boxes = () => Array.from(slot.querySelectorAll('#cla-list input[type="checkbox"]'));
  const count = () => { slot.querySelector('#cla-count').textContent = `Выбрано клиентов: ${boxes().filter((b) => b.checked).length}`; };
  count();
  slot.querySelector('#cla-list').onchange = count;
  slot.querySelector('#cla-search').oninput = (e) => {
    const q = e.target.value.trim().toLowerCase();
    slot.querySelectorAll('#cla-list [data-q]').forEach((row) => row.classList.toggle('hidden', !!q && !row.dataset.q.includes(q)));
  };
  slot.querySelector('#cla-cancel').onclick = close;
  slot.querySelector('#cla-save').onclick = async () => {
    const ids = boxes().filter((b) => b.checked).map((b) => b.value);
    try {
      const res = await api.put(`/api/v1/clients/access/${encodeURIComponent(user.id)}`, { company_ids: ids });
      toast(`Клиентов у бухгалтера: ${res.company_ids.length} (добавлено ${res.added}, снято ${res.removed})`, 'ok');
      close();
    } catch (e) {
      toast(e.message, 'err');
    }
  };
}
