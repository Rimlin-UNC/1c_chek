/* v1.70.94 (T-53): экран «Первичка» — электронные УПД и счета-фактуры (форматы ФНС 5.01–5.03).
   Загрузка XML или ZIP-архива оператора ЭДО, проверка (реквизиты, ИНН и КПП, арифметика строк
   и итогов, ставка НДС на переходе к 22 %, формат 5.03 с 01.04.2026), реестр с фильтрами и
   карточка документа. Доступ — бухгалтер и администратор; каждый видит только свою компанию.
   v1.70.95 (T-61): «комбайн первички». XML сверяется со схемой ФНС. PDF, сканы и фото печатной
   формы распознаются на своём сервере, черновик сверяется в окне: превью, поля «проверьте»,
   подсказки «Подставить». Есть ручной ввод бумажного документа. В реестр документ попадает
   только по кнопке «Сохранить». Все имена верхнего уровня — с приставкой prim (общая область
   модулей в сборке).
   v1.70.96 (T-60): выгрузка в 1С. Кнопка «В 1С» в реестре: файлы УПД 5.03 для типовой кнопки
   1С «Загрузить → Из файла» (собираются из реквизитов и проверяются по схеме ФНС) или пакет
   EnterpriseData. Сначала окно показывает, что уйдёт и почему пропущено остальное; после
   скачивания документы отмечаются «выгружен». Отметки в реестре — выгрузить выбранные.
   v1.70.104 (T-63): карточка «Из почты» — черновики PDF и сканов из почтового ящика компании сверяются в том же
   окне (файл письма — кнопкой «Открыть файл»), XML и ZIP из писем попадают в реестр сами. */
import { api } from './api.js';
import { esc, fmtSum, openModal, toast } from './ui.js';
import { mailConnectPanel, mailInboxHtml } from './primary_mail.js';

export const PRIMARY_STATUSES = [
  { code: 'ok', title: 'Без замечаний', chip: 'verified' },
  { code: 'warnings', title: 'Есть замечания', chip: 'verifying' },
  { code: 'errors', title: 'Ошибки', chip: 'failed' },
];
const PRIM_LEVEL = {
  error: { icon: '⛔', title: 'Ошибка' },
  warning: { icon: '⚠️', title: 'Замечание' },
  info: { icon: 'ℹ️', title: 'Сведения' },
};
const PRIM_RESULT = { created: 'загружен', duplicate: 'уже был загружен', rejected: 'не принят' };
const PRIM_VAT = { none: 'без НДС', agent: 'налоговый агент' };
export const PRIM_SOURCES = { xml: 'файл ЭДО', pdf: 'PDF', scan: 'скан или фото', manual: 'введён вручную' };
const PRIM_STRUCTURED = /\.(xml|zip)$/i;
const PRIM_PRINTED = /\.(pdf|jpe?g|png|tiff?)$/i;
const PRIM_KINDS = [
  { code: 'СЧФДОП', title: 'УПД, статус 1 (счёт-фактура и передаточный документ)' },
  { code: 'ДОП', title: 'УПД, статус 2 (только передаточный документ)' },
  { code: 'СЧФ', title: 'Счёт-фактура' },
];
const PRIM_FIELD_TITLES = {
  number: 'номер', date: 'дата', seller_name: 'продавец', seller_inn: 'ИНН продавца', seller_kpp: 'КПП продавца',
  buyer_name: 'покупатель', buyer_inn: 'ИНН покупателя', buyer_kpp: 'КПП покупателя', lines: 'строки', totals: 'итоги',
};

export function primStatusChip(status) {
  const st = PRIMARY_STATUSES.find((s) => s.code === status) || { title: status || '—', chip: 'unknown' };
  return `<span class="chip ${st.chip}"><span class="dot"></span>${esc(st.title)}</span>`;
}

export function primVatLabel(rate) {
  if (!rate) return '—';
  if (PRIM_VAT[rate]) return PRIM_VAT[rate];
  return rate.includes('/') ? rate : `${rate} %`;
}

export function primDay(iso) {
  const m = /^(\d{4})-(\d{2})-(\d{2})/.exec(iso || '');
  return m ? `${m[3]}.${m[2]}.${m[1]}` : '—';
}

const primMoney = (v) => (v === null || v === undefined ? '—' : fmtSum(v));
const primNum = (v) => (v === null || v === undefined ? '—' : new Intl.NumberFormat('ru-RU', { maximumFractionDigits: 3 }).format(v));

export function primUploadSummary(res) {
  const parts = [];
  if (res.created) parts.push(`загружено ${res.created}`);
  if (res.duplicates) parts.push(`уже были ${res.duplicates}`);
  if (res.rejected) parts.push(`не принято ${res.rejected}`);
  return parts.length ? parts.join(', ') : 'нет документов';
}

export function primIssuesHtml(issues) {
  const list = Array.isArray(issues) ? issues : [];
  const serious = list.some((i) => i.level === 'error' || i.level === 'warning');
  const head = serious ? '' : '<p class="form-hint">Замечаний нет — документ прошёл все проверки.</p>';
  if (!list.length) return head;
  return `${head}<ul class="prim-issues">${list.map((i) => {
    const lv = PRIM_LEVEL[i.level] || PRIM_LEVEL.info;
    const details = Array.isArray(i.details) && i.details.length
      ? `<details><summary>Подробно (${i.details.length})</summary><ul>${i.details.map((d) => `<li>${esc(d)}</li>`).join('')}</ul></details>`
      : '';
    return `<li data-level="${esc(i.level)}"><span aria-hidden="true">${lv.icon}</span>
      <span class="visually-hidden">${esc(lv.title)}:</span> ${esc(i.message)}${details}</li>`;
  }).join('')}</ul>`;
}

export function primSourceLabel(source) {
  return PRIM_SOURCES[source] || PRIM_SOURCES.xml;
}

/** Файлы загрузки: XML и ZIP — сразу в реестр; PDF, сканы и фото — на распознавание и сверку. */
export function primSplitFiles(files) {
  const structured = [];
  const printed = [];
  const unknown = [];
  [...(files || [])].forEach((f) => {
    const name = (f && f.name) || '';
    if (PRIM_PRINTED.test(name)) printed.push(f);
    else if (PRIM_STRUCTURED.test(name)) structured.push(f);
    else unknown.push(f);
  });
  return { structured: structured.concat(unknown), printed };
}

/** ИНН: 10 цифр (организация) или 12 (ИП) с верной контрольной суммой. */
export function primInnValid(inn) {
  const s = String(inn || '');
  const sum = (w) => w.reduce((acc, k, i) => acc + k * Number(s[i]), 0) % 11 % 10;
  if (/^\d{10}$/.test(s)) return sum([2, 4, 10, 3, 5, 9, 4, 6, 8]) === Number(s[9]);
  if (/^\d{12}$/.test(s)) {
    return sum([7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === Number(s[10])
      && sum([3, 7, 2, 4, 10, 3, 5, 9, 4, 6, 8]) === Number(s[11]);
  }
  return false;
}

export function primKppValid(kpp) {
  return /^\d{4}[\dA-Z]{2}\d{3}$/.test(String(kpp || ''));
}

/** «1 100,00», «1100.5», «» → число или null (пустое поле — «не указано»). */
export function primParseMoney(text) {
  const t = String(text ?? '').replace(/[\s\u00a0]/g, '').replace(',', '.');
  if (!t) return null;
  return /^-?\d+(\.\d+)?$/.test(t) ? Number(t) : NaN;
}

export function primMoneyField(v) {
  return v === null || v === undefined || Number.isNaN(Number(v)) ? '' : Number(v).toFixed(2).replace('.', ',');
}

/** Быстрые проверки черновика прямо в окне (окончательная проверка — на сервере). */
export function primDraftChecks(d) {
  const out = [];
  const add = (level, message) => out.push({ level, message });
  if (!d.number) add('error', 'Укажите номер документа');
  if (!d.date) add('error', 'Укажите дату документа');
  if (!d.seller.name) add('error', 'Укажите продавца');
  if (!d.seller.inn) add('error', 'Укажите ИНН продавца');
  [['продавца', d.seller], ['покупателя', d.buyer]].forEach(([role, p]) => {
    if (p.inn && !primInnValid(p.inn)) add('error', `ИНН ${role} ${p.inn} — неверная контрольная сумма`);
    if (p.kpp && p.inn.length === 10 && !primKppValid(p.kpp)) add('warning', `КПП ${role} ${p.kpp} — нужно 9 знаков`);
  });
  const t = d.totals;
  ['without_vat', 'vat', 'with_vat'].forEach((k) => { if (Number.isNaN(t[k])) add('error', 'В суммах — не число'); });
  if ([t.without_vat, t.vat, t.with_vat].every((v) => typeof v === 'number' && !Number.isNaN(v))
      && Math.abs(t.without_vat + t.vat - t.with_vat) > 0.011) {
    add('warning', `Без НДС ${fmtSum(t.without_vat)} + НДС ${fmtSum(t.vat)} не равно сумме с НДС ${fmtSum(t.with_vat)}`);
  }
  if (d.lines.length && typeof t.with_vat === 'number') {
    const sum = d.lines.reduce((acc, ln) => acc + (Number(ln.sum_with_vat) || 0), 0);
    if (Math.abs(sum - t.with_vat) > 0.05) add('warning', `Строки дают ${fmtSum(sum)}, а итог с НДС — ${fmtSum(t.with_vat)}`);
  }
  return out;
}

export function primDraftChecksHtml(checks) {
  if (!checks.length) return '<p class="form-hint">✅ Обязательные реквизиты на месте, суммы сходятся.</p>';
  return primIssuesHtml(checks);
}

export function primOcrStatusHtml(st, isAdmin) {
  if (!st || typeof st.ready !== 'boolean') return '';
  const last = st.last && st.last.seconds
    ? ` Последний скан: ${String(st.last.seconds).replace('.', ',')} с, память ${esc(String(st.last.peak_mb || '—'))} МБ.` : '';
  if (st.ready) return `<span aria-hidden="true">🔎</span> Распознавание сканов и фото: готово.${esc(last)}`;
  const can = isAdmin && st.engine && Array.isArray(st.missing) && st.missing.length;
  return `<span aria-hidden="true">🔎</span> Распознавание сканов и фото: не настроено — ${esc(st.reason || 'нет данных')}.
    PDF из 1С и систем ЭДО распознаются и без него.
    ${can ? '<button type="button" class="btn btn-sm" id="prim-ocr-models">Скачать модели (8 МБ)</button>' : ''}`;
}

function primField(id, label, value, opts = {}) {
  const cls = (opts.flag ? ` prim-${opts.flag}` : '') + (opts.wide ? ' prim-wide' : '');
  const mark = opts.flag === 'uncertain' ? ' <span title="Распознано неуверенно — проверьте">⚠</span>'
    : opts.flag === 'missing' ? ' <span title="Не найдено — заполните">✱</span>' : '';
  return `<label class="field${cls}"><span>${esc(label)}${mark}</span><input id="${id}" type="${opts.type || 'text'}"
    value="${esc(value ?? '')}" ${opts.inputmode ? `inputmode="${opts.inputmode}"` : ''} maxlength="${opts.max || 500}"></label>`;
}

/** Окно сверки черновика: превью слева, реквизиты справа. rec — ответ /recognize (или пустой для ручного ввода). */
export function primReviewHtml(rec, opts = {}) {
  const d = rec.draft || {};
  const seller = d.seller || {};
  const buyer = d.buyer || {};
  const totals = d.totals || {};
  const found = new Set(rec.found || []);
  const uncertain = new Set(rec.uncertain || []);
  const manual = rec.source === 'manual';
  const flag = (key) => (manual ? '' : uncertain.has(key) ? 'uncertain' : found.has(key) ? '' : 'missing');
  const hint = (role) => {
    const h = rec.hints && rec.hints[role];
    return h && h.name ? `<p class="form-hint prim-hint">Из ${esc(h.from || 'базы')}: «${esc(h.name)}»
      <button type="button" class="btn btn-sm" id="prim-hint-${role}">Подставить</button></p>` : '';
  };
  const lines = Array.isArray(d.lines) ? d.lines : [];
  // v1.70.104: у черновика из почты файла в браузере нет — он открывается кнопкой (blob-адрес из /inbox/{id}/file)
  const openFile = opts.fileUrl ? `<a class="btn btn-sm" id="prim-open-file" href="${esc(opts.fileUrl)}" target="_blank"
      rel="noopener">📄 Открыть файл</a>` : '';
  const preview = opts.previewUrl
    ? `<button type="button" class="btn btn-sm" id="prim-zoom" aria-pressed="false">🔍 Крупнее</button>${openFile}
      <img src="${esc(opts.previewUrl)}" alt="Изображение документа для сверки">`
    : opts.fileUrl ? `<p class="form-hint">Сверьте поля с файлом из письма.</p>${openFile}`
      : `<p class="form-hint">${manual ? 'Введите реквизиты с бумажного документа.'
        : 'Сверьте поля с PDF-файлом: текст взят из файла как есть, ошибиться могла только разметка.'}</p>`;
  const ocr = rec.ocr ? `<p class="form-hint">Распознано на сервере: ${esc(rec.ocr.engine || 'Tesseract')}, ${esc(String(rec.ocr.seconds ?? '—'))} с.</p>` : '';
  const missing = [...Object.keys(PRIM_FIELD_TITLES)].filter((k) => !found.has(k) && k !== 'lines');
  const unsure = [...uncertain].map((k) => PRIM_FIELD_TITLES[k] || k);
  const summary = manual ? 'Ручной ввод: заполните реквизиты бумажного документа.'
    : `Источник: ${esc(primSourceLabel(rec.source))}. ${unsure.length ? `Проверьте внимательно: ${esc(unsure.join(', '))}. ` : ''}${
      missing.length ? `Не найдено: ${esc(missing.map((k) => PRIM_FIELD_TITLES[k]).join(', '))}.` : 'Все основные поля найдены.'}`;
  return `
    <h3 class="modal-title">${manual ? '✍️ Документ вручную' : `🔍 Сверьте реквизиты: ${esc(rec.file_name || 'документ')}`}</h3>
    <p class="form-hint">${summary} В реестр документ попадёт только после «Сохранить».</p>
    ${opts.mail ? `<p class="form-hint">📬 Из почты: ${esc(opts.mail)}</p>` : ''}${rec.message ? `<p class="form-hint">${esc(rec.message)}</p>` : ''}
    ${rec.duplicate ? `<div class="form-error">Такой документ уже есть в реестре: ${esc(primTitle(rec.duplicate))}.</div>` : ''}
    <div class="prim-review">
      <div class="prim-preview">${preview}${ocr}</div>
      <div class="prim-form">
        <fieldset><legend>Документ</legend>
          <label class="field prim-wide"><span>Вид</span><select id="prim-f-function">${PRIM_KINDS.map((k) =>
            `<option value="${k.code}" ${k.code === (d.function || 'СЧФДОП') ? 'selected' : ''}>${esc(k.title)}</option>`).join('')}</select></label>
          ${primField('prim-f-number', 'Номер', d.number, { flag: flag('number'), max: 100 })}
          ${primField('prim-f-date', 'Дата', d.date, { flag: flag('date'), type: 'date' })}
          ${primField('prim-f-corr-number', 'Исправление №', d.correction_number, { max: 20 })}
          ${primField('prim-f-corr-date', 'Дата исправления', d.correction_date, { type: 'date' })}
        </fieldset>
        <fieldset><legend>Продавец</legend>
          ${primField('prim-f-seller-name', 'Наименование', seller.name, { flag: flag('seller_name'), wide: true })}
          ${hint('seller')}
          ${primField('prim-f-seller-inn', 'ИНН', seller.inn, { flag: flag('seller_inn'), inputmode: 'numeric', max: 12 })}
          ${primField('prim-f-seller-kpp', 'КПП', seller.kpp, { flag: flag('seller_kpp'), max: 9 })}
        </fieldset>
        <fieldset><legend>Покупатель</legend>
          ${primField('prim-f-buyer-name', 'Наименование', buyer.name, { flag: flag('buyer_name'), wide: true })}
          ${hint('buyer')}
          ${primField('prim-f-buyer-inn', 'ИНН', buyer.inn, { flag: flag('buyer_inn'), inputmode: 'numeric', max: 12 })}
          ${primField('prim-f-buyer-kpp', 'КПП', buyer.kpp, { flag: flag('buyer_kpp'), max: 9 })}
        </fieldset>
        <fieldset><legend>Суммы, ₽</legend>
          ${primField('prim-f-base', 'Без НДС', primMoneyField(totals.without_vat), { flag: flag('totals'), inputmode: 'decimal', max: 20 })}
          ${primField('prim-f-vat', 'НДС', primMoneyField(totals.vat), { flag: flag('totals'), inputmode: 'decimal', max: 20 })}
          ${primField('prim-f-total', 'С НДС', primMoneyField(totals.with_vat), { flag: flag('totals'), inputmode: 'decimal', max: 20 })}
        </fieldset>
        ${lines.length ? `<h4>Строки документа${uncertain.has('lines') ? ' <span title="Арифметика строки не сошлась — проверьте">⚠</span>' : ''}</h4>
          ${primLinesHtml(lines)}
          <label class="field-inline"><input type="checkbox" id="prim-f-lines" checked> Сохранить строки (${lines.length})</label>`
          : (manual ? '' : '<p class="form-hint">Строки не распознаны — документ сохранится по итоговым суммам.</p>')}
        <h4>Проверка</h4><div id="prim-check" aria-live="polite"></div>
        <div class="modal-actions">
          <button type="button" class="btn btn-primary" id="prim-save">Сохранить в реестр</button>
          ${manual ? '' : `<button type="button" class="btn" id="prim-skip">${esc(opts.skipLabel || 'Пропустить')}</button>`}
        </div>
      </div>
    </div>`;
}

/** Значения полей окна → черновик для /confirm. get(id) — элемент по id (в тестах — подделка). */
export function primReadDraft(get, rec) {
  const val = (id) => String((get(id) || {}).value ?? '').trim();
  const digits = (id) => val(id).replace(/\D/g, '');
  const keepLines = Boolean((get('prim-f-lines') || {}).checked);
  return {
    function: val('prim-f-function') || 'СЧФДОП',
    number: val('prim-f-number'),
    date: val('prim-f-date') || null,
    correction_number: val('prim-f-corr-number'),
    correction_date: val('prim-f-corr-date') || null,
    currency: (rec.draft && rec.draft.currency) || '643',
    seller: { name: val('prim-f-seller-name'), inn: digits('prim-f-seller-inn'), kpp: val('prim-f-seller-kpp').toUpperCase() },
    buyer: { name: val('prim-f-buyer-name'), inn: digits('prim-f-buyer-inn'), kpp: val('prim-f-buyer-kpp').toUpperCase() },
    totals: { without_vat: primParseMoney(val('prim-f-base')), vat: primParseMoney(val('prim-f-vat')),
              with_vat: primParseMoney(val('prim-f-total')) },
    lines: keepLines && rec.draft && Array.isArray(rec.draft.lines) ? rec.draft.lines : [],
  };
}


export function primLinesHtml(lines) {
  const list = Array.isArray(lines) ? lines : [];
  if (!list.length) return '<p class="form-hint">В документе нет строк.</p>';
  return `<div class="table-wrap"><table class="data prim-lines"><thead><tr><th>№</th><th>Наименование</th>
    <th class="num">Кол-во</th><th class="num">Цена</th><th class="num">Без НДС</th><th>Ставка</th>
    <th class="num">НДС</th><th class="num">С НДС</th></tr></thead><tbody>
    ${list.map((ln) => `<tr><td>${esc(String(ln.position))}</td>
      <td>${esc(ln.name || '—')}${ln.item_code ? `<div class="form-hint">код ${esc(ln.item_code)}</div>` : ''}</td>
      <td class="num">${primNum(ln.quantity)}${ln.unit_name ? ' ' + esc(ln.unit_name) : ''}</td>
      <td class="num">${primMoney(ln.price)}</td><td class="num">${primMoney(ln.sum_without_vat)}</td>
      <td>${esc(primVatLabel(ln.vat_rate))}</td><td class="num">${primMoney(ln.vat_sum)}</td>
      <td class="num">${primMoney(ln.sum_with_vat)}</td></tr>`).join('')}</tbody></table></div>`;
}

function primParty(p) {
  if (!p || !p.name) return '—';
  const req = [p.inn && `ИНН ${p.inn}`, p.kpp && `КПП ${p.kpp}`].filter(Boolean).join(', ');
  return `${esc(p.name)}${req ? `<div class="form-hint">${esc(req)}</div>` : ''}`;
}

function primTitle(d) {
  const corr = d.correction_number ? ` · исправление № ${d.correction_number} от ${primDay(d.correction_date)}` : '';
  return `${d.kind} № ${d.doc_number || 'без номера'} от ${primDay(d.doc_date)}${corr}`;
}

// v1.70.96 (T-60): выгрузка в 1С
export const PRIM_EXPORT_FORMATS = [
  { code: 'upd', title: 'Файлы УПД для 1С',
    hint: 'Типовая 1С:Бухгалтерия 3.0 загрузит их сама: «Покупки → Поступление (акты, накладные, УПД) → Загрузить → Из файла». Программист не нужен. Несколько документов — архив ZIP: распакуйте его и загрузите файлы.' },
  { code: 'json', title: 'EnterpriseData, JSON', hint: 'Для обработки или расширения 1С — как выгрузка чеков.' },
  { code: 'xml', title: 'EnterpriseData, XML', hint: 'То же в XML.' },
];

/** Когда документ выгружен в 1С: «✓ 11.03.2026». Сервер отдаёт время по UTC без пометки зоны. */
export function primExportedLabel(iso) {
  if (!iso) return '';
  const d = new Date(/(?:[zZ]|[+-]\d\d:?\d\d)$/.test(iso) ? iso : `${iso}Z`);
  return Number.isNaN(d.getTime()) ? '' : `✓ ${d.toLocaleDateString('ru-RU')}`;
}

/** Тело запроса выгрузки: отмеченные — по id, иначе новые документы в фильтре дат и компании. */
export function primExportBody({ ids = [], format = 'upd', includeErrors = false, companyId = '', from = '', to = '' } = {}) {
  const body = { format, include_errors: Boolean(includeErrors) };
  if (ids.length) body.ids = ids.slice(0, 500);
  else {
    if (from) body.date_from = from;
    if (to) body.date_to = to;
  }
  if (companyId) body.company_id = companyId;
  return body;
}

/** Итог проверки выгрузки: сколько уйдёт, что пропущено и почему. */
export function primExportSummaryHtml(check) {
  if (!check) return '';
  const limit = check.limit && check.count >= check.limit ? ` (за один раз — не больше ${check.limit})` : '';
  const out = [`<p><b>Будет выгружено: ${Number(check.count) || 0}</b>${limit}.</p>`];
  if (check.already_exported) {
    out.push(`<p class="form-hint">Уже выгружались раньше: ${check.already_exported} — вы отметили их сами, они выгрузятся повторно. Если документ уже есть в 1С, второй раз его не загружайте.</p>`);
  }
  if (check.errors_excluded) {
    out.push(`<p class="form-hint">С ошибками проверки не выгружаются: ${check.errors_excluded}. Исправьте их или включите «и документы с ошибками».</p>`);
  }
  const skipped = Array.isArray(check.skipped) ? check.skipped : [];
  if (skipped.length) {
    out.push(`<p class="form-hint">Файлом УПД не выгрузятся (${skipped.length}) — поправьте реквизиты или внесите документ в 1С вручную:</p>
      <ul class="prim-results">${skipped.slice(0, 20).map((s) => `<li><b>${esc(s.title)}</b>: ${esc(s.reason)}</li>`).join('')}</ul>`);
  }
  return out.join('');
}

function primDownload(blob, filename) {
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 3000);
}

export function primRowsHtml(items, selected) {
  const list = Array.isArray(items) ? items : [];
  const picked = selected instanceof Set ? selected : new Set();
  if (!list.length) {
    return '<tr><td colspan="9">Документов пока нет. Загрузите XML или ZIP из системы ЭДО — проверка займёт секунды.</td></tr>';
  }
  return list.map((d) => `<tr data-id="${esc(d.id)}"><td><input type="checkbox" class="prim-pick" data-id="${esc(d.id)}"
      ${picked.has(d.id) ? 'checked' : ''} aria-label="Отметить: ${esc(primTitle(d))}"></td><td>${esc(primDay(d.doc_date))}</td>
    <td>${esc(d.doc_number || '—')}${d.correction_number ? `<div class="form-hint">испр. № ${esc(d.correction_number)}</div>` : ''}${
      d.source && d.source !== 'xml' ? `<div class="form-hint">${esc(primSourceLabel(d.source))}</div>` : ''}</td>
    <td>${primParty(d.seller)}</td><td class="num">${primMoney(d.total_with_vat)}</td><td class="num">${primMoney(d.vat_total)}</td>
    <td>${primStatusChip(d.status)}${d.errors || d.warnings
      ? `<div class="form-hint">${[d.errors && `ошибок ${d.errors}`, d.warnings && `замечаний ${d.warnings}`].filter(Boolean).join(', ')}</div>`
      : ''}</td>
    <td>${d.exported_at ? `<span title="Выгружен в 1С">${esc(primExportedLabel(d.exported_at))}</span>` : '<span class="form-hint">—</span>'}</td>
    <td><button type="button" class="btn btn-sm prim-open" data-id="${esc(d.id)}">Открыть</button></td></tr>`).join('');
}

function primResultsHtml(res) {
  const rows = (res.results || []).map((r) => {
    const doc = r.document;
    const what = doc ? `${esc(primTitle(doc))} — ${primStatusChip(doc.status)}` : esc(r.message || '');
    const note = r.result === 'created' ? '' : `<div class="form-hint">${esc(r.message || '')}</div>`;
    return `<li><b>${esc(r.file)}</b>: ${esc(PRIM_RESULT[r.result] || r.result)}<div>${what}</div>${doc ? note : ''}</li>`;
  }).join('');
  return `<div class="form-hint">Итог загрузки: ${esc(primUploadSummary(res))}.</div><ul class="prim-results">${rows}</ul>`;
}

export async function viewPrimary(container, opts = {}) {
  const alive = typeof opts.alive === 'function' ? opts.alive : () => true;
  const companies = (Array.isArray(opts.companies) ? opts.companies : []).filter((c) => c.is_active !== false);
  const filterCompany = opts.companyFilter && opts.companyFilter !== 'all' ? opts.companyFilter : '';
  const state = { status: '', q: '', from: '', to: '', exported: '', offset: 0, limit: 50, selected: new Set() };
  let req = 0;
  container.innerHTML = `
    <div class="glass card">
      <div class="card-title">📑 Первичка: электронные УПД и счета-фактуры</div>
      <p class="form-hint">Загрузите файлы из системы ЭДО (Диадок, СБИС, Такском и другие): XML или ZIP-архив.
        Программа прочитает реквизиты и строки и проверит то, на чём чаще всего теряют вычет: обязательные
        реквизиты, ИНН и КПП, арифметику, ставку НДС (с 2026 года — 22 %) и формат 5.03 (с 01.04.2026 —
        единственный в ЭДО), а XML 5.03 — ещё и по официальной схеме ФНС. Подписи и печатные формы из архива
        пропускаются, коды маркировки не сохраняются.</p>
      <p class="form-hint">Бумажный УПД или счёт-фактура? Перетащите PDF, скан или фото (JPG, PNG, TIFF) —
        программа распознает реквизиты на своём сервере, без облаков, а вы сверите их перед сохранением.</p>
      ${opts.isAdmin ? `<label class="field prim-company"><span>Компания</span><select id="prim-company">
        <option value="">Определить по ИНН покупателя</option>
        ${companies.map((c) => `<option value="${esc(c.id)}" ${filterCompany === c.id ? 'selected' : ''}>${esc(c.short_name || c.name)}</option>`).join('')}
      </select></label>` : ''}
      <div class="dropzone" id="prim-drop" role="button" tabindex="0" aria-label="Выбрать файлы УПД">
        <span class="big-ico" aria-hidden="true">📥</span>Перетащите сюда XML, ZIP, PDF, скан или фото — или нажмите, чтобы выбрать
      </div>
      <input type="file" id="prim-file" accept=".xml,.zip,.pdf,.jpg,.jpeg,.png,.tif,.tiff" multiple class="hidden">
      <div class="prim-tools"><button type="button" class="btn btn-sm" id="prim-manual">✍️ Ввести вручную</button>
        <span class="form-hint" id="prim-ocr" aria-live="polite"></span></div>
      <div id="prim-results" aria-live="polite"></div>
    </div>
    <div class="glass card" id="prim-mail"><div class="skeleton" style="height:60px"></div></div>
    <div class="glass card">
      <div class="card-title">Реестр документов <span class="spacer"></span>
        <span class="form-hint" id="prim-picked" aria-live="polite"></span>
        <button type="button" class="btn btn-sm" id="prim-export" title="Файлы УПД или EnterpriseData для 1С">⬇ В 1С</button>
        <button type="button" class="btn btn-sm" id="prim-refresh">Обновить</button></div>
      <div style="display:flex;gap:8px;flex-wrap:wrap;align-items:flex-end;margin:6px 0 10px">
        <label class="field"><span>Проверка</span><select id="prim-status"><option value="">Все</option>
          ${PRIMARY_STATUSES.map((s) => `<option value="${s.code}">${esc(s.title)}</option>`).join('')}</select></label>
        <label class="field"><span>Поиск</span><input id="prim-q" type="search" maxlength="100"
          placeholder="номер, продавец, ИНН"></label>
        <label class="field"><span>С даты</span><input id="prim-from" type="date"></label>
        <label class="field"><span>По дату</span><input id="prim-to" type="date"></label>
        <label class="field"><span>1С</span><select id="prim-exported"><option value="">Все</option>
          <option value="false">Не выгружены</option><option value="true">Выгружены</option></select></label>
      </div>
      <div id="prim-kpi" class="form-hint"></div>
      <div id="prim-out"><div class="skeleton" style="height:120px"></div></div>
    </div>`;
  const $ = (sel) => container.querySelector(sel);
  const out = $('#prim-out');

  async function load() {
    const current = ++req;
    const params = new URLSearchParams({ limit: String(state.limit), offset: String(state.offset) });
    if (state.status) params.set('status', state.status);
    if (state.q) params.set('q', state.q);
    if (state.from) params.set('date_from', state.from);
    if (state.to) params.set('date_to', state.to);
    if (state.exported) params.set('exported', state.exported);
    if (opts.isAdmin && filterCompany) params.set('company_id', filterCompany);
    try {
      const data = await api.get(`/api/v1/primary?${params}`);
      if (current !== req || !alive() || !container.isConnected) return;
      const s = data.summary || {};
      $('#prim-kpi').textContent = `Без замечаний: ${s.ok || 0} · с замечаниями: ${s.warnings || 0} · с ошибками: ${s.errors || 0}`;
      const total = Number(data.total) || 0;
      out.innerHTML = `<div class="table-wrap"><table class="data"><thead><tr>
        <th><input type="checkbox" id="prim-pick-all" aria-label="Отметить все документы на странице"></th><th>Дата</th><th>Номер</th>
        <th>Продавец</th><th class="num">Сумма с НДС</th><th class="num">НДС</th><th>Проверка</th><th>1С</th><th></th></tr></thead>
        <tbody>${primRowsHtml(data.items, state.selected)}</tbody></table></div>
        <div style="display:flex;gap:8px;align-items:center;margin-top:8px">
          <button type="button" class="btn btn-sm" id="prim-prev" ${state.offset <= 0 ? 'disabled' : ''}>← Назад</button>
          <span class="form-hint">Показано ${Math.min(total, state.offset + 1)}–${Math.min(total, state.offset + state.limit)} из ${total}</span>
          <button type="button" class="btn btn-sm" id="prim-next" ${state.offset + state.limit >= total ? 'disabled' : ''}>Вперёд →</button>
        </div>`;
      out.querySelectorAll('.prim-open').forEach((b) => { b.onclick = () => openCard(b.dataset.id); });
      const picks = [...out.querySelectorAll('.prim-pick')];
      picks.forEach((box) => {
        box.onchange = () => { if (box.checked) state.selected.add(box.dataset.id); else state.selected.delete(box.dataset.id); showPicked(); };
      });
      const all = $('#prim-pick-all');
      if (all) {
        all.checked = picks.length > 0 && picks.every((box) => box.checked);
        all.onchange = () => { picks.forEach((box) => { box.checked = all.checked; box.onchange(); }); };
      }
      showPicked();
      $('#prim-prev').onclick = () => { state.offset = Math.max(0, state.offset - state.limit); load(); };
      $('#prim-next').onclick = () => { state.offset += state.limit; load(); };
    } catch (e) {
      if (current !== req || !container.isConnected) return;
      out.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function openCard(id) {
    let d;
    try { d = await api.get(`/api/v1/primary/${encodeURIComponent(id)}`); }
    catch (e) { toast(e.message, 'err'); return; }
    const { slot, close } = openModal(`
      <h3 class="modal-title">${esc(primTitle(d))}</h3>
      <p class="form-hint">Источник: ${esc(primSourceLabel(d.source))} · формат ${esc(d.format_version || '—')} · функция ${esc(d.function || '—')} · файл ${esc(d.file_name || '—')}</p>
      <div class="grid cards-2-even"><div><b>Продавец</b><div>${primParty(d.seller)}</div></div>
        <div><b>Покупатель</b><div>${primParty(d.buyer)}</div></div></div>
      <p>Без НДС: <b>${primMoney(d.total_without_vat)}</b> · НДС: <b>${primMoney(d.vat_total)}</b> ·
        с НДС: <b>${primMoney(d.total_with_vat)}</b> ${primStatusChip(d.status)}</p>
      <h4>Проверка</h4>${primIssuesHtml(d.issues)}
      <h4>Строки</h4>${primLinesHtml(d.lines)}
      <p class="form-hint">1С: ${d.exported_at ? `выгружен ${esc(primExportedLabel(d.exported_at).replace('✓ ', ''))}` : 'ещё не выгружен'}.
        Файл УПД загружается в 1С кнопкой «Загрузить → Из файла» в разделе «Покупки → Поступление (акты, накладные, УПД)».</p>
      <div class="modal-actions"><button type="button" class="btn" id="prim-card-1c">⬇ Файл для 1С</button>
        <button type="button" class="btn btn-danger" id="prim-del">Удалить документ</button></div>`);
    if (slot) slot.classList.add('prim-modal');   // шире обычного окна: строки документа — 8 колонок
    const file1c = document.getElementById('prim-card-1c');
    if (file1c) {
      file1c.onclick = async () => {
        try {
          const { blob, filename } = await api.download('/api/v1/primary/export',
            primExportBody({ ids: [id], format: 'upd', includeErrors: true }));
          primDownload(blob, filename);
          toast('Файл для 1С скачан — документ отмечен «выгружен»', 'ok');
          if (typeof close === 'function') close();
          load();
        } catch (e) { toast(e.message, 'err'); }
      };
    }
    const del = document.getElementById('prim-del');
    if (del) {
      del.onclick = async () => {
        if (!window.confirm('Удалить документ из реестра? Файл в системе ЭДО не затрагивается.')) return;
        try {
          await api.del(`/api/v1/primary/${encodeURIComponent(id)}`);
          toast('Документ удалён', 'ok');
          if (typeof close === 'function') close();
          load();
        } catch (e) { toast(e.message, 'err'); }
      };
    }
  }

  function showPicked() {
    const label = $('#prim-picked');
    if (label) label.textContent = state.selected.size ? `Отмечено: ${state.selected.size}` : '';
  }

  // v1.70.96 (T-60): выгрузка в 1С — сначала проверка (что уйдёт и почему пропущено), затем скачивание
  async function openExport() {
    const ids = [...state.selected];
    const period = [state.from && `с ${primDay(state.from)}`, state.to && `по ${primDay(state.to)}`].filter(Boolean).join(' ');
    const what = ids.length ? `Отмечено документов: ${ids.length}.`
      : `Новые документы — ещё не выгруженные${period ? ` (${period})` : ''}. Чтобы выгрузить определённые, отметьте их в реестре.`;
    const { slot, close } = openModal(`
      <h3 class="modal-title">⬇ Выгрузка в 1С</h3>
      <p class="form-hint">${esc(what)}</p>
      <fieldset class="prim-export-formats"><legend>Формат</legend>
        ${PRIM_EXPORT_FORMATS.map((f, i) => `<label class="prim-export-format"><input type="radio" name="prim-exp-format"
          id="prim-exp-${f.code}" value="${f.code}" ${i === 0 ? 'checked' : ''}><span><b>${esc(f.title)}</b>
          <span class="form-hint">${esc(f.hint)}</span></span></label>`).join('')}
      </fieldset>
      <label class="prim-export-format"><input type="checkbox" id="prim-exp-errors"><span>и документы с ошибками проверки</span></label>
      <div id="prim-exp-check" aria-live="polite"><div class="skeleton" style="height:48px"></div></div>
      <div class="modal-actions"><button type="button" class="btn btn-primary" id="prim-exp-go" disabled>Скачать</button></div>`);
    if (!slot) return;
    const get = (id) => slot.querySelector(`#${id}`);
    const format = () => (PRIM_EXPORT_FORMATS.find((f) => get(`prim-exp-${f.code}`)?.checked) || PRIM_EXPORT_FORMATS[0]).code;
    const body = () => primExportBody({ ids, format: format(), includeErrors: get('prim-exp-errors').checked,
      companyId: opts.isAdmin ? filterCompany : '', from: state.from, to: state.to });
    let seq = 0;
    let ready = 0;
    const refresh = async () => {
      const mine = ++seq;
      const go = get('prim-exp-go');
      go.disabled = true;
      try {
        const check = await api.post('/api/v1/primary/export/check', body());
        if (mine !== seq) return;
        ready = Number(check.count) || 0;
        get('prim-exp-check').innerHTML = primExportSummaryHtml(check);
        go.disabled = !ready;
      } catch (e) {
        if (mine === seq) get('prim-exp-check').innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
      }
    };
    PRIM_EXPORT_FORMATS.forEach((f) => { const radio = get(`prim-exp-${f.code}`); if (radio) radio.onchange = refresh; });
    get('prim-exp-errors').onchange = refresh;
    get('prim-exp-go').onclick = async () => {
      const go = get('prim-exp-go');
      go.disabled = true;
      try {
        const { blob, filename } = await api.download('/api/v1/primary/export', body());
        primDownload(blob, filename);
        toast(`Выгружено документов: ${ready} — они отмечены «выгружен»`, 'ok');
        state.selected.clear();
        if (typeof close === 'function') close();
        load();
      } catch (e) { go.disabled = false; toast(e.message, 'err'); }
    };
    await refresh();
  }

  const companyChoice = () => { const sel = $('#prim-company'); return sel && sel.value ? sel.value : ''; };

  async function loadOcr() {
    const box = $('#prim-ocr');
    try {
      const st = await api.get('/api/v1/primary/ocr');
      if (!container.isConnected) return;
      box.innerHTML = primOcrStatusHtml(st, opts.isAdmin);
      const btn = box.querySelector('#prim-ocr-models');
      if (btn) {
        btn.onclick = async () => {
          btn.disabled = true;
          btn.textContent = 'Скачиваю…';
          try {
            const res = await api.post('/api/v1/primary/ocr/models', {});
            toast(res.ready ? 'Модели скачаны — сканы и фото распознаются' : 'Модели скачаны', 'ok');
          } catch (e) { toast(e.message, 'err'); }
          loadOcr();
        };
      }
    } catch { box.innerHTML = ''; }
  }

  // extra (v1.70.104, черновик из почты): confirmUrl — куда сохранять, fileUrl и previewUrl — файл письма,
  // mail — от кого и тема, skipLabel и onSkip — «Не нужен» вместо «Пропустить»
  function review(rec, file, extra = {}) {
    return new Promise((resolve) => {
      let result = 'closed';
      const isImage = file && /^image\//.test(file.type || '');
      const blobUrl = isImage && typeof URL !== 'undefined' && URL.createObjectURL ? URL.createObjectURL(file) : '';
      const { slot, close } = openModal(primReviewHtml(rec, { previewUrl: extra.previewUrl || rec.preview || blobUrl,
        fileUrl: extra.fileUrl, mail: extra.mail, skipLabel: extra.skipLabel }), {
        onClose: () => { if (blobUrl && URL.revokeObjectURL) URL.revokeObjectURL(blobUrl); resolve(result); },
      });
      if (!slot) { resolve(result); return; }
      slot.classList.add('prim-modal');
      const get = (id) => slot.querySelector(`#${id}`);
      const zoom = get('prim-zoom');
      if (zoom) {
        zoom.onclick = () => {
          const box = slot.querySelector('.prim-review');
          const on = !box.classList.contains('prim-zoom');
          box.classList.toggle('prim-zoom', on);
          zoom.setAttribute('aria-pressed', String(on));
          zoom.textContent = on ? '↩ Обычный размер' : '🔍 Крупнее';
        };
      }
      const check = () => {
        const checks = primDraftChecks(primReadDraft(get, rec));
        const out = get('prim-check');
        if (out) out.innerHTML = primDraftChecksHtml(checks);
        const save = get('prim-save');
        if (save) save.disabled = checks.some((c) => c.level === 'error' && /Укажите/.test(c.message));
        return checks;
      };
      ['seller', 'buyer'].forEach((role) => {
        const btn = get(`prim-hint-${role}`);
        const h = rec.hints && rec.hints[role];
        if (btn && h) {
          btn.onclick = () => {
            get(`prim-f-${role}-name`).value = h.name;
            if (h.kpp) get(`prim-f-${role}-kpp`).value = h.kpp;
            check();
          };
        }
      });
      slot.oninput = check;
      slot.onchange = check;
      check();
      const skip = get('prim-skip');
      if (skip) {
        skip.onclick = async () => {
          if (typeof extra.onSkip === 'function') {
            try { await extra.onSkip(); } catch (e) { toast(e.message, 'err'); return; }
          }
          result = 'skipped';
          close();
        };
      }
      get('prim-save').onclick = async () => {
        const draft = primReadDraft(get, rec);
        const payload = { source: rec.source, token: rec.token || '', file_name: rec.file_name || '',
                          file_sha256: rec.file_sha256 || '', draft };
        const company = companyChoice() || rec.company_id;
        if (company) payload.company_id = company;
        try {
          const res = extra.confirmUrl
            ? await api.post(extra.confirmUrl, rec.company_id ? { draft, company_id: rec.company_id } : { draft })
            : await api.post('/api/v1/primary/confirm', payload);
          toast(res.result === 'duplicate' ? 'Такой документ уже есть в реестре'
            : `Сохранено: ${res.document ? primTitle(res.document) : 'документ'}`, res.result === 'duplicate' ? 'err' : 'ok');
          result = res.result === 'duplicate' ? 'duplicate' : 'saved';
          close();
        } catch (e) { toast(e.message, 'err'); }
      };
    });
  }

  async function recognizeOne(file, box) {
    const line = document.createElement('div');
    line.className = 'form-hint';
    line.textContent = `«${file.name}»: распознаю…`;
    box.appendChild(line);
    const form = new FormData();
    form.append('file', file);
    if (companyChoice()) form.append('company_id', companyChoice());
    let rec;
    try {
      rec = await api.post('/api/v1/primary/recognize', form, { retries: 1 });
    } catch (e) {
      line.className = 'form-error';
      line.textContent = `«${file.name}»: ${e.message}`;
      return;
    }
    if (!container.isConnected) return;
    const result = await review(rec, file);
    line.textContent = `«${file.name}»: ${{ saved: 'сохранён в реестр', duplicate: 'уже был в реестре',
      skipped: 'пропущен', closed: 'окно закрыто — не сохранён' }[result]}`;
    if (result === 'saved') { state.offset = 0; load(); loadOcr(); }
  }

  async function upload(files) {
    const { structured, printed } = primSplitFiles(files);
    if (!structured.length && !printed.length) return;
    const box = $('#prim-results');
    box.innerHTML = '';
    if (structured.length) {
      const form = new FormData();
      structured.forEach((f) => form.append('files', f));
      if (companyChoice()) form.append('company_id', companyChoice());
      box.innerHTML = '<div class="skeleton" style="height:60px"></div>';
      try {
        const res = await api.post('/api/v1/primary/upload', form);
        if (!container.isConnected) return;
        box.innerHTML = primResultsHtml(res);
        toast(`Первичка: ${primUploadSummary(res)}`, res.rejected && !res.created ? 'err' : 'ok');
        state.offset = 0;
        load();
      } catch (e) {
        box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
      }
    }
    for (const file of printed) {
      if (!container.isConnected) return;
      await recognizeOne(file, box);     // по одному: сервер распознаёт документы по очереди
    }
  }

  async function manual() {
    const rec = { source: 'manual', draft: { function: 'СЧФДОП', seller: {}, buyer: {}, totals: {}, lines: [] },
                  found: [], uncertain: [], hints: {} };
    const result = await review(rec, null);
    if (result === 'saved') { state.offset = 0; load(); }
  }

  const drop = $('#prim-drop');
  const input = $('#prim-file');
  drop.onclick = () => input.click();
  drop.onkeydown = (e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); input.click(); } };
  drop.ondragover = (e) => { e.preventDefault(); drop.classList.add('dragover'); };
  drop.ondragleave = () => drop.classList.remove('dragover');
  drop.ondrop = (e) => { e.preventDefault(); drop.classList.remove('dragover'); upload(e.dataTransfer && e.dataTransfer.files); };
  input.onchange = () => { upload(input.files); input.value = ''; };
  $('#prim-manual').onclick = () => manual();
  $('#prim-refresh').onclick = () => load();
  $('#prim-status').onchange = (e) => { state.status = e.target.value; state.offset = 0; load(); };
  let timer = null;
  $('#prim-q').oninput = (e) => {
    clearTimeout(timer);
    timer = setTimeout(() => { state.q = e.target.value.trim(); state.offset = 0; load(); }, 300);
  };
  $('#prim-from').onchange = (e) => { state.from = e.target.value; state.offset = 0; load(); };
  $('#prim-to').onchange = (e) => { state.to = e.target.value; state.offset = 0; load(); };
  $('#prim-exported').onchange = (e) => { state.exported = e.target.value; state.offset = 0; load(); };
  $('#prim-export').onclick = () => openExport();
  // ---------------------------------------------------------------- v1.70.104 (T-63): из почты
  const mailQuery = () => (opts.isAdmin && filterCompany ? `?company_id=${encodeURIComponent(filterCompany)}` : '');

  async function loadMail() {
    const box = $('#prim-mail');
    if (!box) return;
    try {
      const data = await api.get(`/api/v1/primary/inbox${mailQuery()}`);
      if (!container.isConnected) return;
      box.innerHTML = mailInboxHtml(data, { isAdmin: opts.isAdmin });
      box.querySelectorAll('.mail-review').forEach((b) => { b.onclick = () => reviewMail(b.dataset.id); });
      box.querySelectorAll('.mail-dismiss').forEach((b) => { b.onclick = () => dismissMail(b.dataset.id); });
      box.querySelector('#mail-refresh').onclick = () => loadMail();
      box.querySelector('#mail-setup').onclick = () => openMailSetup();
    } catch (e) {
      if (container.isConnected) box.innerHTML = `<div class="form-error">${esc(e.message)}</div>`;
    }
  }

  async function reviewMail(id) {
    const base = `/api/v1/primary/inbox/${encodeURIComponent(id)}`;
    let rec;
    try { rec = await api.get(base); } catch (e) { toast(e.message, 'err'); loadMail(); return; }
    let fileUrl = '';
    if (rec.file_url && typeof URL !== 'undefined' && URL.createObjectURL) {
      try { fileUrl = URL.createObjectURL((await api.download(rec.file_url)).blob); } catch { fileUrl = ''; }
    }
    const previewUrl = fileUrl && /^image\/(jpeg|png)$/.test(rec.media_type || '') ? fileUrl : '';
    const mail = [rec.sender, rec.subject ? `«${rec.subject}»` : ''].filter(Boolean).join(', ');
    const result = await review(rec, null, { confirmUrl: `${base}/confirm`, fileUrl, previewUrl, mail,
      skipLabel: 'Не нужен', onSkip: () => api.post(`${base}/dismiss`, {}) });
    if (fileUrl && URL.revokeObjectURL) URL.revokeObjectURL(fileUrl);
    if (result !== 'closed') loadMail();
    if (result === 'saved') { state.offset = 0; load(); }
  }

  async function dismissMail(id) {
    if (!window.confirm('Убрать вложение из сверки? Файл удалится из программы, письмо в ящике останется.')) return;
    try {
      await api.post(`/api/v1/primary/inbox/${encodeURIComponent(id)}/dismiss`, {});
      toast('Убрано из сверки', 'ok', '📬');
      loadMail();
    } catch (e) { toast(e.message, 'err'); }
  }

  function openMailSetup() {
    const companyId = opts.isAdmin ? (companyChoice() || filterCompany) : '';
    if (opts.isAdmin && !companyId) {
      toast('Выберите компанию в поле «Компания» выше — у каждой компании свой ящик', 'err', '📬');
      return;
    }
    const { slot, close } = openModal(`<div class="info-callout" id="prim-mail-setup"></div>
      <div class="modal-actions"><button type="button" class="btn" data-close>Закрыть</button></div>`, { onClose: () => loadMail() });
    if (!slot) return;
    slot.querySelector('[data-close]').onclick = close;
    mailConnectPanel(slot.querySelector('#prim-mail-setup'), companyId, {
      onSynced: () => { state.offset = 0; load(); } });
  }

  const listed = load();          // реестр — первым запросом, состояние распознавания — следом, не дожидаясь
  loadOcr();
  loadMail();
  await listed;
}
