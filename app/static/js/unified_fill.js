// ======================================================================
// Ямастер Чек — «Единая база», шаг 4в (T-39, v1.70.91): данные единой записи
// в карточке чека. Решение владельца Q-28 — вариант «в»: компания видит данные
// единой записи, которых нет в её копии (например, у чека по QR — продавца,
// кассира и позиции из Чек-Пула), с пометкой «из единой записи». В выгрузку в
// 1С они попадают, только если бухгалтер перенесёт их в свой чек кнопкой
// «Подставить» в форме правки и сохранит. Источник — блок unified_fill ответа
// GET /api/v1/receipts/{id} и списка (app/unified/read.py).
// ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
// ======================================================================
import { esc, fmtSum } from './ui.js';

export const FILL_FIELDS = [
  ['merchant_name', 'Продавец'],
  ['merchant_inn', 'ИНН продавца'],
  ['merchant_address', 'Адрес расчётов'],
  ['cashier', 'Кассир'],
];

const MARK_TITLE = 'Из единой записи: этих данных нет в копии вашей компании; '
  + 'в выгрузку в 1С они не попадают, пока их не перенесут в чек';

export function fillMark() {
  return `<span class="uf-mark" title="${MARK_TITLE}">из единой записи</span>`;
}

/** Поля, которые единая запись знает, а копия — нет: { поле: значение }. */
export function fillFields(r) {
  const fields = (r && r.unified_fill && r.unified_fill.fields) || {};
  const out = {};
  for (const [field] of FILL_FIELDS) {
    if (!(r && r[field]) && fields[field]) out[field] = fields[field];
  }
  return out;
}

/** Позиции единой записи — только если у копии своих нет. */
export function fillItems(r) {
  const items = r && r.unified_fill && r.unified_fill.items;
  return Array.isArray(items) && !((r.items || []).length) ? items : [];
}

export function hasFill(r) {
  return Object.keys(fillFields(r)).length > 0 || fillItems(r).length > 0;
}

/** Строки <dt>/<dd> продавца: своё значение — как есть, из единой записи — с пометкой. */
export function sellerRows(r) {
  const fill = fillFields(r);
  return FILL_FIELDS.map(([field, label]) => {
    if (r && r[field]) return `<dt>${label}</dt><dd>${esc(r[field])}</dd>`;
    if (fill[field]) return `<dt>${label}</dt><dd>${esc(fill[field])} ${fillMark()}</dd>`;
    return '';
  }).join('');
}

/** Таблица позиций из единой записи (у копии позиций нет) или пустая строка. */
export function fillItemsTable(r) {
  const items = fillItems(r);
  if (!items.length) return '';
  return `<div class="card-title">Позиции чека (${items.length}) ${fillMark()}</div>
    <table class="data uf-items" style="min-width:0"><thead><tr><th>Наименование</th><th>Кол.</th><th>Цена</th><th>Сумма</th></tr></thead>
    <tbody>${items.map((it) => `<tr style="cursor:default"><td>${esc(it.name)}</td><td>${esc(it.quantity)}</td>
      <td class="cell-sum">${fmtSum(it.price)}</td><td class="cell-sum">${fmtSum(it.total)}</td></tr>`).join('')}</tbody></table>`;
}

/** Подсказка в форме правки: что можно перенести из единой записи, и кнопка «Подставить». */
export function fillCallout(r) {
  if (!hasFill(r)) return '';
  const fields = fillFields(r);
  const parts = FILL_FIELDS.filter(([field]) => fields[field]).map(([, label]) => label.toLowerCase());
  const items = fillItems(r);
  if (items.length) parts.push(`позиции (${items.length})`);
  return `<div class="info-callout uf-callout" id="er-uf" style="margin-bottom:12px">
    🔗 В единой записи есть данные, которых нет в вашем чеке: ${esc(parts.join(', '))}.
    В выгрузку в 1С они попадут, только если перенести их в чек и сохранить.
    <button type="button" class="btn btn-sm" id="er-uf-apply" style="margin-left:8px">Подставить</button></div>`;
}

/**
 * «Подставить»: заполнить только пустые поля формы и позиции (если их нет).
 * inputs — { поле: <input> }; addItem(it) — добавить строку позиции в редактор.
 * Возвращает список перенесённого — для подсказки пользователю.
 */
export function applyFill(r, inputs, addItem) {
  const done = [];
  const fields = fillFields(r);
  for (const [field, label] of FILL_FIELDS) {
    const input = inputs[field];
    if (fields[field] && input && !String(input.value || '').trim()) {
      input.value = fields[field];
      done.push(label);
    }
  }
  const items = fillItems(r);
  if (items.length && typeof addItem === 'function') {
    items.forEach((it) => addItem({ name: it.name, quantity: it.quantity, price: it.price, total: it.total }));
    done.push(`позиции (${items.length})`);
  }
  return done;
}
