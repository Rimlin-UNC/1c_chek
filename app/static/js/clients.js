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
      <label class="cl-pick" title="Отметить для массового действия">
        <input type="checkbox" data-pick="${esc(row.id)}"${row.picked ? ' checked' : ''} aria-label="Отметить «${esc(row.name)}»">
      </label>
      <div class="cl-title"><b>${esc(row.name)}</b>${row.home ? ' <span class="chip">своя</span>' : ''}
        <span class="form-hint">ИНН ${esc(row.inn || '—')}</span></div>
      <div class="cl-head-actions">
        <span class="${cls}">${label}</span>
        <button type="button" class="btn btn-sm" data-pack="${esc(row.id)}"
          title="JSON, CSV и УПД за месяц — для других учётных систем">📦 Пакет за месяц</button>
      </div>
    </header>
    ${clientsContractSummary(row)}
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
      ${clientsTile(row, 'bank', 'Банк', bankMain, clientsBankLines(b, conn, synced),
        b.status === 'reauth' || b.status === 'error' ? 'cl-bad' : (b.open ? 'cl-warn' : ''))}
      ${clientsApprovalsTile(row)}
    </div>
  </article>`;
}

/** T-88: договор показывается в сводке; деньги форматируются как строка, без округления float. */
export function clientsContractSummary(row) {
  const c = row.contract;
  const warning = c && c.reminder;
  let msg = '';
  if (warning) {
    const days = Number(warning.days);
    msg = days < 0 ? `Дата окончания по данным карточки прошла ${-days} ${clientsDays(days)} назад — проверьте договор`
      : `Дата окончания по данным карточки через ${days} ${clientsDays(days)} — проверьте договор`;
  }
  const detail = c ? `Договор № ${esc(c.number)} · ${esc(c.fee)} ${esc(c.currency)} / ${esc(CLIENTS_FREQUENCIES[c.frequency] || c.frequency)}`
    : 'Договор не заполнен';
  return `<div class="cl-contract"><span>${detail}</span>${msg ? ` <strong>${esc(msg)}</strong>` : ''}
    <button type="button" class="btn btn-sm" data-contract="${esc(row.id)}">Договор и тариф</button></div>`;
}

const CLIENTS_FREQUENCIES = { month: 'месяц', quarter: 'квартал', year: 'год', once: 'разово' };

export function clientsContractFormHtml(contract) {
  const c = contract || {};
  const value = (key) => esc(c[key] || '');
  return `<form id="contract-form">
    <div class="modal-title">Договор и тариф клиента</div>
    <p class="form-hint">Обязательные поля отмечены звёздочкой. Напоминание видно только при входе на экран
      «Клиенты»; клиенту и во внешние каналы оно не отправляется. Проверьте пролонгацию и дополнительные соглашения:
      дата в карточке сама по себе не подтверждает прекращение договора.</p>
    <label class="field"><span>Номер договора *</span><input id="contract-number" maxlength="200" required value="${value('number')}"></label>
    <label class="field"><span>Дата договора *</span><input id="contract-date" type="date" required value="${value('date')}"></label>
    <label class="field"><span>Начало обслуживания *</span><input id="contract-since" type="date" required value="${value('service_since')}"></label>
    <label class="field"><span>Окончание обслуживания</span><input id="contract-until" type="date" value="${value('service_until')}"></label>
    <label class="field"><span>Стоимость *</span><input id="contract-fee" type="text" inputmode="decimal" required value="${value('fee')}" placeholder="Например 12500.50"></label>
    <label class="field"><span>Периодичность оплаты *</span><select id="contract-frequency" required>
      <option value="">— выберите —</option>${Object.entries(CLIENTS_FREQUENCIES).map(([key, label]) =>
    `<option value="${key}"${c.frequency === key ? ' selected' : ''}>${label}</option>`).join('')}</select></label>
    <label class="field"><span>Валюта *</span><input id="contract-currency" maxlength="3" pattern="[A-Za-z]{3}" required value="${esc(c.currency || 'RUB')}"></label>
    <label class="field"><span>Ответственный менеджер</span><input id="contract-manager" maxlength="200" value="${value('manager')}"></label>
    <p id="contract-error" class="form-error" role="alert"></p>
    <div class="modal-actions"><button type="button" class="btn" id="contract-cancel">Отмена</button>
      <button type="submit" class="btn btn-primary">Сохранить</button></div>
  </form>`;
}

export function clientsContractDialog(row, onSaved) {
  const { slot, close } = openModal(clientsContractFormHtml(row.contract));
  const form = slot.querySelector('#contract-form');
  slot.querySelector('#contract-cancel').onclick = close;
  form.onsubmit = async (event) => {
    event.preventDefault();
    const field = (id) => slot.querySelector('#contract-' + id).value.trim();
    const body = { number: field('number'), date: field('date'), service_since: field('since'),
      service_until: field('until') || null, fee: field('fee').replace(',', '.'), frequency: field('frequency'),
      currency: field('currency').toUpperCase(), manager: field('manager') };
    const submit = form.querySelector('[type="submit"]');
    submit.disabled = true;
    try {
      await api.put(`/api/v1/clients/${encodeURIComponent(row.id)}/contract`, body);
      close();
      toast('Договор и тариф сохранены', 'ok');
      if (typeof onSaved === 'function') onSaved();
    } catch (error) {
      slot.querySelector('#contract-error').textContent = error.message || 'Не удалось сохранить договор';
      submit.disabled = false;
    }
  };
}

/** Состояние банка одной фразой — как на экране «Банк». */
export const CLIENTS_BANK_STATE = {
  active: 'работает', awaiting: 'ждём подтверждения', new: 'ждём настройки', reauth: 'нужно подключить заново',
  error: 'ошибка подключения',
};

/** v1.71.4 (T-87): банки в плитке. Один банк — по-прежнему одна строка «состояние, загрузка». Несколько —
    каждый со своим состоянием и своей ошибкой (худший уже первым, порядок задаёт сервер): видно, какой
    банк сломался и что именно случилось, а не только «банк: ошибка подключения». */
export function clientsBankLines(b, conn, synced) {
  const banks = (b && Array.isArray(b.banks)) ? b.banks : [];
  if (banks.length <= 1) {
    return [`<span class="cl-line${b.status === 'reauth' || b.status === 'error' ? ' cl-bad' : ''}">${esc(conn)}${synced}</span>`];
  }
  return banks.map((x) => {
    const bad = x.status === 'reauth' || x.status === 'error';
    const state = CLIENTS_BANK_STATE[x.status] || x.status;
    const when = x.last_sync_at ? `, ${esc(clientsWhen(x.last_sync_at))}` : '';
    const err = bad && x.error ? `<span class="cl-bank-error">${esc(x.error)}</span>` : '';
    return `<span class="cl-line${bad ? ' cl-bad' : ''}" title="${esc(x.title)}: ${esc(state)}">`
      + `<b>${esc(x.title)}</b> — ${esc(state)}${when}${err}</span>`;
  });
}

/** v1.71.3 (T-86): согласования авансовых отчётов — плитка карточки. Согласование выключено в компании —
    плитки нет вовсе: нули на экране не рисуем, иначе бухгалтер ищет несуществующую работу. */
export function clientsApprovalsTile(row) {
  const a = (row && row.approvals) || {};
  if (!a.enabled) return '';
  const main = a.todo ? `<b>${a.todo}</b> ждут меня` : (a.work ? `<b>${a.work}</b> в работе` : 'ждут не меня');
  return clientsTile(row, 'approvals', 'Согласования', main,
    [clientsLine(a.overdue, 'просрочено', 'cl-bad'), clientsLine(a.work, 'в работе'),
      clientsLine(a.month, 'заведено за месяц')],
    a.overdue ? 'cl-bad' : (a.todo ? 'cl-warn' : ''));
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
    ${kpi('Согласования', Number(x.approvals_todo || 0),
      `ждут моего хода · просрочено: ${x.approvals_overdue || 0}`,
      x.approvals_overdue ? 'cl-bad' : (x.approvals_todo ? 'cl-warn' : ''))}
    ${kpi('Платежи без документов', Number(x.bank_open || 0), 'по выпискам за месяц', x.bank_open ? 'cl-warn' : '')}
    ${kpi('К выгрузке', Number(x.to_export || 0) + Number(x.upd_to_export || 0),
      `чеки: ${x.to_export || 0} · УПД: ${x.upd_to_export || 0}`)}
  </div>`;
}

/** v1.72.0 (T-89): панель массовых действий — появляется, когда отмечен хотя бы один клиент. */
export function clientsBulkHtml(count, opts = {}) {
  const n = Number(count) || 0;
  if (!n) return '';
  return `<div class="glass cl-bulk" id="cl-bulk" role="region" aria-label="Массовые действия">
    <b>Отмечено клиентов: <span id="cl-bulk-count">${n}</span></b>
    <div class="cl-bulk-actions">
      <button type="button" class="btn btn-sm" data-bulk="remind"
        title="Запрос документов у всех отмеченных компаний — по тем же каналам, что и обычное напоминание">📣 Напомнить всем</button>
      <button type="button" class="btn btn-sm" data-bulk="pack"
        title="Архив за месяц по каждому отмеченному клиенту — скачивается по одному">📦 Пакет по всем</button>
      ${opts.isAdmin ? `<button type="button" class="btn btn-sm" data-bulk="assign"
        title="Сотрудник переедет в отмеченные компании; окно выбора — следующим шагом">👤 Назначить сотрудника</button>` : ''}
      <button type="button" class="btn btn-sm" data-bulk="archive"
        title="Компании уйдут в архив: данные и история остаются, в сводке их не будет">🗄 В архив</button>
      <button type="button" class="btn btn-sm" data-bulk="restore"
        title="Вернуть отмеченные компании из архива в работу">↩ Из архива</button>
      <button type="button" class="btn btn-sm btn-ghost" data-bulk="clear">Снять отметки</button>
    </div>
  </div>`;
}

/** Список карточек с фильтром по статусу и поиску по названию или ИНН. */
export function clientsListHtml(companies, filter = 'all', query = '') {
  const q = String(query || '').trim().toLowerCase();
  const rows = (companies || []).filter((c) => (filter === 'all' || c.level === filter)
    && (!q || String(c.name || '').toLowerCase().includes(q) || String(c.inn || '').includes(q)));
  if (!rows.length) {
    return `<div class="glass card cl-empty">${(companies || []).length
      ? 'Под фильтр не попала ни одна компания.'
      : 'Клиентов пока нет. Нажмите «＋ Новый клиент» — компания появится в списке сразу. '
        + 'Ещё клиентов назначает администратор: «Пользователи» → бухгалтер → «🏢 Клиенты».'}</div>`;
  }
  return rows.map(clientsCardHtml).join('');
}

/** Печатная сводка: токен остаётся в API-заголовке, а не попадает в адрес окна. */
export async function clientsOpenReport(period) {
  const win = window.open('', '_blank');
  if (!win) {
    toast('Разрешите всплывающее окно для печатного отчёта', 'err');
    return;
  }
  win.opener = null;
  try {
    const html = await api.get(`/api/v1/clients/report?period=${encodeURIComponent(period)}`);
    const url = URL.createObjectURL(new Blob([html], { type: 'text/html;charset=utf-8' }));
    win.addEventListener('load', () => URL.revokeObjectURL(url), { once: true });
    win.location.href = url;
  } catch (error) {
    win.close();
    toast(error.message || 'Не удалось открыть отчёт', 'err');
  }
}

/** Экран «Клиенты». opts.open(companyId, view) — переключить компанию и открыть экран (из app.js). */
export async function viewClients(container, opts = {}) {
  const alive = opts.alive || (() => true);
  let period = opts.period || '';
  let filter = 'all';
  let query = '';
  let data = null;
  const picked = new Set();          // v1.72.0 (T-89): отмеченные клиенты — переживают фильтр и поиск
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
    if (list) {
      // отметка — часть данных карточки: так чекбокс не «слетает» при смене фильтра
      const rows = data.companies.map((c) => ({ ...c, picked: picked.has(c.id) }));
      list.innerHTML = clientsListHtml(rows, filter, query);
    }
    paintBulk();
  }

  function paintBulk() {
    const host = container.querySelector('#cl-bulk-host');
    if (!host) return;
    host.innerHTML = clientsBulkHtml(picked.size, { isAdmin: opts.isAdmin });
    if (!picked.size) return;
    host.querySelectorAll('[data-bulk]').forEach((btn) => {
      btn.onclick = () => clientsBulkRun(btn.dataset.bulk, [...picked], period, {
        isAdmin: opts.isAdmin, btn, onDone: (action) => {
          if (action === 'archive' || action === 'restore') picked.clear();
          load();
        }, onDownload: (companyId) => clientsDownloadPackage(period, companyId, null),
        onAssign: () => clientsBulkAssign([...picked], () => load()),
      });
    });
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
          <div class="cl-head-actions">
            <label class="field cl-period"><span>Месяц</span>
              <select id="cl-period">${periods.map((p) => `<option value="${esc(p.period)}"${p.period === data.period
                ? ' selected' : ''}>${esc(p.title)}</option>`).join('')}</select></label>
            <button type="button" class="btn btn-sm" id="cl-report" title="Все доступные компании; печать или сохранение в PDF">🖨 Сводный отчёт</button>
            ${clientsCreateButtonHtml()}
          </div>
        </div>
        ${clientsTotalsHtml(data.totals)}
        <div class="cl-tools">
          <div class="segmented cl-filter" role="group" aria-label="Статус компании">${CLIENTS_FILTERS.map(([key, title]) =>
            `<button type="button" data-filter="${key}"${key === filter ? ' class="active"' : ''}>${esc(title)}</button>`).join('')}</div>
          <input type="search" class="cl-search" id="cl-search" placeholder="Поиск по названию или ИНН" value="${esc(query)}">
        </div>
      </div>
      <div id="cl-bulk-host"></div>
      <div class="cl-list" id="cl-list"></div>`;
    paintList();
    container.querySelector('#cl-period').onchange = (e) => { period = e.target.value; load(); };
    container.querySelector('#cl-report').onclick = () => clientsOpenReport(period);
    const newBtn = container.querySelector('#cl-new');
    if (newBtn) newBtn.onclick = () => clientsCreateDialog({
      onCreated: (company) => {
        load();
        // v1.71.1: компания сразу в переключателе в шапке — иначе бухгалтер видит
        // «Клиент добавлен», а в шапке его ещё нет (нашёл e2e в CI).
        if (typeof opts.onCreated === 'function') opts.onCreated(company);
      },
    });
    container.querySelectorAll('.cl-filter button').forEach((btn) => {
      btn.onclick = () => {
        filter = btn.dataset.filter;
        container.querySelectorAll('.cl-filter button').forEach((b) => b.classList.toggle('active', b === btn));
        paintList();
      };
    });
    container.querySelector('#cl-search').oninput = (e) => { query = e.target.value; paintList(); };
    container.querySelector('#cl-list').onchange = (e) => {
      const box = e.target.closest('[data-pick]');
      if (!box) return;
      if (box.checked) picked.add(box.dataset.pick); else picked.delete(box.dataset.pick);
      paintBulk();
    };
    container.querySelector('#cl-list').onclick = (e) => {
      const contract = e.target.closest('[data-contract]');
      if (contract) {
        const row = data.companies.find((company) => company.id === contract.dataset.contract);
        if (row) clientsContractDialog(row, () => load());
        return;
      }
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

// ------------------------------------------------------------------ массовые действия (T-89, v1.72.0)
/** Итог пачки одной фразой: «готово 3 из 4», плюс причина первого отказа. */
export function clientsBulkResult(data) {
  const done = Number(data.done || 0), failed = Number(data.failed || 0);
  const first = (data.items || []).find((i) => !i.ok);
  const tail = first ? ` Первая причина: ${first.name ? first.name + ' — ' : ''}${first.error}` : '';
  return `Готово: ${done}${failed ? `, не получилось: ${failed}.` + tail : ' из ' + (done + failed) + '.'}`;
}

/** Выполнить действие пачкой. pack — «Пакет по всем»: скачиваем архив по каждой компании отдельно. */
export async function clientsBulkRun(action, companyIds, period, opts = {}) {
  if (!companyIds || !companyIds.length) return null;
  const btn = opts.btn;
  if (btn) btn.disabled = true;
  try {
    if (action === 'pack') {
      for (const cid of companyIds) {
        if (typeof opts.onDownload === 'function') await opts.onDownload(cid);
      }
      toast(`Архив за месяц: ${companyIds.length} файлов — по одному на компанию`, 'ok', 'Пакет по всем');
      if (typeof opts.onDone === 'function') opts.onDone(action);
      return { done: companyIds.length, failed: 0, items: [] };
    }
    if (action === 'assign') {
      if (typeof opts.onAssign === 'function') return opts.onAssign();
      return null;
    }
    if (action === 'clear') {
      if (typeof opts.onDone === 'function') opts.onDone(action);
      return null;
    }
    const body = { company_ids: companyIds, action, period };
    const data = await api.post('/api/v1/clients/bulk', body);
    const failed = Number(data.failed || 0);
    toast(clientsBulkResult(data), failed ? 'warn' : 'ok', data.title || 'Готово');
    if (typeof opts.onDone === 'function') opts.onDone(action, data);
    return data;
  } catch (e) {
    toast(e.message, 'err', 'Массовое действие');
    return null;
  } finally {
    if (btn) btn.disabled = false;
  }
}

/** «Назначить сотрудника»: окно выбора и отправка пачкой (администратор). */
export async function clientsBulkAssign(companyIds, onDone) {
  let people = [];
  try {
    people = (await api.get('/api/v1/users')).items || [];
  } catch (e) {
    toast(e.message, 'err', 'Сотрудники');
    return null;
  }
  const staff = people.filter((u) => u.is_active !== false && u.role !== 'admin');
  openModal(`<div class="card-title">👤 Назначить сотрудника</div>
    <p class="form-hint">Отмечено клиентов: ${companyIds.length}. Сотрудник переедет в эти компании — '
      + 'в сводке он появится как их сотрудник.</p>
    <label class="field"><span>Сотрудник</span><select id="cb-user">
      ${staff.map((u) => `<option value="${esc(u.id)}">${esc(u.full_name || u.username)} — ${esc(u.role)}</option>`).join('')}
    </select></label>
    <div class="modal-actions"><button type="button" class="btn" id="cb-cancel">Отмена</button>
      <button type="button" class="btn btn-primary" id="cb-go">Назначить</button></div>`);
  const root = document.querySelector('.modal-slot');
  root.querySelector('#cb-cancel').onclick = () => root.closest('.modal-root').classList.add('hidden');
  root.querySelector('#cb-go').onclick = async () => {
    const userId = root.querySelector('#cb-user').value;
    try {
      const data = await api.post('/api/v1/clients/bulk', { company_ids: companyIds, action: 'assign', user_id: userId });
      root.closest('.modal-root').classList.add('hidden');
      toast(clientsBulkResult(data), data.failed ? 'warn' : 'ok', 'Назначено');
      if (typeof onDone === 'function') onDone(data);
    } catch (e) {
      toast(e.message, 'err', 'Назначение');
    }
  };
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
      других учётных систем, описание формата FORMAT.md и контрольные суммы. Чеки при этом не помечаются
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

/** Кнопка «＋ Новый клиент» на экране «Клиенты» (T-84, v1.71.1). */
export function clientsCreateButtonHtml() {
  return `<button type="button" class="btn btn-primary btn-sm" id="cl-new"
    title="Завести компанию-клиента: сразу появится в списке и в переключателе">＋ Новый клиент</button>`;
}

/** Максимум клиентов у бухгалтера — зеркало app/clients/service.py: MAX_CLIENTS. */
export const CLIENTS_MAX = 500;

/** Сообщение об отказе создания — словами, с подсказкой, что делать. */
export function clientsCreateError(status, message) {
  const text = String(message || '');
  if (status === 409 && /Похожая компания/.test(text)) {
    return `${text}. Откройте её из списка — дубль заводить не нужно.`;
  }
  if (status === 409 && /предел/.test(text)) return text;
  if (status === 403) return 'Заводить клиентов может бухгалтер и администратор.';
  return text || 'Не удалось создать клиента';
}

/** Окно «Новый клиент». opts.onCreated(company) — после успеха (перерисовать список). */
export function clientsCreateDialog(opts = {}) {
  const { slot, close } = openModal(`
    <h3>＋ Новый клиент</h3>
    <p class="form-hint">Компания сразу появится в списке и в переключателе клиента в шапке —
      перевходить не нужно. ИНН можно оставить пустым и заполнить позже.</p>
    <div class="form-grid">
      <label class="field full"><span>ИНН (10 цифр ООО / 12 цифр ИП)</span>
        <div style="display:flex;gap:8px">
          <input id="clc-inn" inputmode="numeric" maxlength="12" placeholder="7707083893" style="flex:1;min-width:0">
          <button type="button" class="btn btn-sm" id="clc-checko" title="Заполнить название из ЕГРЮЛ/ЕГРИП (Checko)">🔍 ЕГРЮЛ</button>
        </div></label>
      <label class="field full"><span>Название (ООО «…», ИП …)</span>
        <input id="clc-name" placeholder="ООО «Ромашка»"></label>
      <label class="field full"><span>Памятка (договор, контакт — необязательно)</span>
        <input id="clc-note" placeholder="договор № 12 от 01.10.2026"></label>
      <div class="full hidden" id="clc-similar"></div>
    </div>
    <div class="modal-actions">
      <button type="button" class="btn" id="clc-cancel">Отмена</button>
      <button type="button" class="btn btn-primary" id="clc-save">Создать клиента</button>
    </div>`);
  const innInput = slot.querySelector('#clc-inn');
  const nameInput = slot.querySelector('#clc-name');
  const noteInput = slot.querySelector('#clc-note');
  const simBox = slot.querySelector('#clc-similar');
  const saveBtn = slot.querySelector('#clc-save');
  let timer = null;

  const digits = () => innInput.value.replace(/\D/g, '');

  async function checkSimilar() {
    const name = nameInput.value.trim();
    if (name.length < 3) { simBox.classList.add('hidden'); return; }
    try {
      const sims = await api.get('/api/v1/companies/similar?name=' + encodeURIComponent(name));
      if (!sims.length) { simBox.classList.add('hidden'); return; }
      simBox.innerHTML = `<div class="info-callout" style="margin:0">⚠ Похожая компания уже есть:
        <b>${esc(sims[0].name)}</b>${sims[0].inn ? ' (ИНН ' + esc(sims[0].inn) + ')' : ''}.
        Если это она — не заводите дубль, выберите её в списке.</div>`;
      simBox.classList.remove('hidden');
    } catch { simBox.classList.add('hidden'); }
  }

  nameInput.addEventListener('input', () => {
    clearTimeout(timer);
    timer = setTimeout(checkSimilar, 500);
  });

  slot.querySelector('#clc-checko').onclick = async () => {
    const inn = digits();
    if (inn.length !== 10 && inn.length !== 12) return toast('ИНН: 10 цифр (ООО) или 12 (ИП)', 'warn');
    const btn = slot.querySelector('#clc-checko');
    btn.disabled = true; btn.textContent = '…ищу';
    try {
      const card = await api.post('/api/v1/companies/lookup-checko', { inn });
      if (!nameInput.value.trim()) nameInput.value = card.name_full || '';
      toast(`Найдено: ${card.name_full}${card.status ? ' · ' + card.status : ''}`, 'ok', '🗂 ЕГРЮЛ');
    } catch (e) {
      toast(e.message, 'err');
    }
    btn.disabled = false; btn.textContent = '🔍 ЕГРЮЛ';
  };

  slot.querySelector('#clc-cancel').onclick = close;
  saveBtn.onclick = async () => {
    const name = nameInput.value.trim();
    if (name.length < 2) return toast('Укажите название компании', 'err');
    saveBtn.disabled = true;
    try {
      const company = await api.post('/api/v1/companies', {
        name, inn: digits(), note: noteInput.value.trim(),
      });
      toast(`Клиент «${company.name}» добавлен — он уже в списке и в шапке`, 'ok', '🏢');
      close();
      if (typeof opts.onCreated === 'function') opts.onCreated(company);
    } catch (e) {
      toast(clientsCreateError(e.status, e.message), 'err', 6000);
    }
    saveBtn.disabled = false;
  };
  return { slot, close };
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
