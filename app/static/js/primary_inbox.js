/* v1.73.0 (T-109, ADR-60): «📥 Приёмная папка» на экране «Первичка».
   Сервер сам обходит каталог на своей машине (туда складывают выгрузку СБИС) и раскладывает УПД по компаниям
   по ИНН — без выбора папки в браузере и без пересылки файлов по сети. Здесь только экран: состояние папки,
   настройка (администратор), кнопка «Забрать сейчас» и журнал «что сделано с файлами».
   Все имена верхнего уровня — с приставкой inbox (общая область модулей в сборке). */
import { api } from './api.js';
import { esc, fmtInt, toast } from './ui.js';

const INBOX_STATUS = {
  ok: ['chip verified', 'в реестре'],
  duplicate: ['chip', 'уже был'],
  rejected: ['chip failed', 'не принят'],
};

const INBOX_ERRORS = {
  403: 'Путь вне разрешённых каталогов сервера',
  404: 'Каталог не найден',
  409: 'Приёмная папка не настроена',
  429: 'Обход уже идёт — повторите через минуту',
};

/** Человеческая причина отказа: сначала словами сервера, затем по коду ответа, затем общий текст. */
export function inboxErrorText(err) {
  const status = err && (err.status || err.statusCode);
  const told = (err && err.message) || '';
  if (told && told !== 'Ошибка' && told !== 'Не получилось') return told;
  return (status && INBOX_ERRORS[status]) || told || 'Не получилось';
}

/** Итог обхода одной строкой: «файлов 12 · в реестр 9 · уже было 2 · не принято 1». */
export function inboxSummary(res) {
  const r = res || {};
  const parts = [`файлов: ${fmtInt(r.files || 0)}`];
  const add = (key, title) => { if (Number(r[key])) parts.push(`${title}: ${fmtInt(Number(r[key]))}`); };
  add('ok', 'в реестр');
  add('duplicate', 'уже было');
  add('rejected', 'не принято');
  if (Number(r.waiting)) parts.push(`дописываются: ${fmtInt(Number(r.waiting))}`);
  if (Number(r.left)) parts.push(`осталось на следующий раз: ${fmtInt(Number(r.left))}`);
  return parts.join(' · ');
}

/** Время последнего обхода: «16.11 14:05» в местной зоне; пусто — «ещё не обходили». */
export function inboxWhen(iso) {
  if (!iso) return 'ещё не обходили';
  const d = new Date(/[zZ]|[+-]\d\d:\d\d$/.test(iso) ? iso : `${iso}Z`);
  if (Number.isNaN(d.getTime())) return '—';
  const pad = (n) => String(n).padStart(2, '0');
  return `${pad(d.getDate())}.${pad(d.getMonth() + 1)} ${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

/** Сводка настроек для шапки карточки: включён ли фон и с каким интервалом. */
export function inboxModeText(state) {
  const s = state || {};
  if (!s.path) return 'папка не задана';
  if (s.off) return 'фоновый обход выключен на сервере';
  return s.enabled ? `фон: каждые ${fmtInt(s.interval_minutes)} мин` : 'фон выключен — только по кнопке';
}

function inboxJournalHtml(items) {
  if (!items || !items.length) {
    return '<p class="form-hint">Журнал пуст: программа ещё не брала файлы из этой папки.</p>';
  }
  const rows = items.map((i) => {
    const [cls, title] = INBOX_STATUS[i.status] || ['chip', i.status_title || i.status];
    return `<tr><td>${esc(i.file)}</td><td><span class="${cls}">${esc(title)}</span></td>
      <td>${esc(i.message || '—')}</td><td>${esc(inboxWhen(i.created_at))}</td></tr>`;
  }).join('');
  return `<div class="table-wrap"><table class="data"><thead><tr><th>Файл</th><th>Итог</th>
    <th>Что сказала программа</th><th>Когда</th></tr></thead><tbody>${rows}</tbody></table></div>`;
}

/** Карточка «Приёмная папка» целиком (без обработчиков — их вешает inboxAttach). */
export function inboxPanelHtml(state, log, opts = {}) {
  const s = state || {};
  const counters = s.counters || {};
  const problems = (s.problems || []).map((p) => `<li>${esc(p)}</li>`).join('');
  const roots = (s.roots || []).map((r) => `<code>${esc(r)}</code>`).join(', ');
  const form = opts.isAdmin ? `
    <div class="inbox-settings">
      <label class="field"><span>Каталог на сервере</span>
        <input id="inbox-path" type="text" maxlength="400" value="${esc(s.path || '')}"
          placeholder="/var/lib/ymaster-check/inbox"></label>
      <label class="field"><span>Интервал, минут</span>
        <input id="inbox-interval" type="number" min="1" max="720" step="1" value="${Number(s.interval_minutes) || 10}"></label>
      <label class="field inbox-check"><span>Фоновый обход</span>
        <input id="inbox-enabled" type="checkbox" ${s.enabled ? 'checked' : ''}> брать новые файлы сам</label>
      <div class="inbox-actions">
        <button type="button" class="btn btn-sm" id="inbox-save">Сохранить настройку</button>
        <button type="button" class="btn btn-sm" id="inbox-run">Забрать сейчас</button>
        <button type="button" class="btn btn-sm" id="inbox-refresh">Обновить журнал</button>
      </div>
      <p class="form-hint">Разрешённые каталоги сервера: ${roots || '<span class="form-error">не заданы (YM_PRIMARY_INBOX_ROOTS)</span>'}.
        Другой путь программа не примет.</p>
    </div>` : `
    <div class="inbox-actions">
      <button type="button" class="btn btn-sm" id="inbox-run">Забрать сейчас</button>
      <button type="button" class="btn btn-sm" id="inbox-refresh">Обновить журнал</button>
    </div>
    <p class="form-hint">Каталог на сервере задаёт администратор; вы можете забрать новые файлы в свои компании.</p>`;
  return `
    <div class="card-title">📥 Приёмная папка на сервере <span class="spacer"></span>
      <span class="chip">${esc(inboxModeText(s))}</span></div>
    <p class="form-hint">Выгрузите УПД из СБИС в этот каталог — программа заберёт новые файлы сама: компания
      определяется по ИНН (покупатель — расход, продавец — продажа), принятое переносится в «processed»,
      отказ — в «quarantine» с причиной. Повторная выкладка той же папки ничего не задваивает.</p>
    ${problems ? `<div class="form-error"><ul>${problems}</ul></div>` : ''}
    <p class="form-hint">${esc(inboxLastRun(s))}. Всего в журнале:
      ${fmtInt(counters.total || 0)} — в реестр ${fmtInt(counters.ok || 0)}, уже было
      ${fmtInt(counters.duplicate || 0)}, не принято ${fmtInt(counters.rejected || 0)}.</p>
    ${s.last_error ? `<p class="form-error">Последний обход не удался: ${esc(s.last_error)}</p>` : ''}
    <div id="inbox-result" aria-live="polite"></div>
    ${form}
    <div id="inbox-log">${inboxJournalHtml(log && log.items)}</div>`;
}

/** Строка итога одного обхода — та же формулировка, что в ответе «Забрать сейчас». */
export function inboxLastRun(state) {
  const s = state || {};
  const res = s.last_result || {};
  const files = Number(res.files) || 0;
  if (!files) return 'Последний обход: ' + inboxWhen(s.last_run_at) + '. Файлов на разбор не было.';
  const parts = [`файлов: ${fmtInt(files)}`, `в реестр: ${fmtInt(res.ok || 0)}`,
    `уже было: ${fmtInt(res.duplicate || 0)}`, `не принято: ${fmtInt(res.rejected || 0)}`];
  if (Number(res.later)) parts.push(`дописываются: ${fmtInt(res.later)}`);
  if (Number(res.left)) parts.push(`осталось на следующий раз: ${fmtInt(res.left)}`);
  return parts.join(' · ');
}

/** Прочитать состояние и журнал и перерисовать карточку внутри container. */
export async function inboxLoad(container, opts = {}) {
  const [state, log] = await Promise.all([api.get('/api/v1/primary/inbox/state'),
    api.get('/api/v1/primary/inbox/log?limit=20')]);
  container.innerHTML = inboxPanelHtml(state, log, opts);
  inboxAttach(container, opts);
  return state;
}

/** Обработчики карточки: сохранить настройку, забрать сейчас, обновить журнал. */
export function inboxAttach(container, opts = {}) {
  const result = container.querySelector('#inbox-result');
  const say = (html) => { if (result) result.innerHTML = html; };
  const busy = (button, on) => {
    if (!button) return;
    button.disabled = on;
    button.setAttribute('aria-busy', on ? 'true' : 'false');
  };
  const save = container.querySelector('#inbox-save');
  if (save) {
    save.onclick = async () => {
      const path = (container.querySelector('#inbox-path').value || '').trim();
      const interval = Number(container.querySelector('#inbox-interval').value) || 10;
      const enabled = container.querySelector('#inbox-enabled').checked;
      busy(save, true);
      try {
        await api.put('/api/v1/primary/inbox/settings', { path, enabled, interval_minutes: interval });
        toast('Настройка приёмной папки сохранена', 'ok');
        await inboxLoad(container, opts);
        return;
      } catch (e) {
        say(`<div class="form-error">${esc(inboxErrorText(e))}</div>`);
      } finally {
        busy(save, false);
      }
    };
  }
  const run = container.querySelector('#inbox-run');
  if (run) {
    run.onclick = async () => {
      busy(run, true);
      say('<p class="form-hint">Смотрим папку…</p>');
      try {
        const res = await api.post('/api/v1/primary/inbox/run');
        const sum = (res && res.result) || {};
        say(`<p class="form-hint"><b>Готово.</b> ${esc(inboxSummary(sum))}</p>`);
        toast(`Приёмная папка: ${inboxSummary(sum)}`, sum.rejected && !sum.ok ? 'err' : 'ok');
        if (typeof opts.onTaken === 'function') opts.onTaken(sum);
        await inboxLoad(container, opts);
      } catch (e) {
        say(`<div class="form-error">${esc(inboxErrorText(e))}</div>`);
      } finally {
        busy(run, false);
      }
    };
  }
  const refresh = container.querySelector('#inbox-refresh');
  if (refresh) refresh.onclick = () => inboxLoad(container, opts).catch((e) => toast(inboxErrorText(e), 'err'));
}
