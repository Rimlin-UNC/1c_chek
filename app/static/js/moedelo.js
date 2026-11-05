/* v1.70.107 (T-71, ADR-37): «Моё дело» по API — акты покупок из первички.
   Панель «📒 «Моё дело»» — в карточке компании: ключ API (хранится зашифрованным и не показывается),
   «Сохранить и проверить», «Проверить ключ», «Отключить». В карточке документа первички — что уйдёт и что
   мешает, кнопка «📒 В «Моё дело»» и отметка об отправке. На экране «Первичка» — пакет: отмеченные документы
   или период (сначала проверка «что уйдёт», потом отправка). Накладные с товарами — следующим этапом.
   Все имена верхнего уровня — с приставкой md (общая область модулей в сборке). */
import { api } from './api.js';
import { esc, toast } from './ui.js';

export const MD_STATUS = {
  none: ['chip unknown', 'не подключено'],
  new: ['chip verifying', 'ключ сохранён, не проверен'],
  active: ['chip verified', 'подключено'],
  error: ['chip failed', 'ошибка подключения'],
};

export const MD_DOCS_URL = 'https://restapi.moedelo.org/docs';

/** Время с сервера (UTC без пометки зоны) → «05.11 21:40». */
export function mdWhen(iso) {
  if (!iso) return '';
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return '';
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

export function mdPanelHtml(state) {
  const d = state || {};
  const status = MD_STATUS[d.status] ? d.status : 'none';
  const [cls, label] = MD_STATUS[status];
  const facts = [d.key_set && d.key_tail ? `ключ …${d.key_tail}` : '',
    d.last_ok_at ? `проверен ${mdWhen(d.last_ok_at)}` : ''].filter(Boolean).join(' · ');
  return `<div class="bank-conn-head"><div class="card-title">📒 «Моё дело»</div><span class="${cls}">${esc(label)}</span></div>
    <p class="form-hint">Документы первички уходят в онлайн-бухгалтерию «Моё дело» без ручного ввода: услуги — актом
      покупки, поставщик находится или добавляется по ИНН, повторно документ не уходит. Накладные с товарами — следующим этапом.</p>
    ${facts ? `<div class="form-hint">${esc(facts)}</div>` : ''}
    ${d.last_error ? `<div class="form-error">${esc(d.last_error)}</div>` : ''}
    <details ${status === 'active' ? '' : 'open'}><summary>Ключ API${d.key_set ? ` — …${esc(d.key_tail || '')}` : ''}</summary>
      <label class="field"><span>Ключ API «Моего дела»</span><input id="mdc-key" type="password" maxlength="300"
        autocomplete="off" spellcheck="false" placeholder="${d.key_set ? 'задан — вставьте новый, чтобы заменить' : 'xxxxxxxx-xxxx-xxxx-xxxx-xxxxxxxxxxxx'}"></label>
      <p class="form-hint">Где взять: в «Моём деле» — «Настройки → Интеграция с партнёрами»: включите интеграцию и скопируйте
        ключ. Один ключ — одна компания. Ключ хранится зашифрованным и никому не показывается.
        <a href="${esc(d.docs_url || MD_DOCS_URL)}" target="_blank" rel="noopener">Описание API</a></p>
    </details>
    <div class="dreq-tools">
      <button type="button" class="btn btn-sm btn-primary" id="mdc-save">${d.key_set ? 'Сохранить и проверить' : 'Подключить и проверить'}</button>
      ${d.key_set ? '<button type="button" class="btn btn-sm" id="mdc-check">Проверить ключ</button>' : ''}
      ${d.key_set ? '<button type="button" class="btn btn-sm btn-bad" id="mdc-del">Отключить</button>' : ''}
    </div>
    <div id="mdc-msg" class="form-hint" aria-live="polite"></div>`;
}

export async function mdConnectPanel(el, companyId) {
  if (!el) return;
  const q = companyId ? `?company_id=${encodeURIComponent(companyId)}` : '';
  const scope = companyId ? { company_id: companyId } : {};
  let data = null;

  function render() {
    el.innerHTML = mdPanelHtml(data);
    const get = (id) => el.querySelector(`#${id}`);
    const say = (text, bad = false) => {
      const m = get('mdc-msg');
      if (!m) return;
      m.textContent = text;
      m.classList.toggle('form-error', bad);
    };
    const check = async () => {
      say('Проверяем ключ в «Моём деле»…');
      try {
        data = await api.post('/api/v1/moedelo/connection/check', scope);
        render();
        toast('«Моё дело» подключено: ключ работает', 'ok', '📒');
      } catch (e) {
        try { data = await api.get(`/api/v1/moedelo/connection${q}`); render(); } catch { /* панель покажет прежнее */ }
        const m = el.querySelector('#mdc-msg');
        if (m) { m.textContent = e.message; m.classList.add('form-error'); }
      }
    };
    get('mdc-save').onclick = async () => {
      const key = get('mdc-key').value.trim();
      if (!key && !(data && data.key_set)) { say('Вставьте ключ API из «Моего дела»', true); return; }
      const btn = get('mdc-save');
      btn.disabled = true;
      if (key) {
        try { data = await api.put('/api/v1/moedelo/connection', { ...scope, key }); } catch (e) {
          btn.disabled = false;
          say(e.message, true);
          return;
        }
      }
      await check();
    };
    const again = get('mdc-check');
    if (again) again.onclick = () => { again.disabled = true; check(); };
    const del = get('mdc-del');
    if (del) {
      del.onclick = async () => {
        if (!window.confirm('Отключить «Моё дело»? Ключ будет удалён; отметки об отправленных документах останутся — повторно они не уйдут.')) return;
        try {
          await api.del(`/api/v1/moedelo/connection${q}`);
          toast('«Моё дело» отключено', 'ok', '📒');
          load();
        } catch (e) { say(e.message, true); }
      };
    }
  }

  async function load() {
    el.innerHTML = '<div class="skeleton bank-conn-skeleton"></div>';
    try {
      data = await api.get(`/api/v1/moedelo/connection${q}`);
      if (el.isConnected) render();
    } catch (e) {
      if (el.isConnected) el.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }
  await load();
}

/** Блок «Моё дело» в карточке документа: отметка об отправке, причина «не уйдёт» или кнопка. */
export function mdDocHtml(state) {
  const s = state || {};
  const conn = s.connection || {};
  const plan = s.plan || {};
  const link = s.link;
  if (link) {
    return `<p class="form-hint">📒 «Моё дело»: ${esc(link.title || 'акт покупки')}${link.kontragent_new ? ' · поставщик добавлен по ИНН' : ''}.</p>
      ${link.message ? `<div class="form-error">${esc(link.message)}</div>` : ''}
      <div class="dreq-tools"><button type="button" class="btn btn-sm" id="md-forget">Забыть отправку</button></div>`;
  }
  if (conn.status !== 'active') {
    const why = !conn.status || conn.status === 'none'
      ? 'не подключено — ключ API вставляется в карточке компании' : (conn.status_title || 'ключ не проверен');
    return `<p class="form-hint">📒 «Моё дело»: ${esc(why)}.</p>`;
  }
  const blockers = Array.isArray(plan.blockers) ? plan.blockers : [];
  if (blockers.length) {
    return `<p class="form-hint">📒 «Моё дело»: не отправится.</p>
      <ul class="prim-results">${blockers.map((b) => `<li>${esc(b)}</li>`).join('')}</ul>`;
  }
  const services = plan.kind === 'services';
  return `<p class="form-hint">📒 «Моё дело»: ${esc(plan.kind_title || '')}.</p>
    ${services ? '' : '<label class="prim-export-format"><input type="checkbox" id="md-as-services"><span>Это услуги — отправить актом покупки</span></label>'}
    <div class="dreq-tools"><button type="button" class="btn btn-sm btn-primary" id="md-send"${services ? '' : ' disabled'}>📒 В «Моё дело»</button></div>`;
}

export async function mdDocPanel(el, docId, opts = {}) {
  if (!el) return;
  const url = `/api/v1/moedelo/documents/${encodeURIComponent(docId)}`;
  let state = null;

  function render() {
    el.innerHTML = mdDocHtml(state);
    const send = el.querySelector('#md-send');
    const asServices = el.querySelector('#md-as-services');
    if (asServices && send) asServices.onchange = () => { send.disabled = !asServices.checked; };
    if (send) {
      send.onclick = async () => {
        send.disabled = true;
        try {
          const res = await api.post(`${url}/send`, { as_services: Boolean(asServices && asServices.checked) });
          state = res.state;
          render();
          const l = res.link || {};
          if (l.status === 'mismatch') toast(l.message || 'Сумма в «Моём деле» другая — проверьте акт', 'warn', '📒');
          else toast(`В «Моём деле»: ${l.title || 'акт покупки'}`, 'ok', '📒');
          if (typeof opts.onSent === 'function') opts.onSent(res);
        } catch (e) { send.disabled = false; toast(e.message, 'err', '📒'); }
      };
    }
    const forget = el.querySelector('#md-forget');
    if (forget) {
      forget.onclick = async () => {
        if (!window.confirm('Забыть отправку? Это нужно, только если акт удалён в «Моём деле». При повторной отправке программа сначала поищет акт там.')) return;
        try { await api.del(`${url}/link`); load(); } catch (e) { toast(e.message, 'err'); }
      };
    }
  }

  async function load() {
    try {
      state = await api.get(url);
      if (el.isConnected) render();
    } catch (e) {
      if (el.isConnected) el.innerHTML = `<p class="form-hint">📒 «Моё дело»: ${esc(e.message)}</p>`;
    }
  }
  await load();
}

/** Итог проверки пакета: сколько уйдёт, сколько уже было, что не уйдёт и почему. */
export function mdBatchSummaryHtml(check) {
  if (!check) return '';
  const more = Number(check.more) ? ` (за один раз — не больше ${Number(check.limit) || 50}, ещё ${Number(check.more)} — следующим заходом)` : '';
  const out = [`<p><b>Уйдёт актами покупки: ${Number(check.count) || 0}</b>${more}.</p>`];
  if (Number(check.already)) out.push(`<p class="form-hint">Уже в «Моём деле»: ${Number(check.already)} — повторно не уйдут.</p>`);
  const skipped = Array.isArray(check.skipped) ? check.skipped : [];
  if (skipped.length) {
    out.push(`<p class="form-hint">Не уйдут (${Number(check.skipped_total) || skipped.length}):</p>
      <ul class="prim-results">${skipped.slice(0, 20).map((s) => `<li><b>${esc(s.title)}</b>: ${esc(s.reason)}</li>`).join('')}</ul>`);
  }
  return out.join('');
}

/** Итог отправки пакета одной строкой: «отправлено: 2 · не ушло: 1». */
export function mdResultSummary(res) {
  const r = res || {};
  const parts = [`отправлено: ${(Number(r.sent) || 0) + (Number(r.mismatch) || 0)}`];
  if (Number(r.found)) parts.push(`уже были в «Моём деле»: ${Number(r.found)}`);
  if (Number(r.mismatch)) parts.push(`сумма не совпала: ${Number(r.mismatch)}`);
  const failed = Array.isArray(r.failed) ? r.failed.length : 0;
  if (failed) parts.push(`не ушло: ${failed}`);
  return parts.join(' · ');
}
