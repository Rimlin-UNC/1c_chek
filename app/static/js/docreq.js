/* v1.70.97 (T-58): экран «Что нужно от клиента» — недостающие документы компании за месяц, срок сдачи
   и «час икс», напоминания клиенту (Telegram, MAX, почта) и журнал — письменный след. Программа сама
   находит недостающее: регулярные поставщики без документа за месяц, чеки без подтверждения ФНС,
   УПД с ошибками; пришедший документ закрывает запрос сам. Перед «Напомнить сейчас» видно, кому
   и что уйдёт. Все имена верхнего уровня — с приставкой dreq (общая область модулей в сборке). */
import { api } from './api.js';
import { esc, fmtSum, openModal, toast } from './ui.js';

export const DREQ_STATUSES = {
  open: ['chip unknown', 'Ждём'], received: ['chip verified', 'Получено'], cancelled: ['chip failed', 'Отменено'],
};
export const DREQ_KINDS = {
  expected: '📅 ожидается по истории', receipt: '🧾 чек не подтверждён ФНС',
  upd_fix: '🛠 нужен исправленный документ', payment: '🏦 платёж без документов',   // v1.70.98: T-45
  manual: '✍️ запрос бухгалтера',
};
export const DREQ_EVENTS = {
  created: 'Запрос создан', reminder: 'Напоминание', received: 'Получено', cancelled: 'Отменено', reopened: 'Снова ждём',
};
export const DREQ_CHANNELS = { telegram: 'Telegram', max: 'MAX', email: 'почта' };
const DREQ_ROLES = { manager: 'Руководителю компании', user: 'Сотрудникам компании' };

export function dreqStatusChip(status) {
  const [cls, label] = DREQ_STATUSES[status] || ['chip unknown', status || '—'];
  return `<span class="${cls}">${esc(label)}</span>`;
}

/** Время из API: со смещением — как есть, без него — это UTC (так сервер хранит журнал). */
export function dreqTime(iso) {
  if (!iso) return '—';
  const d = new Date(/(?:[zZ]|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? '—'
    : d.toLocaleString('ru-RU', { day: '2-digit', month: '2-digit', year: 'numeric', hour: '2-digit', minute: '2-digit' });
}

export function dreqRowsHtml(items) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    return '<tr><td colspan="5">Запросов пока нет. Нажмите «Найти недостающее» — программа проверит документы и чеки за месяц.</td></tr>';
  }
  return list.map((r) => {
    const act = (status, label) => `<button type="button" class="btn btn-sm dreq-act" data-id="${esc(r.id)}" data-status="${status}">${label}</button>`;
    const actions = r.status === 'open' ? `${act('received', '✓ Получено')} ${act('cancelled', 'Отменить')}` : act('open', '↺ Снова ждём');
    const about = [DREQ_KINDS[r.kind] || r.kind_title || r.kind, r.counterparty_inn && `ИНН ${r.counterparty_inn}`].filter(Boolean).join(' · ');
    return `<tr data-id="${esc(r.id)}"><td>${esc(r.title)}<div class="form-hint">${esc(about)}</div></td>
      <td>${esc(r.assignee || '—')}</td>
      <td class="num dreq-sum">${r.amount === null || r.amount === undefined ? '—' : fmtSum(r.amount)}</td>
      <td>${dreqStatusChip(r.status)}${r.close_note ? `<div class="form-hint">${esc(r.close_note)}</div>` : ''}</td>
      <td><div class="dreq-actions">${actions}</div></td></tr>`;
  }).join('');
}

export function dreqJournalHtml(items) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) return '<p class="form-hint">Журнал пуст: здесь появятся запросы, напоминания и отметки о получении.</p>';
  const rows = list.map((e) => {
    const what = `${DREQ_EVENTS[e.kind] || e.kind}${e.slot ? ` (${e.slot === 'manual' ? 'вручную' : e.slot})` : ''}`;
    const who = e.kind === 'reminder' ? (e.recipient || '—') : (e.actor || '—');
    return `<tr${e.ok ? '' : ' class="dreq-failed"'}><td>${esc(dreqTime(e.at))}</td><td>${esc(what)}</td>
      <td>${esc(DREQ_CHANNELS[e.channel] || '—')}</td><td>${esc(who)}</td><td>${e.ok ? '✓' : '✗'}</td>
      <td><details><summary>текст</summary><pre class="dreq-text">${esc(e.text || '')}</pre></details></td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="data"><thead><tr><th>Когда</th><th>Что</th><th>Канал</th>
    <th>Кому или кто</th><th></th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function dreqScheduleHtml(schedule, enabled) {
  if (!enabled) {
    return '<span class="form-hint">Автонапоминания выключены — их включает бухгалтер в «Срок и напоминания». Список и журнал работают и без них.</span>';
  }
  const list = Array.isArray(schedule) ? schedule : [];
  return `<span class="form-hint">Напоминания: ${list.map((s) => `${s.sent ? '✓ ' : ''}${esc(s.human)}`).join(' · ') || 'не запланированы'}</span>`;
}

export function dreqDisciplineText(d) {
  if (!d || d.share === null || d.share === undefined) return '';
  return `В срок за три месяца: ${d.share} % (${d.on_time} из ${d.received})`;
}

/** «3, 1, 0» → [3, 1, 0]; ошибка — null. */
export function dreqParseDays(text) {
  const parts = String(text || '').split(/[\s,;]+/).filter(Boolean);
  if (parts.length > 5 || parts.some((p) => !/^\d{1,2}$/.test(p) || Number(p) > 10)) return null;
  return [...new Set(parts.map(Number))].sort((a, b) => b - a);
}

export function dreqSettingsHtml(s, channels) {
  const ch = channels || {};
  const box = (id, on, label, hint = '') => `<label class="dreq-check"><input type="checkbox" id="${id}" ${on ? 'checked' : ''}>
    <span>${esc(label)}${hint ? `<span class="form-hint"> — ${esc(hint)}</span>` : ''}</span></label>`;
  return `<h3 class="modal-title">⚙ Срок сдачи и напоминания</h3>
    ${box('dreq-s-enabled', s.enabled, 'Напоминать клиенту автоматически')}
    ${box('dreq-s-auto', s.auto_detect, 'Перед напоминанием искать недостающее')}
    <div class="dreq-grid">
      <label class="field"><span>Срок сдачи — число следующего месяца</span><input id="dreq-s-day" type="number" min="1" max="28" value="${esc(s.deadline_day)}"></label>
      <label class="field"><span>«Час икс» (МСК)</span><input id="dreq-s-hour" type="time" value="${esc(s.x_hour)}"></label>
      <label class="field"><span>Напоминать за, дней до срока</span><input id="dreq-s-days" value="${esc((s.remind_days || []).join(', '))}" placeholder="3, 1, 0"></label>
      <label class="field"><span>После срока — раз в, дней</span><input id="dreq-s-every" type="number" min="1" max="14" value="${esc(s.repeat_every)}"></label>
      <label class="field"><span>Повторов после срока</span><input id="dreq-s-times" type="number" min="0" max="10" value="${esc(s.repeat_max)}"></label>
      <label class="field"><span>Почта клиента для запросов</span><input id="dreq-s-email" type="email" maxlength="200" value="${esc(s.contact_email || '')}"></label>
    </div>
    <fieldset class="dreq-set"><legend>Каналы</legend>
      ${Object.entries(DREQ_CHANNELS).map(([code, title]) => box(`dreq-s-ch-${code}`, (s.channels || []).includes(code), title,
        ch[code] && !ch[code].ready ? ch[code].hint : '')).join('')}
    </fieldset>
    <fieldset class="dreq-set"><legend>Кому</legend>
      ${Object.entries(DREQ_ROLES).map(([code, title]) => box(`dreq-s-role-${code}`, (s.roles || []).includes(code), title)).join('')}
      <p class="form-hint">Чек без подтверждения ФНС — всегда лично тому, кто его сканировал.</p>
    </fieldset>
    <p class="form-hint">Плановые напоминания приходят в 10:00 по Москве; в день срока — не позже чем за час до «часа икс».
      После срока — не дольше 30 дней.</p>
    <div id="dreq-s-error" class="form-error dreq-error" aria-live="polite"></div>
    <div class="modal-actions"><button type="button" class="btn btn-primary" id="dreq-s-save">Сохранить</button></div>`;
}

/** Тело PUT /settings из окна настроек; ошибка ввода — { error }. */
export function dreqSettingsBody(get, companyId) {
  const days = dreqParseDays(get('dreq-s-days').value);
  if (days === null) return { error: 'Дни до срока — числа от 0 до 10 через запятую, не больше пяти' };
  const body = {
    enabled: get('dreq-s-enabled').checked, auto_detect: get('dreq-s-auto').checked,
    deadline_day: Number(get('dreq-s-day').value), x_hour: get('dreq-s-hour').value, remind_days: days,
    repeat_every: Number(get('dreq-s-every').value), repeat_max: Number(get('dreq-s-times').value),
    channels: Object.keys(DREQ_CHANNELS).filter((c) => get(`dreq-s-ch-${c}`).checked),
    roles: Object.keys(DREQ_ROLES).filter((r) => get(`dreq-s-role-${r}`).checked),
    contact_email: get('dreq-s-email').value.trim(),
  };
  if (companyId) body.company_id = companyId;
  return body;
}

export async function viewDocRequests(container, opts = {}) {
  const alive = typeof opts.alive === 'function' ? opts.alive : () => true;
  const companies = (Array.isArray(opts.companies) ? opts.companies : []).filter((c) => c.is_active !== false);
  const filterCompany = opts.companyFilter && opts.companyFilter !== 'all' ? opts.companyFilter : '';
  const state = { company: opts.isAdmin ? filterCompany : '', period: '', data: null };
  let req = 0;
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">📋 Что нужно от клиента</div>
      <p class="form-hint">Программа сама находит, чего не хватает для закрытия месяца: документы от поставщиков, которые
        присылают их каждый месяц, чеки без подтверждения ФНС, УПД с ошибками, платежи из банковской выписки без
        документов (раздел «Банк: выписка»). Пришедший документ закрывает запрос сам.
        Напоминания клиенту — по Telegram, MAX и почте, к сроку сдачи и после него; каждое сообщение остаётся в журнале.</p>
      <div class="dreq-controls">
        ${opts.isAdmin ? `<label class="field"><span>Компания</span><select id="dreq-company">
          <option value="">Выберите компанию</option>
          ${companies.map((c) => `<option value="${esc(c.id)}" ${state.company === c.id ? 'selected' : ''}>${esc(c.short_name || c.name)}</option>`).join('')}
        </select></label>` : ''}
        <label class="field"><span>Месяц</span><input id="dreq-period" type="month"></label>
      </div>
      <div id="dreq-head" class="dreq-head" aria-live="polite"></div>
      <div class="dreq-tools">
        <button type="button" class="btn btn-primary btn-sm" id="dreq-detect">🔎 Найти недостающее</button>
        <button type="button" class="btn btn-sm" id="dreq-add">＋ Добавить запрос</button>
        <button type="button" class="btn btn-sm" id="dreq-remind">📣 Напомнить сейчас</button>
        <button type="button" class="btn btn-sm" id="dreq-settings">⚙ Срок и напоминания</button>
      </div>
    </div>
    <div class="glass card">
      <div class="card-title">Запросы за месяц <span class="spacer"></span><span class="form-hint" id="dreq-kpi"></span></div>
      <div id="dreq-out"><div class="skeleton" style="height:120px"></div></div>
    </div>
    <div class="glass card">
      <div class="card-title">Журнал — письменный след</div>
      <div id="dreq-journal"><div class="skeleton" style="height:80px"></div></div>
    </div>`;
  const $ = (sel) => container.querySelector(sel);
  const query = () => {
    const q = new URLSearchParams();
    if (state.company) q.set('company_id', state.company);
    if (state.period) q.set('period', state.period);
    return q;
  };
  const body = (extra = {}) => ({ ...(state.company ? { company_id: state.company } : {}), period: state.period, ...extra });

  function render(data) {
    state.data = data;
    state.period = data.period;
    const period = $('#dreq-period');
    if (period && period.value !== data.period) period.value = data.period;
    const late = data.overdue ? ' <span class="chip failed">срок прошёл</span>' : '';
    $('#dreq-head').innerHTML = `<p>Срок сдачи за ${esc(data.period_title)}: <b>${esc(data.deadline_human)}</b> (МСК)${late}</p>
      <p>${dreqScheduleHtml(data.schedule, data.settings && data.settings.enabled)}</p>
      ${dreqDisciplineText(data.discipline) ? `<p class="form-hint">${esc(dreqDisciplineText(data.discipline))}</p>` : ''}`;
    const s = data.summary || {};
    $('#dreq-kpi').textContent = `Ждём: ${s.open || 0} · получено: ${s.received || 0} · отменено: ${s.cancelled || 0}`;
    $('#dreq-out').innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Что нужно</th><th>Ответственный</th>
      <th class="num">Сумма</th><th>Статус</th><th></th></tr></thead><tbody>${dreqRowsHtml(data.items)}</tbody></table></div>`;
    $('#dreq-out').querySelectorAll('.dreq-act').forEach((b) => { b.onclick = () => setStatus(b.dataset.id, b.dataset.status); });
  }

  async function loadJournal() {
    try {
      const res = await api.get(`/api/v1/doc-requests/journal?${query()}`);
      if (!container.isConnected) return;
      $('#dreq-journal').innerHTML = dreqJournalHtml(res.items);
    } catch (e) {
      if (container.isConnected) $('#dreq-journal').innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function load() {
    if (opts.isAdmin && !state.company) {
      $('#dreq-out').innerHTML = '<p class="form-hint">Выберите компанию — у каждой свой срок сдачи и свои запросы.</p>';
      $('#dreq-journal').innerHTML = '';
      return;
    }
    const current = ++req;
    try {
      const data = await api.get(`/api/v1/doc-requests?${query()}`);
      if (current !== req || !alive() || !container.isConnected) return;
      render(data);
      loadJournal();
    } catch (e) {
      if (current === req && container.isConnected) $('#dreq-out').innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function setStatus(id, status) {
    let note = '';
    if (status === 'cancelled') {
      note = window.prompt('Почему запрос больше не нужен? (попадёт в журнал)', '') ?? null;
      if (note === null) return;
    }
    try {
      await api.post(`/api/v1/doc-requests/${encodeURIComponent(id)}/status`, { status, note });
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function detect() {
    const btn = $('#dreq-detect');
    btn.disabled = true;
    try {
      const res = await api.post('/api/v1/doc-requests/detect', body());
      toast(res.created ? `Найдено недостающего: ${res.created}` : 'Нового недостающего нет', 'ok');
      if (res.overview && container.isConnected) render(res.overview);
      loadJournal();
    } catch (e) { toast(e.message, 'err'); }
    btn.disabled = false;
  }

  function addRequest() {
    const { slot, close } = openModal(`<h3 class="modal-title">＋ Запрос документа</h3>
      <label class="field"><span>Что нужно</span><input id="dreq-a-title" maxlength="500" placeholder="Например: акт сверки с ООО «Песок» за октябрь"></label>
      <div class="dreq-grid">
        <label class="field"><span>От кого (необязательно)</span><input id="dreq-a-name" maxlength="500"></label>
        <label class="field"><span>ИНН (необязательно)</span><input id="dreq-a-inn" maxlength="20" inputmode="numeric"></label>
        <label class="field"><span>Сумма, ₽ (необязательно)</span><input id="dreq-a-amount" inputmode="decimal"></label>
      </div>
      <div id="dreq-a-error" class="form-error dreq-error" aria-live="polite"></div>
      <div class="modal-actions"><button type="button" class="btn btn-primary" id="dreq-a-save">Добавить</button></div>`);
    if (!slot) return;
    const get = (id) => slot.querySelector(`#${id}`);
    get('dreq-a-save').onclick = async () => {
      const raw = get('dreq-a-amount').value.replace(/\s/g, '').replace(',', '.');
      const amount = raw ? Number(raw) : null;
      if (raw && (Number.isNaN(amount) || amount < 0)) { get('dreq-a-error').textContent = 'Сумма — число, например 1500,00'; return; }
      try {
        await api.post('/api/v1/doc-requests', body({ title: get('dreq-a-title').value, counterparty_name: get('dreq-a-name').value,
          counterparty_inn: get('dreq-a-inn').value, amount }));
        toast('Запрос добавлен', 'ok');
        if (typeof close === 'function') close();
        load();
      } catch (e) { get('dreq-a-error').textContent = e.message; }
    };
  }

  async function remindNow() {
    let pre;
    try { pre = await api.get(`/api/v1/doc-requests/preview?${query()}`); }
    catch (e) { toast(e.message, 'err'); return; }
    const list = Array.isArray(pre.recipients) ? pre.recipients : [];
    const rows = list.map((r) => `<li><b>${esc(r.recipient)}</b> — документов: ${esc(r.items)}; ${r.channels.length
      ? `каналы: ${r.channels.map((c) => esc(DREQ_CHANNELS[c] || c)).join(', ')}` : '<span class="form-error">нет подключённых каналов</span>'}</li>`).join('');
    const { slot, close } = openModal(`<h3 class="modal-title">📣 Напомнить сейчас</h3>
      ${pre.warning ? `<div class="form-error">${esc(pre.warning)}</div>` : ''}
      ${list.length ? `<p class="form-hint">Кому уйдёт напоминание:</p><ul class="prim-results">${rows}</ul>
        <details><summary>Текст сообщения</summary><pre class="dreq-text">${esc(list[0].text)}</pre></details>`
        : '<p>Открытых запросов нет — напоминать не о чем.</p>'}
      <div class="modal-actions"><button type="button" class="btn btn-primary" id="dreq-r-send" ${list.length ? '' : 'disabled'}>Отправить</button></div>`);
    if (!slot) return;
    slot.querySelector('#dreq-r-send').onclick = async () => {
      try {
        const res = await api.post('/api/v1/doc-requests/remind', body());
        const done = res.note || `Отправлено: ${res.sent}${res.failed ? `, не доставлено: ${res.failed}` : ''}`;
        toast(res.warning ? `${done}. ${res.warning}` : done, res.failed || res.note || res.warning ? 'err' : 'ok');
        if (typeof close === 'function') close();
        loadJournal();
      } catch (e) { toast(e.message, 'err'); }
    };
  }

  function settings() {
    const data = state.data;
    if (!data) return;
    const { slot, close } = openModal(dreqSettingsHtml(data.settings, data.channels));
    if (!slot) return;
    const get = (id) => slot.querySelector(`#${id}`);
    get('dreq-s-save').onclick = async () => {
      const payload = dreqSettingsBody(get, state.company);
      if (payload.error) { get('dreq-s-error').textContent = payload.error; return; }
      try {
        await api.put('/api/v1/doc-requests/settings', payload);
        toast(payload.enabled ? 'Сохранено: напоминания включены' : 'Сохранено', 'ok');
        if (typeof close === 'function') close();
        load();
      } catch (e) { get('dreq-s-error').textContent = e.message; }
    };
  }

  const company = $('#dreq-company');
  if (company) company.onchange = (e) => { state.company = e.target.value; state.period = ''; load(); };
  $('#dreq-period').onchange = (e) => { state.period = e.target.value; load(); };
  $('#dreq-detect').onclick = () => detect();
  $('#dreq-add').onclick = () => addRequest();
  $('#dreq-remind').onclick = () => remindNow();
  $('#dreq-settings').onclick = () => settings();
  await load();
}
