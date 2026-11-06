/* v1.70.112 (T-46, ADR-24, ADR-42): согласование авансовых отчётов — экран «Согласования».
   Маршрут: сотрудник → руководитель → бухгалтер. Подпись — простая электронная подпись в программе: вход под
   своей учётной записью, явное подтверждение действия над конкретным содержимым (хеш SHA-256 снимка отчёта) и
   запись в журнале. Этап без ответа за срок компании (по умолчанию 72 часа) считается согласованным — отдельным
   событием журнала. Включает администратор (выключено по умолчанию). Имена верхнего уровня — с приставкой
   approvals (общая область модулей в сборке). */
import { api } from './api.js';
import { esc, fmtSum, openModal, toast } from './ui.js';

export const APPROVALS_STATUS = {
  draft: ['chip new', 'ждёт подписи сотрудника'],
  pending: ['chip verifying', 'на согласовании'],
  approved: ['chip verified', 'согласован'],
  rejected: ['chip failed', 'отклонён'],
  cancelled: ['chip mono', 'отменён'],
};
export const APPROVALS_STAGES = [['employee', 'Сотрудник'], ['manager', 'Руководитель'], ['accountant', 'Бухгалтер']];
export const APPROVALS_ACTIONS = {
  created: 'отчёт сформирован', signed: 'подписал', approved: 'согласовал', rejected: 'отклонил',
  cancelled: 'отменил', auto_approved: 'согласовано автоматически', threshold_approved: 'согласовано по порогу',
  skipped: 'этап пропущен', completed: 'согласование завершено', notified: 'уведомления',
};
export const APPROVALS_FNS = {
  valid: ['chip valid', 'ФНС: подтверждён'], invalid: ['chip invalid', 'ФНС: не подтверждён'],
  not_found: ['chip not_found', 'ФНС: не найден'], unknown: ['chip unknown', 'ФНС: не проверен'],
};
const APPROVALS_BOXES = [['todo', 'Ждут меня'], ['mine', 'Мои отчёты'], ['all', 'Все']];
const APPROVALS_MONTHS = ['января', 'февраля', 'марта', 'апреля', 'мая', 'июня', 'июля', 'августа', 'сентября',
  'октября', 'ноября', 'декабря'];

/** «ГГГГ-ММ-ДД» → «ДД.ММ.ГГГГ». */
export function approvalsDay(d) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(d || ''));
  return m ? `${m[3]}.${m[2]}.${m[1]}` : String(d || '');
}

/** Момент UTC («…Z») → «6 ноября 2026, 15:40» в поясе браузера. */
export function approvalsWhen(iso) {
  if (!iso) return '';
  const d = new Date(iso);
  if (Number.isNaN(d.getTime())) return '';
  const hh = String(d.getHours()).padStart(2, '0'), mm = String(d.getMinutes()).padStart(2, '0');
  return `${d.getDate()} ${APPROVALS_MONTHS[d.getMonth()]} ${d.getFullYear()}, ${hh}:${mm}`;
}

/** Сколько осталось до автосогласования этапа: минуты → «2 д 5 ч», «3 ч 10 мин», «45 мин». */
export function approvalsLeft(minutes) {
  if (minutes === null || minutes === undefined) return '';
  const m = Math.max(0, Math.floor(Number(minutes) || 0));
  if (m === 0) return 'срок вышел — этап согласуется автоматически';
  const d = Math.floor(m / 1440), h = Math.floor((m % 1440) / 60), mm = m % 60;
  if (d) return `до автосогласования ${d} д${h ? ` ${h} ч` : ''}`;
  if (h) return `до автосогласования ${h} ч${mm ? ` ${mm} мин` : ''}`;
  return `до автосогласования ${mm} мин`;
}

/** «12 чеков» — склонение. */
export function approvalsReceipts(n) {
  const a = Math.abs(Number(n) || 0) % 100, b = a % 10;
  const word = (a > 10 && a < 20) ? 'чеков' : b === 1 ? 'чек' : (b >= 2 && b <= 4) ? 'чека' : 'чеков';
  return `${Number(n) || 0} ${word}`;
}

export function approvalsStatusChip(status) {
  const [cls, title] = APPROVALS_STATUS[status] || ['chip', status || '—'];
  return `<span class="${cls}">${esc(title)}</span>`;
}

/** Карточка отчёта в списке. */
export function approvalsCardHtml(a) {
  const stage = a.status === 'pending' ? ` · этап: ${esc(a.stage_title || '')}` : '';
  const left = a.status === 'pending' ? `<div class="form-hint ap-left">⏳ ${esc(approvalsLeft(a.minutes_left))}</div>` : '';
  return `
    <button type="button" class="glass card ap-card" data-id="${esc(a.id)}">
      <div class="ap-card-top"><b>${esc(a.number || 'АО')}</b> ${approvalsStatusChip(a.status)}</div>
      <div>${esc(a.employee_name)} · ${esc(approvalsDay(a.date_from))}–${esc(approvalsDay(a.date_to))}</div>
      <div class="ap-sum">${esc(approvalsReceipts(a.receipts_count))} · к учёту <b>${esc(fmtSum(a.total))}</b>${stage}</div>
      ${left}
    </button>`;
}

export function approvalsListHtml(items, box) {
  if (!items || !items.length) {
    const empty = box === 'todo' ? 'Сейчас от вас ничего не ждут.' : 'Отчётов пока нет.';
    return `<div class="glass card form-hint">${esc(empty)}</div>`;
  }
  return items.map(approvalsCardHtml).join('');
}

/** Состояние шагов маршрута по журналу: done | auto | skipped | current | rejected | wait. */
export function approvalsRoute(a, events) {
  const out = {};
  for (const [stage] of APPROVALS_STAGES) out[stage] = { state: 'wait', who: '', note: '' };
  for (const e of events || []) {
    const step = out[e.stage];
    if (!step) continue;
    if (e.action === 'signed' || e.action === 'approved') Object.assign(step, { state: 'done', who: e.actor_name });
    else if (e.action === 'auto_approved') Object.assign(step, { state: 'auto', note: 'без ответа — согласовано' });
    else if (e.action === 'threshold_approved') Object.assign(step, { state: 'auto', note: 'по порогу' });
    else if (e.action === 'skipped') Object.assign(step, { state: 'skipped', note: 'этап пропущен' });
    else if (e.action === 'rejected') Object.assign(step, { state: 'rejected', who: e.actor_name });
  }
  if ((a.status === 'draft' || a.status === 'pending') && out[a.stage] && out[a.stage].state === 'wait') {
    out[a.stage].state = 'current';
  }
  return out;
}

export function approvalsRouteHtml(a, events) {
  const route = approvalsRoute(a, events);
  const icons = { done: '✅', auto: '⏱️', skipped: '↷', current: '✍️', rejected: '⛔', wait: '○' };
  return `<ol class="ap-route">${APPROVALS_STAGES.map(([stage, title]) => {
    const s = route[stage];
    const sub = s.who || s.note || (s.state === 'current' ? 'ждём решения' : '');
    return `<li class="ap-step ap-${s.state}"><span class="ap-ico">${icons[s.state]}</span>
      <b>${esc(title)}</b>${sub ? `<small>${esc(sub)}</small>` : ''}</li>`;
  }).join('')}</ol>`;
}

/** Строка журнала: время, этап, кто и что сделал; у подписи — отметка ПЭП, адрес и начало хеша. */
export function approvalsEventHtml(e) {
  const action = APPROVALS_ACTIONS[e.action] || e.action;
  const who = e.actor_name ? `${esc(e.actor_name)} — ` : 'Программа — ';
  const pep = e.pep ? `<span class="chip verified ap-pep" title="простая электронная подпись">ПЭП</span>` : '';
  const trail = e.pep ? `<small class="ap-trail">адрес ${esc(e.ip || '—')} · содержимое ${esc((e.doc_hash || '').slice(0, 12))}…</small>` : '';
  const stage = e.stage_title ? ` <small>(${esc(e.stage_title)})</small>` : '';
  return `<li class="ap-event"><time>${esc(approvalsWhen(e.created_at))}</time>
    <div>${pep} ${who}${esc(action)}${stage}${e.comment ? `: ${esc(e.comment)}` : ''}${trail}</div></li>`;
}

/** Текст подтверждения: что именно подписывает человек (сотрудник — отчёт, согласующий — своё решение). */
export function approvalsConfirmText(a, name, kind) {
  const verb = kind === 'sign' ? 'подписываю' : 'подтверждаю своё решение по';
  return `Я, ${name}, ${verb} простой электронной подписью авансов${kind === 'sign' ? 'ый отчёт' : 'ому отчёту'} ` +
    `${a.number || ''} (${a.employee_name}, ${approvalsDay(a.date_from)}–${approvalsDay(a.date_to)}, ` +
    `${approvalsReceipts(a.receipts_count)}, к учёту ${fmtSum(a.total)}). Содержимое: SHA-256 ` +
    `${String(a.doc_hash || '').slice(0, 16)}…`;
}

function approvalsIntegrityHtml(d) {
  const ok = d.integrity && d.integrity.content && d.integrity.journal && d.integrity.journal.ok;
  if (ok) return '<div class="form-hint ap-ok">🔒 Содержимое и журнал целы: печати сходятся.</div>';
  const broken = d.integrity && d.integrity.journal && !d.integrity.journal.ok
    ? ` Журнал нарушен с записи № ${d.integrity.journal.broken_at}.` : '';
  return `<div class="ap-warn">⚠️ Целостность нарушена: ${d.integrity && d.integrity.content ? '' : 'содержимое не сходится с хешем.'}${broken} Сообщите администратору.</div>`;
}

function approvalsChangesHtml(d) {
  if (!d.changes || !d.changes.length) return '';
  const rows = d.changes.map((c) => c.kind === 'removed'
    ? `<li>${esc(c.merchant || c.id)} — чек удалён или перенесён</li>`
    : `<li>${esc(c.merchant || c.id)} — было ${esc(fmtSum(c.was))}, сейчас ${esc(fmtSum(c.now))}${c.fns_was !== c.fns_now
      ? `, ФНС: ${esc(c.fns_now)}` : ''}</li>`).join('');
  return `<div class="ap-warn">⚠️ После формирования изменены чеки (${d.changes.length}). Подпись ставится под составом
    на момент формирования; при сомнении отклоните отчёт — сотрудник сформирует его заново.<ul>${rows}</ul></div>`;
}

export function approvalsItemsHtml(snapshot) {
  const items = (snapshot && snapshot.items) || [];
  return `<div class="table-wrap"><table class="table ap-items"><thead><tr><th>Дата</th><th>Продавец</th>
    <th>Статья</th><th class="num">К учёту</th><th>Проверка</th></tr></thead><tbody>${items.map((i) => {
    const [cls, title] = APPROVALS_FNS[i.fns] || APPROVALS_FNS.unknown;
    const date = i.date ? `${approvalsDay(i.date)} ${i.date.slice(11, 16)}` : '';
    return `<tr><td>${esc(date)}</td><td>${esc(i.merchant || '—')}${i.return ? ' <small>(возврат)</small>' : ''}</td>
      <td>${esc(i.category || '—')}</td><td class="num">${esc(fmtSum(i.amount))}</td>
      <td><span class="${cls}">${esc(title)}</span>${i.flags ? ' <span class="chip failed">политика</span>' : ''}</td></tr>`;
  }).join('')}</tbody><tfoot><tr><td colspan="3"><b>Итого к учёту</b></td>
    <td class="num"><b>${esc(fmtSum(snapshot && snapshot.total))}</b></td><td></td></tr></tfoot></table></div>`;
}

/** Лист согласования для печати: реквизиты отчёта и подписи с адресами и хешем содержимого. */
export function approvalsSheetHtml(d) {
  const snap = d.snapshot || {};
  const signs = (d.events || []).filter((e) => e.pep || ['auto_approved', 'threshold_approved', 'skipped',
    'completed'].includes(e.action));
  return `<!doctype html><html lang="ru"><head><meta charset="utf-8"><title>Лист согласования ${esc(d.number)}</title>
<style>body{font:14px/1.45 system-ui,sans-serif;margin:24px;color:#111}table{border-collapse:collapse;width:100%}
td,th{border:1px solid #999;padding:6px;text-align:left;vertical-align:top}h1{font-size:18px}small{color:#555}</style>
</head><body><h1>Лист согласования авансового отчёта ${esc(d.number)}</h1>
<p>Организация: ${esc((snap.company && snap.company.name) || '')}${snap.company && snap.company.inn ? `, ИНН ${esc(snap.company.inn)}` : ''}<br>
Подотчётное лицо: ${esc(d.employee_name)}<br>Период: ${esc(approvalsDay(d.date_from))}–${esc(approvalsDay(d.date_to))};
${esc(approvalsReceipts(d.receipts_count))}; к учёту ${esc(fmtSum(d.total))}<br>
Содержимое (SHA-256): <code>${esc(d.doc_hash)}</code><br>Статус: ${esc((APPROVALS_STATUS[d.status] || ['', d.status])[1])}</p>
<table><thead><tr><th>Этап</th><th>Решение</th><th>Кто</th><th>Когда</th><th>Адрес</th></tr></thead><tbody>
${signs.map((e) => `<tr><td>${esc(e.stage_title || '—')}</td><td>${esc(APPROVALS_ACTIONS[e.action] || e.action)}${e.pep
    ? ' (ПЭП)' : ''}${e.comment ? `<br><small>${esc(e.comment)}</small>` : ''}</td><td>${esc(e.actor_name || 'программа')}</td>
<td>${esc(approvalsWhen(e.created_at))}</td><td>${esc(e.ip || '—')}</td></tr>`).join('')}
</tbody></table><p><small>Документ подписан простой электронной подписью в программе «Ямастер Чек» (63-ФЗ, ст. 9):
вход под учётной записью, подтверждение действия и запись в журнале с печатью целостности. Порядок — по
внутреннему положению компании об электронной подписи.</small></p></body></html>`;
}

function approvalsSettingsHtml(data) {
  const s = data.settings;
  const ch = data.channels || {};
  const lim = (v) => (Number(v) > 0 ? fmtSum(v) : 'выключен');
  if (!data.can_edit) {
    return `<p class="form-hint">Согласование ${s.enabled ? '<b>включено</b>' : '<b>выключено</b>'} администратором.
      Срок ответа — ${esc(s.timeout_hours)} ч. Порог руководителя — ${esc(lim(s.manager_limit))}, порог бухгалтера —
      ${esc(lim(s.accountant_limit))}.</p>`;
  }
  const box = (key, title) => `<label class="check"><input type="checkbox" class="ap-ch" value="${key}"
    ${s.channels.includes(key) ? 'checked' : ''}> ${title}${ch[key] && !ch[key].ready ? ` <small>(${esc(ch[key].hint)})</small>` : ''}</label>`;
  return `<form id="ap-settings" class="ap-settings">
    <label class="check"><input type="checkbox" id="ap-enabled" ${s.enabled ? 'checked' : ''}> Согласование включено в компании</label>
    <label class="field"><span>Срок ответа этапа, ч (без ответа — согласовано)</span>
      <input type="number" id="ap-timeout" min="24" max="168" value="${esc(s.timeout_hours)}"></label>
    <label class="field"><span>Порог руководителя, ₽ (до суммы — без руководителя; 0 — выключен)</span>
      <input type="number" id="ap-mlimit" min="0" step="0.01" value="${esc(s.manager_limit)}"></label>
    <label class="field"><span>Порог бухгалтера, ₽ (до суммы, если все чеки подтверждены ФНС; 0 — выключен)</span>
      <input type="number" id="ap-alimit" min="0" step="0.01" value="${esc(s.accountant_limit)}"></label>
    <div class="field"><span>Уведомления</span>${box('telegram', 'Telegram')}${box('max', 'MAX')}</div>
    <button type="submit" class="btn btn-primary">Сохранить</button></form>`;
}

export async function viewApprovals(container, opts = {}) {
  const alive = opts.alive || (() => true);
  const me = opts.me || {};
  const isAdmin = me.role === 'admin';
  const companies = (Array.isArray(opts.companies) ? opts.companies : []).filter((c) => c.is_active !== false);
  const st = { box: 'todo', company: isAdmin && opts.companyFilter && opts.companyFilter !== 'all' ? opts.companyFilter : '' };
  container.innerHTML = '<div class="glass card"><div class="skeleton"></div><div class="skeleton"></div></div>';
  const q = (extra = {}) => {
    const p = new URLSearchParams(extra);
    if (st.company) p.set('company_id', st.company);
    const s = p.toString();
    return s ? `?${s}` : '';
  };

  async function load() {
    let list, conf = null;
    try {
      list = await api.get('/api/v1/approvals' + q({ box: st.box }));
      if (!isAdmin || st.company) conf = await api.get('/api/v1/approvals/settings' + q());
    } catch (e) {
      if (alive()) container.innerHTML = `<div class="glass card">Не удалось загрузить согласования: ${esc(e.message)}</div>`;
      return;
    }
    if (alive()) render(list, conf);
  }

  function render(list, conf) {
    const enabled = conf ? conf.settings.enabled : false;
    const hours = conf ? conf.settings.timeout_hours : 72;
    container.innerHTML = `
      <div class="glass card ap-top">
        <div class="card-title">✍️ Согласование авансовых отчётов</div>
        <p class="form-hint">Маршрут: сотрудник → руководитель → бухгалтер. Подпись — простая электронная подпись в
          программе: вход, подтверждение и запись в журнале. Этап без ответа ${esc(hours)} ч считается согласованным.</p>
        ${isAdmin ? `<label class="field"><span>Компания</span><select id="ap-company"><option value="">— выберите компанию —</option>
          ${companies.map((c) => `<option value="${esc(c.id)}" ${st.company === c.id ? 'selected' : ''}>${esc(c.short_name || c.name)}</option>`).join('')}
        </select></label>` : ''}
        ${conf ? `<details class="ap-conf"${isAdmin && !enabled ? ' open' : ''}><summary>Настройки согласования</summary>
          ${approvalsSettingsHtml(conf)}</details>` : ''}
        <div class="cl-tools">
          <div class="segmented ap-boxes" role="group" aria-label="Отчёты">${APPROVALS_BOXES.map(([key, title]) =>
            `<button type="button" data-box="${key}"${key === st.box ? ' class="active"' : ''}>${esc(title)}${key !== 'all'
              && list.counts ? ` <span class="badge">${esc(list.counts[key] || 0)}</span>` : ''}</button>`).join('')}</div>
          ${enabled && !isAdmin ? '<button type="button" class="btn btn-primary" id="ap-new">＋ Авансовый отчёт</button>' : ''}
        </div>
        ${conf && !enabled ? '<p class="form-hint">Согласование в компании выключено — включает администратор.</p>' : ''}
      </div>
      <div class="ap-list" id="ap-list">${approvalsListHtml(list.items, st.box)}</div>`;
    const company = container.querySelector('#ap-company');
    if (company) company.onchange = (e) => { st.company = e.target.value; load(); };
    container.querySelectorAll('.ap-boxes button').forEach((btn) => {
      btn.onclick = () => { st.box = btn.dataset.box; load(); };
    });
    const add = container.querySelector('#ap-new');
    if (add) add.onclick = () => approvalsCreate(me, st.company, load);
    container.querySelector('#ap-list').onclick = (e) => {
      const card = e.target.closest('[data-id]');
      if (card) approvalsOpen(card.dataset.id, me, load);
    };
    const form = container.querySelector('#ap-settings');
    if (form) {
      form.onsubmit = async (e) => {
        e.preventDefault();
        const body = {
          company_id: st.company, enabled: form.querySelector('#ap-enabled').checked,
          timeout_hours: Number(form.querySelector('#ap-timeout').value || 72),
          manager_limit: Number(form.querySelector('#ap-mlimit').value || 0),
          accountant_limit: Number(form.querySelector('#ap-alimit').value || 0),
          channels: [...form.querySelectorAll('.ap-ch:checked')].map((x) => x.value),
        };
        try {
          await api.put('/api/v1/approvals/settings', body);
          toast(body.enabled ? 'Согласование включено' : 'Согласование выключено');
          load();
        } catch (err) { toast(err.message, 'err'); }
      };
    }
  }

  await load();
}

/** Новый отчёт: сотрудник — себе; бухгалтер — любому сотруднику компании. */
export async function approvalsCreate(me, companyId, done) {
  const q = companyId ? `?company_id=${encodeURIComponent(companyId)}` : '';
  let people = [];
  try { people = (await api.get('/api/v1/approvals/employees' + q)).items || []; } catch (e) { toast(e.message, 'err'); return; }
  const now = new Date();
  const pad = (n) => String(n).padStart(2, '0');
  const first = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-01`;
  const today = `${now.getFullYear()}-${pad(now.getMonth() + 1)}-${pad(now.getDate())}`;
  const { slot, close } = openModal(`
    <h3>Авансовый отчёт на согласование</h3>
    <label class="field"><span>Подотчётное лицо</span><select id="ap-emp">${people.map((p) =>
      `<option value="${esc(p.id)}" ${p.id === me.id ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}</select></label>
    <div class="grid-2"><label class="field"><span>С</span><input type="date" id="ap-from" value="${first}"></label>
      <label class="field"><span>По</span><input type="date" id="ap-to" value="${today}"></label></div>
    <label class="field"><span>Назначение (необязательно)</span><input type="text" id="ap-purpose" maxlength="500"
      placeholder="например, материалы для объекта на ул. Ленина"></label>
    <div id="ap-preview" class="form-hint">Выберите период — покажем чеки сотрудника.</div>
    <div class="modal-actions"><button type="button" class="btn" id="ap-check">Показать чеки</button>
      <button type="button" class="btn btn-primary" id="ap-make" disabled>Сформировать</button></div>`);
  const val = (id) => slot.querySelector(id).value;
  slot.querySelector('#ap-check').onclick = async () => {
    const p = new URLSearchParams({ date_from: val('#ap-from'), date_to: val('#ap-to'), employee_id: val('#ap-emp') });
    if (companyId) p.set('company_id', companyId);
    try {
      const r = await api.get('/api/v1/approvals/preview?' + p.toString());
      slot.querySelector('#ap-preview').innerHTML = r.count
        ? `${esc(approvalsReceipts(r.count))} · к учёту <b>${esc(fmtSum(r.total))}</b>`
        : 'За период нет чеков сотрудника.';
      slot.querySelector('#ap-make').disabled = !r.count;
    } catch (e) { toast(e.message, 'err'); }
  };
  slot.querySelector('#ap-make').onclick = async () => {
    try {
      const d = await api.post('/api/v1/approvals', {
        company_id: companyId || null, employee_id: val('#ap-emp'), date_from: val('#ap-from'), date_to: val('#ap-to'),
        purpose: val('#ap-purpose'),
      });
      close();
      toast(`Отчёт ${d.number} сформирован`);
      approvalsShow(d, me, done);
    } catch (e) { toast(e.message, 'err'); }
  };
}

export async function approvalsOpen(id, me, done) {
  try { approvalsShow(await api.get(`/api/v1/approvals/${encodeURIComponent(id)}`), me, done); } catch (e) { toast(e.message, 'err'); }
}

function approvalsShow(d, me, done) {
  const name = me.full_name || me.username || '';
  const can = d.can || {};
  const kind = can.sign ? 'sign' : can.decide ? 'decide' : '';
  const action = kind ? `
    <div class="ap-sign glass">
      <label class="check"><input type="checkbox" id="ap-agree"> <span>${esc(approvalsConfirmText(d, name, kind))}</span></label>
      ${can.decide ? '<label class="field"><span>Комментарий (для отказа — обязательно)</span><textarea id="ap-comment" maxlength="2000" rows="2"></textarea></label>' : ''}
      <div class="modal-actions">
        ${can.sign ? '<button type="button" class="btn btn-primary" id="ap-do-sign" disabled>Подписать и отправить</button>' : ''}
        ${can.decide ? `<button type="button" class="btn btn-primary" id="ap-do-approve" disabled>Согласовать</button>
          <button type="button" class="btn btn-danger" id="ap-do-reject" disabled>Отклонить</button>` : ''}
      </div></div>` : '';
  const { slot, close } = openModal(`
    <h3>Авансовый отчёт ${esc(d.number)} ${approvalsStatusChip(d.status)}</h3>
    <p>${esc(d.employee_name)} · ${esc(approvalsDay(d.date_from))}–${esc(approvalsDay(d.date_to))} ·
      ${esc(approvalsReceipts(d.receipts_count))} · к учёту <b>${esc(fmtSum(d.total))}</b>${d.purpose ? `<br><small>${esc(d.purpose)}</small>` : ''}</p>
    ${approvalsRouteHtml(d, d.events)}
    ${d.status === 'pending' ? `<p class="form-hint">⏳ ${esc(approvalsLeft(d.minutes_left))}${d.approvers && d.approvers.length
      ? ` · решают: ${esc(d.approvers.join(', '))}` : ''}</p>` : ''}
    ${approvalsIntegrityHtml(d)}${approvalsChangesHtml(d)}
    ${approvalsItemsHtml(d.snapshot)}
    ${action}
    <details class="ap-journal" open><summary>Журнал решений</summary><ol class="ap-events" id="ap-events">${(d.events || [])
      .map(approvalsEventHtml).join('')}</ol></details>
    <div class="modal-actions">
      <button type="button" class="btn" id="ap-print">🖨 Лист согласования</button>
      ${can.cancel ? '<button type="button" class="btn" id="ap-cancel">Отменить отчёт</button>' : ''}
    </div>`);
  const agree = slot.querySelector('#ap-agree');
  const buttons = ['#ap-do-sign', '#ap-do-approve', '#ap-do-reject'].map((s) => slot.querySelector(s)).filter(Boolean);
  if (agree) agree.onchange = () => buttons.forEach((b) => { b.disabled = !agree.checked; });
  const after = (r, message) => { close(); toast(message); if (done) done(); approvalsShow(r, me, done); };
  const fail = (e) => toast(e.message, 'err');
  const signBtn = slot.querySelector('#ap-do-sign');
  if (signBtn) {
    signBtn.onclick = async () => {
      try { after(await api.post(`/api/v1/approvals/${d.id}/sign`, { doc_hash: d.doc_hash, confirm: true }), 'Отчёт подписан и отправлен'); } catch (e) { fail(e); }
    };
  }
  const decide = (decision) => async () => {
    const comment = (slot.querySelector('#ap-comment') || {}).value || '';
    try {
      const r = await api.post(`/api/v1/approvals/${d.id}/decision`, { decision, doc_hash: d.doc_hash, confirm: true, comment });
      after(r, decision === 'approve' ? 'Отчёт согласован' : 'Отчёт отклонён');
    } catch (e) { fail(e); }
  };
  const approveBtn = slot.querySelector('#ap-do-approve');
  if (approveBtn) approveBtn.onclick = decide('approve');
  const rejectBtn = slot.querySelector('#ap-do-reject');
  if (rejectBtn) rejectBtn.onclick = decide('reject');
  const cancelBtn = slot.querySelector('#ap-cancel');
  if (cancelBtn) {
    cancelBtn.onclick = async () => {
      try { after(await api.post(`/api/v1/approvals/${d.id}/cancel`, { comment: '' }), 'Отчёт отменён'); } catch (e) { fail(e); }
    };
  }
  slot.querySelector('#ap-print').onclick = () => {
    const w = window.open('', '_blank');
    if (!w) { toast('Браузер не дал открыть окно печати', 'err'); return; }
    w.document.write(approvalsSheetHtml(d));
    w.document.close();
    w.focus();
    w.print();
  };
}
