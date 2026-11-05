/* v1.70.98 (T-45): экран «Банк: выписка» — бухгалтер загружает выписку в формате обмена 1С, программа
   находит списания без документов (оплаты поставщикам без УПД, покупки по картам без чека) и чеки, которых
   нет в выписке: сотрудник платил своими — ему нужно вернуть деньги. Платежи без документов сами попадают
   в «Что нужно от клиента»; покупка по карте сотрудника — запрос ему лично. Пришёл документ — запрос
   закрывается сам. Все имена верхнего уровня — с приставкой bank (общая область модулей в сборке).
   v1.70.99 (ADR-29): панель «Банк по API» — Точка по OAuth 2.0 или JWT-ключу; та же панель в карточке
   компании у администратора. Секреты в браузер не приходят: только «задан» и последние 4 цифры счетов.
   v1.70.102 (ADR-32): банков у компании несколько — Т-Банк и Модульбанк (ключ API), Сбербанк (токены и
   сертификат клиента), Альфа-Банк (ключ API, сертификат — если выдан); список банков компании над настройками. */
import { api } from './api.js';
import { esc, fmtSum, openModal, toast } from './ui.js';

export const BANK_STATUSES = {
  open: ['chip unknown', 'Нет документов'], matched: ['chip verified', 'Документ найден'], ignored: ['chip failed', 'Не нужны'],
};
export const BANK_KINDS = { card: '💳 покупка по карте', transfer: '🏢 оплата поставщику' };
export const BANK_SKIPPED = {
  tax: 'налоги и взносы', salary: 'зарплата', fee: 'комиссии банка', own: 'свои счета', advance: 'под отчёт',
  income: 'поступления', loan: 'кредиты и займы',
};
export const BANK_REFUND = {
  pending: ['chip unknown', 'К возмещению'], paid: ['chip verified', 'Возмещено'], declined: ['chip failed', 'Не возмещать'],
};
const BANK_TABS = [['open', 'Без документов'], ['matched', 'Документ найден'], ['ignored', 'Не нужны'],
  ['refund', 'К возмещению'], ['statements', 'Выписки']];
export const BANK_CONN_STATUS = {
  none: ['chip unknown', 'не подключён'], new: ['chip unknown', 'не подключён'],
  awaiting: ['chip unknown', 'ждём подтверждения в банке'], active: ['chip verified', 'подключён'],
  reauth: ['chip failed', 'нужно подключить заново'], error: ['chip failed', 'ошибка подключения'],
};
export const BANK_SOURCES = { file: 'файл', tochka: 'Точка, API', tbank: 'Т-Банк, API', sber: 'Сбербанк, API',
  alfa: 'Альфа-Банк, API', modul: 'Модульбанк, API' };

export function bankDate(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${m[3]}.${m[2]}.${m[1]}` : '—';
}

function bankChip(map, status) {
  const [cls, label] = map[status] || ['chip unknown', status || '—'];
  return `<span class="${cls}">${esc(label)}</span>`;
}

/** Итог загрузки одной строкой: сколько операций, чему нужны документы, что нашлось. */
export function bankUploadSummary(res) {
  if (!res) return '';
  const skipped = Object.entries(res.kinds || {}).filter(([k, n]) => BANK_SKIPPED[k] && n)
    .map(([k, n]) => `${BANK_SKIPPED[k]} ${n}`);
  const parts = [`операций: ${res.operations}`, `нужны документы: ${res.saved}`, `документ найден: ${res.matched}`,
    `без документов: ${res.open}`];
  if (res.duplicates) parts.push(`уже были в прошлых выписках: ${res.duplicates}`);
  if (skipped.length) parts.push(`не требуют документов: ${skipped.join(', ')}`);
  if (res.requests) parts.push(`в «Что нужно от клиента» добавлено: ${res.requests}`);
  return parts.join(' · ');
}

export function bankOpsHtml(items, status) {
  const list = Array.isArray(items) ? items : [];
  if (!list.length) {
    const empty = { open: 'Платежей без документов нет. Загрузите выписку — программа проверит списания.',
      matched: 'Сопоставленных платежей пока нет.', ignored: 'Отмеченных «документы не нужны» нет.' };
    return `<tr><td colspan="6">${esc(empty[status] || 'Операций нет.')}</td></tr>`;
  }
  return list.map((op) => {
    const act = (action, label) => `<button type="button" class="btn btn-sm bank-act" data-id="${esc(op.id)}" data-action="${action}">${label}</button>`;
    const actions = op.status === 'open' ? act('ignore', 'Документы не нужны')
      : act('open', op.status === 'matched' ? '↺ Отменить сопоставление' : '↺ Вернуть в работу');
    const what = op.kind === 'card'
      ? `${BANK_KINDS.card}${op.card_tail ? ` *${esc(op.card_tail)}` : ''}${op.card_owner ? `<div class="form-hint">карта: ${esc(op.card_owner)}</div>` : ''}`
      : `${BANK_KINDS.transfer}${op.number ? `<div class="form-hint">платёж № ${esc(op.number)}</div>` : ''}`;
    const who = `${esc(op.counterparty_name || '—')}${op.counterparty_inn ? `<div class="form-hint">ИНН ${esc(op.counterparty_inn)}</div>` : ''}`;
    const note = op.match_note ? `<div class="form-hint">${esc(op.match_note)}${op.decided_by ? ` — ${esc(op.decided_by)}` : ''}</div>` : '';
    return `<tr data-id="${esc(op.id)}"><td>${esc(bankDate(op.date))}</td><td>${what}</td><td>${who}</td>
      <td class="bank-purpose">${esc(op.purpose || '—')}</td><td class="num dreq-sum">${fmtSum(op.amount)}</td>
      <td>${bankChip(BANK_STATUSES, op.status)}${note}<div class="dreq-actions">${actions}</div></td></tr>`;
  }).join('');
}

export function bankRefundHtml(data) {
  const groups = data && Array.isArray(data.employees) ? data.employees : [];
  if (!data || !data.covered) return '<p class="form-hint">Загрузите выписку — программа покажет чеки, которых в ней нет: их сотрудник оплатил сам.</p>';
  if (!groups.length) return '<p class="form-hint">Все чеки за период выписки нашлись в ней — возмещать нечего.</p>';
  return groups.map((g) => {
    const rows = g.items.map((it) => {
      const act = (status, label) => `<button type="button" class="btn btn-sm bank-refund" data-id="${esc(it.receipt_id)}" data-status="${status}">${label}</button>`;
      const actions = it.status === 'pending' ? `${act('paid', '✓ Возмещено')} ${act('declined', 'Не возмещать')}` : act('pending', '↺ Вернуть');
      return `<tr><td>${esc(bankDate(it.date))}</td><td>${esc(it.merchant || '—')}${it.payment ? `<div class="form-hint">${esc(it.payment)}</div>` : ''}</td>
        <td class="num dreq-sum">${fmtSum(it.amount)}${it.amount !== it.total_sum ? `<div class="form-hint">чек ${fmtSum(it.total_sum)}</div>` : ''}</td>
        <td>${bankChip(BANK_REFUND, it.status)}${it.note ? `<div class="form-hint">${esc(it.note)}</div>` : ''}<div class="dreq-actions">${actions}</div></td></tr>`;
    }).join('');
    return `<div class="bank-group"><h4>${esc(g.name)} <span class="form-hint">к возмещению: <b>${fmtSum(g.pending_total)}</b>${g.paid_total ? ` · возмещено: ${fmtSum(g.paid_total)}` : ''}</span></h4>
      <div class="table-wrap"><table class="data"><thead><tr><th>Дата</th><th>Продавец</th><th class="num">К учёту</th><th>Решение</th></tr></thead>
      <tbody>${rows}</tbody></table></div></div>`;
  }).join('');
}

/** Откуда выписка: файл (кто загрузил) или API банка (по кнопке — кто нажал, иначе сама по расписанию). */
export function bankStatementSource(s) {
  const src = s && s.source;
  if (src && src !== 'file' && BANK_SOURCES[src]) return s.uploaded_by ? `${BANK_SOURCES[src]} · ${s.uploaded_by}` : `${BANK_SOURCES[src]}, сама`;
  return (s && s.uploaded_by) || BANK_SOURCES.file;
}

export function bankStatementsHtml(list) {
  const items = Array.isArray(list) ? list : [];
  if (!items.length) return '<p class="form-hint">Выписок пока нет.</p>';
  const rows = items.map((s) => `<tr><td>${esc(bankDate(s.date_from))} — ${esc(bankDate(s.date_to))}</td>
    <td>${s.account_tail ? `счёт …${esc(s.account_tail)}` : '—'}</td>
    <td class="num">${esc(s.operations)}</td><td class="num">${esc(s.saved)}</td>
    <td>${esc(bankStatementSource(s))}<div class="form-hint">${esc(bankDate(s.created_at))}</div></td>
    <td><button type="button" class="btn btn-sm bank-del" data-id="${esc(s.id)}">Удалить</button></td></tr>`).join('');
  return `<div class="table-wrap"><table class="data"><thead><tr><th>Период</th><th>Счёт</th><th class="num">Операций</th>
    <th class="num">Нужны документы</th><th>Откуда</th><th></th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

export function bankSettingsHtml(settings, staff) {
  const s = settings || {};
  const people = Array.isArray(staff) ? staff : [];
  const cards = (Array.isArray(s.cards) ? s.cards : []).concat([{ tail: '', user_id: '' }, { tail: '', user_id: '' }]);
  const options = (sel) => `<option value="">— не указан —</option>${people.map((p) => `<option value="${esc(p.id)}" ${p.id === sel ? 'selected' : ''}>${esc(p.name)}</option>`).join('')}`;
  const rows = cards.map((c, i) => `<div class="dreq-grid">
      <label class="field"><span>Карта — последние 4 цифры</span><input id="bank-s-tail-${i}" maxlength="4" inputmode="numeric" value="${esc(c.tail || '')}"></label>
      <label class="field"><span>Сотрудник</span><select id="bank-s-user-${i}">${options(c.user_id)}</select></label></div>`).join('');
  return `<h3 class="modal-title">⚙ Карты и срок хранения</h3>
    <p class="form-hint">Укажите корпоративные карты сотрудников: покупка по карте без чека станет запросом лично держателю карты.</p>
    ${rows}
    <label class="field"><span>Хранить операции выписок, дней (от 30 до 3650; по умолчанию 1095 — три года)</span>
      <input id="bank-s-days" type="number" min="30" max="3650" value="${esc(s.retention_days || 1095)}"></label>
    <div id="bank-s-error" class="form-error dreq-error" aria-live="polite"></div>
    <div class="modal-actions"><button type="button" class="btn btn-primary" id="bank-s-save">Сохранить</button></div>
    <input type="hidden" id="bank-s-count" value="${cards.length}">`;
}

/** Тело сохранения настроек; ошибка ввода — { error }. */
export function bankSettingsBody(get, companyId) {
  const count = Number(get('bank-s-count').value) || 0;
  const cards = [];
  for (let i = 0; i < count; i += 1) {
    const tail = (get(`bank-s-tail-${i}`).value || '').replace(/\D/g, '');
    const user = get(`bank-s-user-${i}`).value || '';
    if (!tail && !user) continue;
    if (tail.length !== 4) return { error: 'Карта — ровно 4 последние цифры номера' };
    cards.push({ tail, user_id: user });
  }
  const days = Number(get('bank-s-days').value);
  if (!Number.isInteger(days) || days < 30 || days > 3650) return { error: 'Срок хранения — от 30 до 3650 дней' };
  return { ...(companyId ? { company_id: companyId } : {}), retention_days: days, cards };
}

/** Момент из ответа сервера (UTC без зоны) — по местному времени: «04.11.2026 18:05». */
export function bankWhen(iso) {
  if (!iso) return '—';
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return '—';
  const two = (n) => String(n).padStart(2, '0');
  return `${two(d.getDate())}.${two(d.getMonth() + 1)}.${d.getFullYear()} ${two(d.getHours())}:${two(d.getMinutes())}`;
}

/** Итог загрузки из банка одной строкой. */
export function bankSyncSummary(res) {
  if (!res) return '';
  if (res.ok === false) return res.error || 'Загрузка не удалась';
  const parts = [`операций: ${res.operations || 0}`, `нужны документы: ${res.saved || 0}`, `документ найден: ${res.matched || 0}`,
    `без документов: ${res.open || 0}`];
  if (res.duplicates) parts.push(`уже были в загруженных файлах: ${res.duplicates}`);
  if (res.requests) parts.push(`в «Что нужно от клиента»: ${res.requests}`);
  if (res.pending) parts.push('банк ещё готовит выписку — заберём через несколько минут');
  return parts.join(' · ');
}

export const BANK_METHOD_TITLES = { oauth: 'OAuth 2.0', jwt: 'JWT-ключ', token: 'Ключ API', refresh: 'OAuth 2.0 — токены из кабинета' };

/** Банк из реестра ответа сервера; старый ответ (только id и title) — как у Точки в 1.70.99. */
export function bankMeta(d, id) {
  const list = Array.isArray(d && d.banks) && d.banks.length ? d.banks : [{ id: 'tochka', title: 'Точка Банк' }];
  const found = list.find((b) => b.id === id) || list[0];
  return { methods: found.id === 'tochka' ? ['oauth', 'jwt'] : ['token'], cert: '', how: '', docs: '', ...found };
}

/** v1.70.102 (ADR-32): банки компании одной строкой каждый — состояние, хвосты счетов, по какой день выписка. */
export function bankListHtml(list, current) {
  const items = Array.isArray(list) ? list : [];
  if (!items.length) return '';
  const rows = items.map((x) => {
    const [cls, label] = BANK_CONN_STATUS[x.status] || BANK_CONN_STATUS.none;
    const tails = (Array.isArray(x.accounts) ? x.accounts : []).map((t) => `*${esc(t)}`).join(' ');
    const when = x.synced_until ? `выписка по ${bankDate(x.synced_until)}` : '';
    const facts = [tails, esc(when)].filter(Boolean).join(' · ');
    return `<li class="bank-conn-row${x.bank === current ? ' is-current' : ''}"><span class="bank-conn-name">${esc(x.bank_title || x.bank)}</span>
      <span class="${cls}">${esc(label)}</span>${facts ? `<span class="form-hint">${facts}</span>` : ''}
      ${x.bank === current ? '<span class="form-hint">открыт ниже</span>'
    : `<button type="button" class="btn btn-sm bank-conn-open" data-bank="${esc(x.bank)}">Открыть</button>`}</li>`;
  }).join('');
  return `<ul class="bank-conn-list" aria-label="Банки компании">${rows}</ul>`;
}

/** Банки первой десятки без открытого API выписок: честно — выписка файлом в формате 1С. */
export function bankFileBanksHtml(list) {
  const items = Array.isArray(list) ? list : [];
  if (!items.length) return '';
  return `<details class="bank-conn-files"><summary>Другие банки — выписка файлом</summary>
    <p class="form-hint">У этих банков нет открытого API выписок для своей компании (или оно по отдельному договору).
      Выгрузите выписку в формате 1С из интернет-банка и загрузите её на экране «Банк: выписка» — проверка та же.</p>
    <ul class="bank-conn-file-list">${items.map((b) => `<li><b>${esc(b.title)}</b> — ${esc(b.note)}</li>`).join('')}</ul></details>`;
}

/** Панель «Банк по API»: банки компании, состояние, счета, настройки и кнопки. c — ответ GET /api/v1/bank/connection. */
export function bankConnHtml(c) {
  const d = c || {};
  const status = BANK_CONN_STATUS[d.status] ? d.status : 'none';
  const active = status === 'active';
  const bankId = d.bank || 'tochka';
  const meta = bankMeta(d, bankId);
  const tochka = bankId === 'tochka';
  const method = meta.methods.includes(d.method) ? d.method : meta.methods[0];
  const oauth = method === 'oauth';
  const banks = (Array.isArray(d.banks) && d.banks.length ? d.banks : [{ id: 'tochka', title: 'Точка Банк' }])
    .map((b) => `<option value="${esc(b.id)}" ${b.id === bankId ? 'selected' : ''}>${esc(b.title)}</option>`).join('');
  const methods = meta.methods
    .map((m) => `<option value="${esc(m)}" ${m === method ? 'selected' : ''}>${esc(BANK_METHOD_TITLES[m] || m)}</option>`).join('');
  const accounts = (Array.isArray(d.accounts) ? d.accounts : [])
    .map((a) => `<span class="${a.loaded ? 'chip verified' : 'chip unknown'}">*${esc(a.tail)} ${esc(a.currency)}${a.loaded ? '' : ' — не загружается'}</span>`)
    .join(' ');
  const res = d.last_result || {};
  const facts = [d.synced_until ? `выписка загружена по ${bankDate(d.synced_until)}` : '',
    d.last_sync_at ? `последняя проверка ${bankWhen(d.last_sync_at)}` : '',
    d.consent_expires_at && oauth ? `доступ до ${bankDate(d.consent_expires_at)}` : '',
    d.cert && d.cert.until ? `сертификат до ${bankDate(d.cert.until)}` : ''].filter(Boolean).join(' · ');
  const [cls, label] = BANK_CONN_STATUS[status];
  const certNeed = meta.cert === 'required' ? ' — обязательно' : ' — если банк его выдал';
  const cert = meta.cert ? `<div id="bankc-cert-box" class="bank-conn-cert">
        <label class="field"><span>Сертификат клиента и закрытый ключ (PEM)${certNeed}</span><textarea id="bankc-cert" rows="3"
          spellcheck="false" maxlength="70000" placeholder="${d.cert_set ? `задан${d.cert && d.cert.until ? `, до ${esc(bankDate(d.cert.until))}` : ''} — вставьте новый, чтобы заменить`
    : '-----BEGIN CERTIFICATE----- … -----BEGIN PRIVATE KEY----- …'}"></textarea></label>
        <div class="dreq-grid">
          <label class="field"><span>или файл .p12, .pfx, .pem</span><input id="bankc-cert-file" type="file" accept=".p12,.pfx,.pem,.crt,.cer,.key"></label>
          <label class="field"><span>Пароль ключа (если есть)</span><input id="bankc-cert-pass" type="password" autocomplete="new-password" maxlength="200"></label>
        </div>
        <p class="form-hint">Сертификат хранится зашифрованным и нужен только для защищённого канала к банку (взаимный TLS).</p>
      </div>` : '';
  return `<div class="bank-conn-head"><div class="card-title">🏦 Банк по API</div><span class="${cls}">${esc(label)}</span></div>
    ${bankListHtml(d.connections, bankId)}
    <p class="form-hint">Выписка приходит сама: раз в 6 часов программа забирает операции из банка и проверяет их так же,
      как загруженный файл, — платежи без документов попадут в «Что нужно от клиента». Доступ — только на чтение счетов и выписок.
      Банков у компании может быть несколько: Точка, Т-Банк, Сбербанк, Альфа-Банк, Модульбанк.</p>
    ${active ? `<div class="form-hint">${esc(meta.title)} · счета: ${accounts || '—'}</div>${facts ? `<div class="form-hint">${esc(facts)}</div>` : ''}` : ''}
    ${active && res.ok ? `<div class="form-hint">Последняя загрузка: ${esc(bankSyncSummary(res))}</div>` : ''}
    ${d.pending ? '<div class="form-hint">Банк готовит выписку — заберём её через несколько минут.</div>' : ''}
    ${d.awaiting ? '<div class="form-hint">Ждём подтверждения в интернет-банке Точки (ссылка действует 15 минут).</div>' : ''}
    ${d.last_error ? `<div class="form-error">${esc(d.last_error)}</div>` : ''}
    <details ${active ? '' : 'open'}><summary>Настройки подключения — ${esc(meta.title)}</summary>
      <div class="dreq-grid">
        <label class="field"><span>Банк</span><select id="bankc-bank">${banks}</select></label>
        <label class="field"><span>Способ доступа</span><select id="bankc-method">${methods}</select></label>
        <label class="field"><span>Загружать операции с</span><input id="bankc-start" type="date" value="${esc(d.start_date || '')}"></label>
      </div>
      ${meta.how && !tochka ? `<p class="form-hint bank-conn-how">Где взять ключи: ${esc(meta.how)}</p>` : ''}
      <div id="bankc-oauth"${oauth || method === 'refresh' ? '' : ' class="hidden"'}>
        <div class="dreq-grid">
          <label class="field"><span>client_id</span><input id="bankc-client" autocomplete="off" spellcheck="false" maxlength="200" value="${esc(d.client_id || '')}"></label>
          <label class="field"><span>client_secret</span><input id="bankc-secret" type="password" autocomplete="new-password" maxlength="500"
            placeholder="${d.secret_set ? 'задан — пусто, не менять' : 'выдаётся с client_id'}"></label>
        </div>
        <p class="form-hint${tochka ? '' : ' hidden'}">Рекомендуемый способ: доступ подтверждает сам клиент в банке, его срок виден, отозвать можно в любой момент.
          Интернет-банк Точки → «Интеграции и API» → «Подключить OAuth»: банк выдаст client_id и client_secret.
          Адрес возврата для приложения: <code class="bank-conn-uri">${esc(d.redirect_uri || '')}</code>${d.server_app
    ? '. Поля можно оставить пустыми — подключение пойдёт через приложение сервера.' : ''}</p>
      </div>
      <div id="bankc-jwt-box"${method === 'jwt' ? '' : ' class="hidden"'}>
        <label class="field"><span>JWT-ключ</span><textarea id="bankc-jwt" rows="3" maxlength="8000" spellcheck="false"
          placeholder="${d.jwt_set ? 'ключ задан — вставьте новый, чтобы заменить' : 'Интернет-банк → «Интеграции и API» → «Создать JWT-ключ»'}"></textarea></label>
      </div>
      <div id="bankc-token-box"${method === 'token' ? '' : ' class="hidden"'}>
        <label class="field"><span>Ключ API</span><textarea id="bankc-token" rows="2" maxlength="4000" spellcheck="false"
          placeholder="${d.token_set ? 'ключ задан — вставьте новый, чтобы заменить' : 'вставьте ключ из интернет-банка'}"></textarea></label>
      </div>
      <div id="bankc-refresh-box"${method === 'refresh' ? '' : ' class="hidden"'}>
        <label class="field"><span>refresh_token</span><input id="bankc-refresh" type="password" autocomplete="new-password" maxlength="4000"
          placeholder="${d.refresh_set ? 'задан — пусто, не менять' : 'из личного кабинета банка'}"></label>
      </div>
      ${cert}
    </details>
    ${bankFileBanksHtml(d.file_banks)}
    <div class="dreq-tools">
      <button type="button" class="btn btn-sm" id="bankc-save">Сохранить</button>
      <button type="button" class="btn btn-sm btn-primary${oauth ? '' : ' hidden'}" id="bankc-auth">🔐 ${active || status === 'reauth' ? 'Подключить заново' : 'Подключить через Точку'}</button>
      <button type="button" class="btn btn-sm btn-primary${oauth ? ' hidden' : ''}" id="bankc-jwtcheck">Проверить ключ и подключить</button>
      ${active ? '<button type="button" class="btn btn-sm" id="bankc-sync">⬇ Загрузить сейчас</button>' : ''}
      ${status !== 'none' ? '<button type="button" class="btn btn-sm btn-bad" id="bankc-del">Отключить</button>' : ''}
    </div>
    <div id="bankc-msg" class="form-hint" aria-live="polite"></div>`;
}

/** Тело PUT /api/v1/bank/connection из панели; ошибка ввода — { error }. */
export function bankConnBody(get, companyId) {
  const bank = get('bankc-bank').value || 'tochka';
  let method = get('bankc-method').value || 'oauth';
  if (bank === 'tochka') method = method === 'jwt' ? 'jwt' : 'oauth';
  const body = { bank, method };
  if (companyId) body.company_id = companyId;
  const start = get('bankc-start').value;
  if (start) body.start_date = start;
  if (method === 'oauth' || method === 'refresh') {
    body.client_id = get('bankc-client').value.trim();
    body.client_secret = get('bankc-secret').value.trim();
    if (/\s/.test(body.client_id) || /\s/.test(body.client_secret)) return { error: 'client_id и client_secret — без пробелов' };
  }
  if (method === 'jwt') {
    body.jwt = get('bankc-jwt').value.replace(/\s+/g, '');
    if (body.jwt && body.jwt.split('.').length !== 3) return { error: 'JWT-ключ — три части через точку, как его выдаёт интернет-банк' };
  }
  if (method === 'token') {
    body.token = get('bankc-token').value.replace(/\s+/g, '');
    if (body.token && body.token.length < 8) return { error: 'Ключ API слишком короткий — скопируйте его из интернет-банка целиком' };
  }
  if (method === 'refresh') body.refresh_token = get('bankc-refresh').value.replace(/\s+/g, '');
  const cert = get('bankc-cert');
  if (cert) {
    const pem = cert.value.trim();
    const file = get('bankc-cert-file');
    const p12 = (file && file.dataset && file.dataset.p12) || '';
    if (pem && !pem.includes('-----BEGIN CERTIFICATE-----')) return { error: 'Сертификат — блок «-----BEGIN CERTIFICATE-----» и закрытый ключ к нему' };
    if (pem) body.cert = pem;
    if (p12) body.cert_p12 = p12;
    const pass = get('bankc-cert-pass');
    if ((pem || p12) && pass && pass.value) body.cert_password = pass.value;
  }
  return body;
}

/** Файл сертификата: .p12/.pfx — в base64, PEM — текстом. */
export function bankReadFile(file) {
  return new Promise((resolve, reject) => {
    const reader = new FileReader();
    const binary = /\.(p12|pfx)$/i.test(file.name || '');
    reader.onload = () => resolve({ binary, text: binary ? String(reader.result).split(',')[1] || '' : String(reader.result) });
    reader.onerror = () => reject(new Error('Файл сертификата не читается'));
    if (binary) reader.readAsDataURL(file); else reader.readAsText(file);
  });
}

/** Панель подключения банка в элементе el: экран «Банк: выписка» и карточка компании.
    v1.70.102: банков несколько — список банков компании, выбор банка перерисовывает настройки под него. */
export async function bankConnectPanel(el, companyId, opts = {}) {
  if (!el) return;
  const url = (bank, forDelete = false) => {
    const q = new URLSearchParams();
    if (companyId) q.set('company_id', companyId);
    if (bank && !(forDelete && bank === 'tochka')) q.set('bank', bank);
    const text = q.toString();
    return `/api/v1/bank/connection${text ? `?${text}` : ''}`;
  };
  const scope = (bank) => ({ ...(companyId ? { company_id: companyId } : {}), ...(bank && bank !== 'tochka' ? { bank } : {}) });
  let data = null;

  function render() {
    el.innerHTML = bankConnHtml(data);
    const get = (id) => el.querySelector(`#${id}`);
    const bank = (data && data.bank) || 'tochka';
    const title = bankMeta(data, bank).title;
    const say = (text, bad = false) => {
      const m = get('bankc-msg');
      if (!m) return;
      m.textContent = text;
      m.classList.toggle('form-error', bad);
    };
    const save = async () => {
      const body = bankConnBody(get, companyId);
      if (body.error) { say(body.error, true); return null; }
      return api.put('/api/v1/bank/connection', body);
    };
    // видимость — классом .hidden (display: none !important): атрибут hidden проигрывает display кнопок .btn
    get('bankc-method').onchange = (e) => {
      const method = e.target.value;
      const jwt = method !== 'oauth';               // всё, кроме входа через Точку, — «проверить ключ»
      get('bankc-oauth').classList.toggle('hidden', !(method === 'oauth' || method === 'refresh'));
      get('bankc-jwt-box').classList.toggle('hidden', method !== 'jwt');
      get('bankc-token-box').classList.toggle('hidden', method !== 'token');
      get('bankc-refresh-box').classList.toggle('hidden', method !== 'refresh');
      get('bankc-auth').classList.toggle('hidden', jwt);
      get('bankc-jwtcheck').classList.toggle('hidden', !jwt);
    };
    get('bankc-bank').onchange = (e) => load(e.target.value);
    el.querySelectorAll('.bank-conn-open').forEach((btn) => { btn.onclick = () => load(btn.dataset.bank); });
    const file = get('bankc-cert-file');
    if (file) {
      file.onchange = async () => {
        const picked = file.files && file.files[0];
        if (!picked) return;
        try {
          const read = await bankReadFile(picked);
          if (read.binary) { file.dataset.p12 = read.text; say(`Файл «${picked.name}» прочитан — укажите пароль и сохраните`); }
          else { get('bankc-cert').value = `${get('bankc-cert').value.trim()}\n${read.text.trim()}`.trim(); say('Сертификат вставлен — сохраните'); }
        } catch (e) { say(e.message, true); }
      };
    }
    get('bankc-save').onclick = async () => {
      try {
        const res = await save();
        if (res) { data = res; render(); toast('Подключение сохранено', 'ok', '🏦'); }
      } catch (e) { say(e.message, true); }
    };
    get('bankc-auth').onclick = async () => {
      try {
        if (!(await save())) return;
        say('Открываем вход в интернет-банк Точки…');
        const { url } = await api.post('/api/v1/bank/connection/authorize', scope(bank));
        window.location.assign(url);
      } catch (e) { say(e.message, true); }
    };
    get('bankc-jwtcheck').onclick = async () => {
      try {
        if (!(await save())) return;
        say('Проверяем ключ в банке…');
        data = await api.post(bank === 'tochka' ? '/api/v1/bank/connection/jwt' : '/api/v1/bank/connection/check', scope(bank));
        render();
        toast(`${title} подключён — выписка загрузится в течение минуты`, 'ok', '🏦');
      } catch (e) { say(e.message, true); }
    };
    const sync = get('bankc-sync');
    if (sync) {
      sync.onclick = async () => {
        sync.disabled = true;
        say('Загружаем выписку из банка…');
        try {
          const res = await api.post('/api/v1/bank/connection/sync', scope(bank));
          data = res.connection;
          render();
          toast(bankSyncSummary(res.result), res.result && res.result.ok ? 'ok' : 'err', `🏦 ${title}`);
          if (res.result && res.result.ok && typeof opts.onSynced === 'function') opts.onSynced(res.result);
        } catch (e) { sync.disabled = false; say(e.message, true); }
      };
    }
    const del = get('bankc-del');
    if (del) {
      del.onclick = async () => {
        if (!window.confirm(`Отключить банк «${title}»? Загруженные выписки останутся, ключи и токены будут удалены. `
          + 'Отозвать доступ можно и в интернет-банке.')) return;
        try {
          await api.del(url(bank, true));
          toast('Банк отключён', 'ok', '🏦');
          load();
        } catch (e) { say(e.message, true); }
      };
    }
  }

  async function load(bank = '') {
    el.innerHTML = '<div class="skeleton bank-conn-skeleton"></div>';
    try {
      data = await api.get(url(bank));
      if (el.isConnected) render();
    } catch (e) {
      if (el.isConnected) el.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }
  await load(opts.bank || '');
}

/** Возврат из Точки (?bank=connected|error): уведомление и чистый адрес. { status, company, reason } или null. */
export function bankReturnNotice(loc = window.location) {
  const q = new URLSearchParams(loc.search || '');
  const status = q.get('bank');
  if (status !== 'connected' && status !== 'error') return null;
  const company = (q.get('company') || '').slice(0, 36);
  const reason = (q.get('reason') || '').slice(0, 240);
  try { window.history.replaceState(null, '', `${loc.pathname}${loc.hash || '#/bank'}`); } catch { /* адрес не меняем */ }
  if (status === 'connected') toast('Точка подключена — выписка загрузится в течение минуты', 'ok', '🏦 Банк');
  else toast(reason || 'Подключение банка не завершено', 'err', '🏦 Банк');
  return { status, company, reason };
}

function bankDownload(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}

export async function viewBank(container, opts = {}) {
  const alive = typeof opts.alive === 'function' ? opts.alive : () => true;
  const companies = (Array.isArray(opts.companies) ? opts.companies : []).filter((c) => c.is_active !== false);
  const filterCompany = opts.companyFilter && opts.companyFilter !== 'all' ? opts.companyFilter : '';
  const state = { company: opts.isAdmin ? filterCompany : '', tab: 'open', data: null };
  let req = 0;
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">🏦 Банк: выписка</div>
      <p class="form-hint">Загрузите выписку в формате обмена с 1С (в интернет-банке — «Выгрузка в 1С», файл kl_to_1c.txt).
        Программа найдёт списания без документов: оплаты поставщикам без УПД, накладной или акта и покупки по картам без чека —
        они сами попадут в «Что нужно от клиента». А чеки, которых нет в выписке, сотрудник оплатил сам — это «к возмещению».
        Хранятся только списания, которым нужны документы; налоги, зарплата и комиссии лишь считаются.</p>
      <div class="dreq-controls">
        ${opts.isAdmin ? `<label class="field"><span>Компания</span><select id="bank-company">
          <option value="">Выберите компанию</option>
          ${companies.map((c) => `<option value="${esc(c.id)}" ${state.company === c.id ? 'selected' : ''}>${esc(c.short_name || c.name)}</option>`).join('')}
        </select></label>` : ''}
      </div>
      <div class="dreq-tools">
        <button type="button" class="btn btn-primary btn-sm" id="bank-upload">⬆ Загрузить выписку</button>
        <input type="file" id="bank-file" accept=".txt,text/plain" hidden>
        <button type="button" class="btn btn-sm" id="bank-rematch">🔄 Сопоставить заново</button>
        <button type="button" class="btn btn-sm" id="bank-settings">⚙ Карты и срок хранения</button>
      </div>
      <div id="bank-result" class="form-hint" aria-live="polite"></div>
    </div>
    <div class="glass card" id="bank-conn"></div>
    <div class="glass card">
      <div class="card-title">Платежи и чеки</div>
      <p class="form-hint" id="bank-kpi" aria-live="polite"></p>
      <div class="segmented bank-tabs" id="bank-tabs">${BANK_TABS.map(([id, label]) => `<button type="button" id="bank-tab-${id}" data-tab="${id}" class="${id === 'open' ? 'active' : ''}">${label}</button>`).join('')}</div>
      <div id="bank-out"><div class="skeleton" style="height:120px"></div></div>
    </div>`;
  const $ = (sel) => container.querySelector(sel);
  const query = (extra = {}) => {
    const q = new URLSearchParams();
    if (state.company) q.set('company_id', state.company);
    Object.entries(extra).forEach(([k, v]) => q.set(k, v));
    return q;
  };

  function kpi(data) {
    const t = data.totals || {};
    const r = data.reimburse || {};
    const open = t.open || { count: 0, amount: 0 };
    $('#bank-kpi').textContent = `без документов: ${open.count} на ${fmtSum(open.amount)} · найдено: ${(t.matched || {}).count || 0}`
      + ` · к возмещению: ${fmtSum(r.pending_total || 0)} (${r.pending_count || 0})`;
  }

  async function showRefund() {
    $('#bank-out').innerHTML = '<div class="skeleton" style="height:80px"></div>';
    try {
      const data = await api.get(`/api/v1/bank/reimburse?${query()}`);
      if (!container.isConnected || state.tab !== 'refund') return;
      const csv = data.covered && data.employees.length
        ? '<div class="dreq-tools"><button type="button" class="btn btn-sm" id="bank-csv">⬇ Таблица для авансового отчёта (CSV)</button></div>' : '';
      $('#bank-out').innerHTML = `<p class="form-hint">Чеки за период выписок, которых в выписках нет: сотрудник оплатил их сам.
        Последние ${esc(data.cover_tail_days)} дня выписки ждут следующую — списание по карте приходит позже покупки.</p>${csv}${bankRefundHtml(data)}`;
      $('#bank-out').querySelectorAll('.bank-refund').forEach((b) => { b.onclick = () => decide(b.dataset.id, b.dataset.status); });
      const btn = $('#bank-csv');
      if (btn) {
        btn.onclick = async () => {
          try {
            const { blob, filename } = await api.download(`/api/v1/bank/reimburse.csv?${query()}`);
            bankDownload(blob, filename);
          } catch (e) { toast(e.message, 'err'); }
        };
      }
    } catch (e) {
      if (container.isConnected) $('#bank-out').innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  function render(data) {
    state.data = data;
    kpi(data);
    BANK_TABS.forEach(([id]) => { const b = $(`#bank-tab-${id}`); if (b) b.classList.toggle('active', id === state.tab); });
    if (state.tab === 'refund') { showRefund(); return; }
    if (state.tab === 'statements') {
      $('#bank-out').innerHTML = bankStatementsHtml(data.statements);
      $('#bank-out').querySelectorAll('.bank-del').forEach((b) => { b.onclick = () => removeStatement(b.dataset.id); });
      return;
    }
    $('#bank-out').innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Дата</th><th>Операция</th><th>Получатель</th>
      <th>Назначение</th><th class="num">Сумма</th><th>Документы</th></tr></thead><tbody>${bankOpsHtml(data.operations, state.tab)}</tbody></table></div>`;
    $('#bank-out').querySelectorAll('.bank-act').forEach((b) => { b.onclick = () => operation(b.dataset.id, b.dataset.action); });
  }

  async function load() {
    if (opts.isAdmin && !state.company) {
      $('#bank-out').innerHTML = '<p class="form-hint">Выберите компанию — у каждой свои счета и выписки.</p>';
      $('#bank-kpi').textContent = '';
      return;
    }
    const current = ++req;
    const status = ['open', 'matched', 'ignored'].includes(state.tab) ? state.tab : 'open';
    try {
      const data = await api.get(`/api/v1/bank?${query({ status })}`);
      if (current !== req || !alive() || !container.isConnected) return;
      render(data);
    } catch (e) {
      if (current === req && container.isConnected) $('#bank-out').innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function upload(file) {
    if (!file) return;
    if (opts.isAdmin && !state.company) { toast('Сначала выберите компанию', 'err'); return; }
    const form = new FormData();
    form.append('file', file);
    if (state.company) form.append('company_id', state.company);
    $('#bank-result').textContent = 'Разбираем выписку…';
    try {
      const res = await api.post('/api/v1/bank/statements', form);
      if (!container.isConnected) return;
      $('#bank-result').textContent = `Выписка ${bankDate(res.statement.date_from)} — ${bankDate(res.statement.date_to)}: ${bankUploadSummary(res)}`;
      toast(res.open ? `Без документов: ${res.open}` : 'Документы найдены для всех платежей', 'ok');
      state.tab = 'open';
      load();
    } catch (e) {
      if (container.isConnected) $('#bank-result').textContent = '';
      toast(e.message, 'err');
    }
  }

  async function operation(id, action) {
    let note = '';
    if (action === 'ignore') {
      note = window.prompt('Почему документы не нужны? Например: возврат займа, депозит (попадёт в журнал)', '') ?? null;
      if (note === null) return;
    }
    try {
      await api.post(`/api/v1/bank/operations/${encodeURIComponent(id)}`, { action, note });
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function decide(id, status) {
    let note = '';
    if (status === 'declined') {
      note = window.prompt('Почему не возмещать? Например: оплачено из подотчётных', '') ?? null;
      if (note === null) return;
    }
    try {
      await api.post(`/api/v1/bank/reimburse/${encodeURIComponent(id)}`, { ...(state.company ? { company_id: state.company } : {}), status, note });
      showRefund();
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function removeStatement(id) {
    if (!window.confirm('Удалить выписку? Её операции и открытые запросы по ним будут сняты (запросы — с пометкой в журнале).')) return;
    try {
      const res = await api.del(`/api/v1/bank/statements/${encodeURIComponent(id)}`);
      toast(`Выписка удалена, операций: ${res.deleted}`, 'ok');
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  async function rematch() {
    if (opts.isAdmin && !state.company) { toast('Сначала выберите компанию', 'err'); return; }
    try {
      const res = await api.post(`/api/v1/bank/match?${query()}`, {});
      toast(res.matched ? `Найдено документов: ${res.matched}` : 'Новых совпадений нет', 'ok');
      load();
    } catch (e) { toast(e.message, 'err'); }
  }

  function settings() {
    const data = state.data;
    if (!data) return;
    const { slot, close } = openModal(bankSettingsHtml(data.settings, data.staff));
    if (!slot) return;
    const get = (id) => slot.querySelector(`#${id}`);
    get('bank-s-save').onclick = async () => {
      const payload = bankSettingsBody(get, state.company);
      if (payload.error) { get('bank-s-error').textContent = payload.error; return; }
      try {
        await api.put('/api/v1/bank/settings', payload);
        toast('Сохранено', 'ok');
        if (typeof close === 'function') close();
        load();
      } catch (e) { get('bank-s-error').textContent = e.message; }
    };
  }

  // v1.70.99 (ADR-29): подключение банка по API — у администратора для выбранной компании
  function connection() {
    const box = $('#bank-conn');
    if (opts.isAdmin && !state.company) { box.classList.add('hidden'); box.innerHTML = ''; return; }
    box.classList.remove('hidden');
    bankConnectPanel(box, state.company, { onSynced: () => { state.tab = 'open'; load(); } });
  }
  const company = $('#bank-company');
  if (company) company.onchange = (e) => { state.company = e.target.value; connection(); load(); };
  const fileInput = $('#bank-file');
  $('#bank-upload').onclick = () => fileInput.click();
  fileInput.onchange = () => { const f = fileInput.files && fileInput.files[0]; fileInput.value = ''; upload(f); };
  $('#bank-rematch').onclick = () => rematch();
  $('#bank-settings').onclick = () => settings();
  BANK_TABS.forEach(([id]) => {
    const b = $(`#bank-tab-${id}`);
    if (b) b.onclick = () => { state.tab = id; if (state.data && (id === 'refund' || id === 'statements')) render(state.data); else load(); };
  });
  connection();
  await load();
}
