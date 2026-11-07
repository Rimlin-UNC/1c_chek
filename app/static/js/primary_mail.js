/* v1.70.104 (T-63, ADR-34): первичка из почтового ящика компании.
   Панель «📬 Почта для документов» — в карточке компании и на экране «Первичка»: адрес IMAP-сервера,
   логин, пароль приложения, папка, от кого принимать и с какой даты; «Сохранить и проверить»,
   «Загрузить сейчас», «Отключить». Ящик читается только на чтение: письма остаются непрочитанными.
   Карточка «Из почты» на экране «Первичка»: черновики PDF и сканов на сверку и журнал вложений.
   Все имена верхнего уровня — с приставкой mail (общая область модулей в сборке). */
import { api } from './api.js';
import { esc, toast } from './ui.js';

export const MAIL_PROVIDERS = [
  { id: 'yandex', title: 'Яндекс 360 / Яндекс Почта', host: 'imap.yandex.ru', port: 993, security: 'ssl',
    how: 'Яндекс ID → «Безопасность» → «Пароли приложений» → «Почта»; в настройках почты включите доступ по IMAP.' },
  { id: 'mailru', title: 'Mail.ru / VK WorkSpace', host: 'imap.mail.ru', port: 993, security: 'ssl',
    how: 'Настройки → «Безопасность» → «Пароли для внешних приложений»; доступ по IMAP включён по умолчанию.' },
  { id: 'rambler', title: 'Рамблер Почта', host: 'imap.rambler.ru', port: 993, security: 'ssl',
    how: 'Настройки → «Программы» → «Доступ к почтовому ящику с помощью почтовых клиентов» → «Вкл.».' },
  { id: 'custom', title: 'Свой почтовый сервер', host: '', port: 993, security: 'ssl',
    how: 'Адрес IMAP-сервера и порт — у администратора почты; вход только по TLS (993 или STARTTLS на 143).' },
];

const MAIL_STATUS = {
  '': ['chip unknown', 'не подключён'],
  new: ['chip verifying', 'сохранён, не проверен'],
  active: ['chip verified', 'подключён'],
  error: ['chip failed', 'ошибка подключения'],
};

export function mailProviderOf(host) {
  const h = String(host || '').toLowerCase();
  return (MAIL_PROVIDERS.find((p) => p.host && p.host === h) || MAIL_PROVIDERS[MAIL_PROVIDERS.length - 1]).id;
}

export function mailWhen(iso) {
  if (!iso) return '—';
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Итог проверки ящика одной строкой: «писем: 2 · в реестр: 1 · на сверку: 1». */
export function mailResultSummary(res) {
  const r = res || {};
  const parts = [`писем: ${Number(r.messages) || 0}`];
  const add = (key, title) => { if (Number(r[key])) parts.push(`${title}: ${Number(r[key])}`); };
  add('created', 'в реестр');
  add('drafts', 'на сверку');
  add('queued', 'распознаётся');
  add('duplicates', 'дублей');
  add('rejected', 'не принято');
  add('skipped', 'не УПД');
  add('foreign', 'от других отправителей');
  add('too_big', 'слишком больших');
  if (Number(r.left)) parts.push(`ещё писем в очереди: ${Number(r.left)}`);
  return parts.join(' · ');
}

export function mailPanelHtml(state) {
  const d = state || {};
  const status = MAIL_STATUS[d.status] ? d.status : '';
  const [cls, label] = MAIL_STATUS[status];
  const active = status === 'active';
  const provider = d.configured ? mailProviderOf(d.host) : 'yandex';
  const meta = MAIL_PROVIDERS.find((p) => p.id === provider) || MAIL_PROVIDERS[0];
  const host = d.configured ? d.host : meta.host;
  const security = d.security === 'starttls' ? 'starttls' : 'ssl';
  const facts = [d.last_check_at ? `последняя проверка ${mailWhen(d.last_check_at)}` : '',
    active ? `проверка каждые ${Number(d.check_every_min) || 15} минут` : '',
    Number(d.drafts) ? `ждут сверки: ${Number(d.drafts)}` : ''].filter(Boolean).join(' · ');
  const res = d.last_result || {};
  return `<div class="bank-conn-head"><div class="card-title">📬 Почта для документов</div><span class="${cls}">${esc(label)}</span></div>
    <p class="form-hint">Письма поставщиков с документами разбираются сами: XML и ZIP из ЭДО — сразу в реестр первички,
      PDF, сканы и фото — черновиками на сверку. Ящик читается только на чтение: письма остаются непрочитанными,
      ничего не удаляется и не перемещается. Удобно завести отдельный ящик или пересылать в него письма с документами.</p>
    ${facts ? `<div class="form-hint">${esc(facts)}</div>` : ''}
    ${active && res.ok ? `<div class="form-hint">Последняя загрузка: ${esc(mailResultSummary(res))}</div>` : ''}
    ${d.last_error ? `<div class="form-error">${esc(d.last_error)}</div>` : ''}
    <details ${active ? '' : 'open'}><summary>Настройки ящика${d.username ? ` — ${esc(d.username)}` : ''}</summary>
      <div class="dreq-grid">
        <label class="field"><span>Почта</span><select id="mailc-provider">${MAIL_PROVIDERS.map((p) =>
          `<option value="${p.id}" ${p.id === provider ? 'selected' : ''}>${esc(p.title)}</option>`).join('')}</select></label>
        <label class="field"><span>IMAP-сервер</span><input id="mailc-host" maxlength="200" spellcheck="false" autocomplete="off"
          value="${esc(host)}" placeholder="imap.example.ru"></label>
        <label class="field"><span>Защита</span><select id="mailc-security">
          <option value="ssl" ${security === 'ssl' ? 'selected' : ''}>SSL/TLS, порт 993</option>
          <option value="starttls" ${security === 'starttls' ? 'selected' : ''}>STARTTLS, порт 143</option></select></label>
      </div>
      <p class="form-hint mail-how" id="mailc-how">${esc(meta.how)}</p>
      <div class="dreq-grid">
        <label class="field"><span>Логин (адрес ящика)</span><input id="mailc-user" maxlength="200" spellcheck="false" autocomplete="off"
          value="${esc(d.username || '')}" placeholder="docs@company.ru"></label>
        <label class="field"><span>Пароль приложения</span><input id="mailc-pass" type="password" maxlength="500" autocomplete="new-password"
          placeholder="${d.password_set ? 'задан — пусто, не менять' : 'не основной пароль ящика'}"></label>
        <label class="field"><span>Папка</span><input id="mailc-folder" maxlength="200" spellcheck="false" value="${esc(d.folder || 'INBOX')}"></label>
        <label class="field"><span>Письма с даты</span><input id="mailc-start" type="date" value="${esc(d.start_date || '')}"></label>
      </div>
      <label class="field"><span>Принимать только от (адреса или @домены через запятую; пусто — от всех)</span>
        <input id="mailc-senders" maxlength="5000" spellcheck="false" value="${esc(d.senders || '')}" placeholder="@supplier.ru, buh@partner.ru"></label>
      <p class="form-hint">Пароль хранится зашифрованным и никому не показывается; вход — только по защищённому соединению.</p>
    </details>
    <div class="dreq-tools">
      <button type="button" class="btn btn-sm btn-primary" id="mailc-save">${d.configured ? 'Сохранить и проверить' : 'Подключить и проверить'}</button>
      ${active ? '<button type="button" class="btn btn-sm" id="mailc-sync">⬇ Загрузить сейчас</button>' : ''}
      ${d.configured ? '<button type="button" class="btn btn-sm btn-bad" id="mailc-del">Отключить</button>' : ''}
    </div>
    <div id="mailc-msg" class="form-hint" aria-live="polite"></div>`;
}

/** Тело PUT /api/v1/primary/mailbox из панели; ошибка ввода — { error }. */
export function mailBody(get, companyId) {
  const host = get('mailc-host').value.trim().toLowerCase();
  const username = get('mailc-user').value.trim();
  if (!host) return { error: 'Укажите IMAP-сервер, например imap.yandex.ru' };
  if (!username) return { error: 'Укажите логин — обычно это адрес ящика' };
  const security = get('mailc-security').value === 'starttls' ? 'starttls' : 'ssl';
  const body = { host, port: security === 'ssl' ? 993 : 143, security, username, password: get('mailc-pass').value,
    folder: get('mailc-folder').value.trim() || 'INBOX', senders: get('mailc-senders').value.trim(),
    start_date: get('mailc-start').value || null };
  if (companyId) body.company_id = companyId;
  return body;
}

export async function mailConnectPanel(el, companyId, opts = {}) {
  if (!el) return;
  const q = companyId ? `?company_id=${encodeURIComponent(companyId)}` : '';
  const scope = companyId ? { company_id: companyId } : {};
  let data = null;

  function render() {
    el.innerHTML = mailPanelHtml(data);
    const get = (id) => el.querySelector(`#${id}`);
    const say = (text, bad = false) => {
      const m = get('mailc-msg');
      if (!m) return;
      m.textContent = text;
      m.classList.toggle('form-error', bad);
    };
    get('mailc-provider').onchange = (e) => {
      const p = MAIL_PROVIDERS.find((x) => x.id === e.target.value) || MAIL_PROVIDERS[0];
      if (p.host) get('mailc-host').value = p.host;
      get('mailc-security').value = p.security;
      get('mailc-how').textContent = p.how;
    };
    get('mailc-save').onclick = async () => {
      const body = mailBody(get, companyId);
      if (body.error) { say(body.error, true); return; }
      const btn = get('mailc-save');
      btn.disabled = true;
      try {
        data = await api.put('/api/v1/primary/mailbox', body);
        say('Сохранено — входим в ящик…');
        const res = await api.post('/api/v1/primary/mailbox/check', scope);
        data = res.mailbox;
        render();
        toast(`Ящик подключён: писем в папке — ${Number(res.result && res.result.messages_in_folder) || 0}. Документы придут в течение 15 минут`, 'ok', '📬');
      } catch (e) {
        try { data = await api.get(`/api/v1/primary/mailbox${q}`); render(); } catch { /* панель покажет прежнее */ }
        const m = el.querySelector('#mailc-msg');
        if (m) { m.textContent = e.message; m.classList.add('form-error'); }
      }
    };
    const sync = get('mailc-sync');
    if (sync) {
      sync.onclick = async () => {
        sync.disabled = true;
        say('Забираем письма…');
        try {
          const res = await api.post('/api/v1/primary/mailbox/sync', scope);
          data = res.mailbox;
          render();
          toast(mailResultSummary(res.result), 'ok', '📬 Почта');
          if (typeof opts.onSynced === 'function') opts.onSynced(res.result);
        } catch (e) { sync.disabled = false; say(e.message, true); }
      };
    }
    const del = get('mailc-del');
    if (del) {
      del.onclick = async () => {
        if (!window.confirm('Отключить ящик? Адрес и пароль будут удалены; пришедшие черновики останутся на сверке.')) return;
        try {
          await api.del(`/api/v1/primary/mailbox${q}`);
          toast('Ящик отключён', 'ok', '📬');
          load();
        } catch (e) { say(e.message, true); }
      };
    }
  }

  async function load() {
    el.innerHTML = '<div class="skeleton bank-conn-skeleton"></div>';
    try {
      data = await api.get(`/api/v1/primary/mailbox${q}`);
      if (el.isConnected) render();
    } catch (e) {
      if (el.isConnected) el.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }
  await load();
}

function mailDay(iso) {
  const m = String(iso || '').match(/^(\d{4})-(\d{2})-(\d{2})/);
  return m ? `${m[3]}.${m[2]}.${m[1]}` : '';
}

/** Карточка «Из почты»: черновики на сверку и последние вложения. */
// v1.70.105 (T-64): фото документов от сотрудников приходят через ботов — канал виден в карточке и при сверке
export const CHANNEL_MARK = { telegram: '✈️ Telegram', max: '💬 MAX' };
export const CHANNEL_FROM = { mail: '📬 Из почты', telegram: '✈️ Из Telegram', max: '💬 Из MAX' };

export function mailInboxHtml(data, opts = {}) {
  const drafts = Array.isArray(data && data.drafts) ? data.drafts : [];
  const recent = Array.isArray(data && data.recent) ? data.recent : [];
  const week = (data && data.week) || {};
  const company = (i) => (opts.isAdmin && i.company ? `<div class="form-hint">${esc(i.company)}</div>` : '');
  const mark = (i) => (CHANNEL_MARK[i.channel] ? `${CHANNEL_MARK[i.channel]} · ` : '');
  const from = (i) => `${mark(i)}${esc(i.sender || 'без адреса')}${i.subject ? ` · «${esc(i.subject)}»` : ''}`;
  const draftRows = drafts.map((i) => {
    const d = i.draft || {};
    const what = i.result === 'queued' ? 'распознаётся…'
      : [d.number ? `№ ${esc(d.number)}` : '', d.date ? `от ${esc(mailDay(d.date))}` : '', d.seller ? esc(d.seller) : '']
        .filter(Boolean).join(' ') || 'реквизиты не распознаны — заполните по файлу';
    return `<div class="mail-draft"><div class="mail-draft-main"><b>${esc(i.file_name)}</b>
        <div class="form-hint">${what}</div><div class="form-hint">${from(i)} · ${esc(mailWhen(i.received_at || i.created_at))}</div>${company(i)}</div>
      <div class="mail-draft-tools">${i.result === 'draft'
    ? `<button type="button" class="btn btn-sm btn-primary mail-review" data-id="${esc(i.id)}">Сверить</button>` : ''}
        <button type="button" class="btn btn-sm mail-dismiss" data-id="${esc(i.id)}">Не нужен</button></div></div>`;
  }).join('');
  const recentRows = recent.slice(0, 10).map((i) => `<tr><td>${esc(mailWhen(i.received_at || i.created_at))}</td>
      <td>${mark(i)}${esc(i.sender || '—')}</td><td>${esc(i.file_name)}</td>
      <td title="${esc(i.message || '')}">${esc(i.result_title || i.result)}</td></tr>`).join('');
  const weekText = Object.keys(week).length
    ? `За неделю: в реестр — ${Number(week.created || 0) + Number(week.confirmed || 0)}, дублей — ${Number(week.duplicate || 0)}, `
      + `не принято — ${Number(week.rejected || 0)}, не УПД — ${Number(week.skipped || 0)}.`
    : 'Из почты и от сотрудников через ботов ещё ничего не приходило.';
  return `<div class="card-title mail-title">📥 Входящие: почта и боты <span class="spacer"></span>
      <button type="button" class="btn btn-sm" id="mail-setup">⚙ Почтовый ящик</button>
      <button type="button" class="btn btn-sm" id="mail-refresh">Обновить</button></div>
    <p class="form-hint">${esc(weekText)}</p>
    ${drafts.length ? `<h4>Ждут сверки: ${drafts.length}</h4>${draftRows}`
    : '<p class="form-hint">Черновиков на сверку нет. XML и ZIP из писем попадают в реестр сами, фото документов '
      + 'от сотрудников из Telegram и MAX — сюда, на сверку.</p>'}
    ${recentRows ? `<details><summary>Последние вложения</summary><div class="table-wrap"><table class="data">
      <thead><tr><th>Когда</th><th>От кого</th><th>Файл</th><th>Итог</th></tr></thead><tbody>${recentRows}</tbody></table></div></details>` : ''}`;
}
