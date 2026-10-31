// ======================================================================
// Ямастер Чек — отрисовка полной карточки чека (v1.70.72)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
//
// Модуль нарочно без состояния: принимает документ чека (ответ
// /api/v1/receipts/{id}/card) и возвращает HTML. Так его можно
// проверять юнит-тестами, не поднимая браузер.
// ======================================================================
import { esc, fmtSum, fmtDate, statusLabel } from './ui.js';

function money0(v) {
  return (Number(v) || 0).toLocaleString('ru-RU',
    { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function qtyText(v) {
  const n = Number(v) || 0;
  return Number.isInteger(n) ? String(n) : n.toLocaleString('ru-RU',
    { maximumFractionDigits: 3 });
}

// --- Вид бумажного чека (как в магазине) ----------------------------------
function shopReceiptHTML(d) {
  const s = d.seller || {};
  const k = d.kkt || {};
  const m = d.money || {};
  const t = d.tax || {};
  const items = d.items || [];
  const line = (a, b) => `<div class="ticket-line"><span>${esc(a)}</span><span>${esc(b)}</span></div>`;
  const nds = (t.nds || []).filter(x => Number(x.sum) > 0)
    .map(x => line(`НДС ${x.rate_name}`, money0(x.sum))).join('');
  const pays = [];
  if (Number(m.cash) > 0) pays.push(line('НАЛИЧНЫМИ', money0(m.cash)));
  if (Number(m.ecash) > 0) pays.push(line('БЕЗНАЛИЧНЫМИ', money0(m.ecash)));
  if (Number(m.prepaid) > 0) pays.push(line('ПРЕДОПЛАТА', money0(m.prepaid)));
  if (Number(m.credit) > 0) pays.push(line('КРЕДИТ', money0(m.credit)));
  if (Number(m.provision) > 0) pays.push(line('ВСТРЕЧНОЕ ПРЕДОСТАВЛЕНИЕ', money0(m.provision)));
  return `
    <div class="ticket-wrap"><div class="ticket" id="shop-ticket">
      <div class="ticket-head">
        <div class="ticket-title">КАССОВЫЙ ЧЕК</div>
        <div class="ticket-sub">${esc(d.operation_name || 'Приход')}</div>
      </div>
      <div class="ticket-sep"></div>
      ${s.name ? `<div class="ticket-center ticket-title" style="font-size:12.5px">${esc(s.name)}</div>` : ''}
      ${s.inn ? `<div class="ticket-center ticket-sub">ИНН ${esc(s.inn)}</div>` : ''}
      ${s.address ? `<div class="ticket-center ticket-small">${esc(s.address)}</div>` : ''}
      ${s.place ? `<div class="ticket-center ticket-small">${esc(s.place)}</div>` : ''}
      <div class="ticket-sep"></div>
      <div class="ticket-small">${esc((d.date_time || '').replace('T', ' ').slice(0, 19))}</div>
      ${k.shift ? line('СМЕНА', k.shift) : ''}
      ${k.request ? line('ЧЕК ЗА СМЕНУ', k.request) : ''}
      ${d.cashier && d.cashier.name ? line('КАССИР', d.cashier.name) : ''}
      ${d.cashier && d.cashier.inn ? line('ИНН КАССИРА', d.cashier.inn) : ''}
      ${d.buyer && d.buyer.name ? line('ПОКУПАТЕЛЬ', d.buyer.name) : ''}
      ${d.buyer && d.buyer.contact ? line('КОНТАКТ ПОКУПАТЕЛЯ', d.buyer.contact) : ''}
      <div class="ticket-sep"></div>
      ${items.length ? items.map(it => `
        <div class="ticket-item">
          <div class="ticket-item-name">${esc(it.name)}</div>
          <div class="ticket-item-calc">
            <span>${qtyText(it.quantity)}${it.measure ? ' ' + esc(it.measure) : ''} × ${money0(it.price)}</span>
            <span>${money0(it.total)}</span>
          </div>
          ${it.nds_name ? `<div class="ticket-small">НДС ${esc(it.nds_name)}${Number(it.nds_sum) ? ': ' + money0(it.nds_sum) : ''}</div>` : ''}
          ${it.payment_type_name ? `<div class="ticket-small">${esc(it.payment_type_name)}</div>` : ''}
          ${it.product_type_name ? `<div class="ticket-small">${esc(it.product_type_name)}</div>` : ''}
        </div>`).join('')
      : `<div class="ticket-small">Позиции недоступны — полный документ ФНС не получен.</div>`}
      <div class="ticket-sep"></div>
      ${nds}
      <div class="ticket-total"><span>ИТОГ</span><span>${money0(m.total)}</span></div>
      <div class="ticket-sep"></div>
      ${pays.join('') || '<div class="ticket-small">Способ оплаты не указан</div>'}
      ${t.taxation_name ? `<div class="ticket-small">СНО: ${esc(t.taxation_name)}</div>` : ''}
      <div class="ticket-sep"></div>
      <div class="ticket-small">
        ${line('ФН', k.fn || '—')}
        ${line('ФД', k.fd || '—')}
        ${line('ФПД', k.fp || '—')}
        ${k.reg_id ? line('РН ККТ', k.reg_id) : ''}
        ${k.ffd ? line('ФФД', k.ffd) : ''}
      </div>
      ${d.receipt_note ? `<div class="ticket-small">Проверка ФНС: ${esc(statusLabel(d.receipt_note.fns_status))}</div>` : ''}
      ${(d.properties || []).map(p => line(p.name || 'РЕКВИЗИТ', p.value ?? '')).join('')}
      <div class="ticket-qr"><img src="/api/v1/receipts/${encodeURIComponent(d.receipt_id || '')}/qr.png" alt="QR-код чека"></div>
      <div class="ticket-center ticket-small">Копия по данным системы</div>
    </div></div>`;
}

function kvRows(pairs) {
  return `<dl class="fcard-grid">${pairs.filter(p => p && p[1] !== ''
    && p[1] !== null && p[1] !== undefined)
    .map(p => `<dt>${esc(p[0])}</dt><dd>${p[2] ? p[1] : esc(p[1])}</dd>`).join('')}</dl>`;
}

function fullCardDetailsHTML(d) {
  const s = d.seller || {};
  const k = d.kkt || {};
  const m = d.money || {};
  const t = d.tax || {};
  const reg = d.region || {};
  const ind = d.industry || {};
  const note = d.receipt_note || {};
  const industries = (ind.shares || []).map(x =>
    `${esc(x.group)} — ${fmtSum(x.sum)} (${x.count})`).join('; ');
  return `
    <div class="fcard-group">
      <div class="fcard-group-title">Продавец</div>
      ${kvRows([
        ['Наименование', s.name || '—'],
        ['ИНН', s.inn || '—'],
        ['КПП', s.kpp || ''],
        ['Адрес расчётов', s.address || '—'],
        ['Место расчётов', s.place || ''],
        ['Регион', `${reg.name || '—'}${reg.code ? ' (код ' + reg.code + ')' : ''}`],
        ['Город', reg.city || ''],
        ['Индекс', s.postal || ''],
        ['Как определён регион', s.region_source === 'address' ? 'по адресу места расчётов'
          : s.region_source === 'inn' ? 'по ИНН продавца' : 'из карточки чека'],
      ])}
    </div>
    <div class="fcard-group">
      <div class="fcard-group-title">Отрасль покупки</div>
      ${kvRows([
        ['Отрасль', ind.industry || '—'],
        ['Группа товаров', ind.group || ''],
        ['Распределение', industries || ''],
        ['Как определено', d.industry_source || 'по наименованиям позиций'],
      ])}
      <div class="fcard-note">Отрасль определяется по наименованиям позиций —
        ФНС её не передаёт. Если нужно иначе, её можно поправить в карточке чека.</div>
    </div>
    <div class="fcard-group">
      <div class="fcard-group-title">Расчёт</div>
      ${kvRows([
        ['Дата и время', (d.date_time || '').replace('T', ' ').slice(0, 19)],
        ['Признак расчёта', d.operation_name || '—'],
        ['Итог', money0(m.total) + ' ₽'],
        ['Наличные', Number(m.cash) ? money0(m.cash) + ' ₽' : ''],
        ['Безналичные', Number(m.ecash) ? money0(m.ecash) + ' ₽' : ''],
        ['Предоплата', Number(m.prepaid) ? money0(m.prepaid) + ' ₽' : ''],
        ['Кредит', Number(m.credit) ? money0(m.credit) + ' ₽' : ''],
        ['Способ оплаты', d.pay_type || m.pay_type || ''],
        ['Система налогообложения', d.taxation_name || t.taxation_name || ''],
        ['НДС по ставкам', (t.nds || []).map(x => `${x.rate_name}: ${money0(x.sum)}`).join('; ')],
      ])}
    </div>
    <div class="fcard-group">
      <div class="fcard-group-title">Касса и документ</div>
      ${kvRows([
        ['Регистрационный № ККТ', k.reg_id || '—'],
        ['ФН (накопитель)', k.fn || '—'],
        ['ФД (документ)', k.fd || '—'],
        ['ФПД (признак)', k.fp || '—'],
        ['Смена', k.shift ?? ''],
        ['Чек за смену', k.request ?? ''],
        ['Формат ФФД', k.ffd || ''],
        ['Кассир', (d.cashier || {}).name || '—'],
        ['ИНН кассира', (d.cashier || {}).inn || ''],
        ['ОФД', d.ofd_id || ''],
      ])}
    </div>
    ${(d.properties || []).length ? `<div class="fcard-group">
      <div class="fcard-group-title">Дополнительные реквизиты</div>
      ${kvRows((d.properties || []).map(p => [p.name || 'Реквизит', p.value || '']))}
    </div>` : ''}
    ${(d.industry_details || []).length ? `<div class="fcard-group">
      <div class="fcard-group-title">Отраслевой реквизит чека</div>
      ${kvRows((d.industry_details || []).map(p => [p.foiv_name || p.foiv,
        `${p.value || ''}${p.doc_number ? ' · документ ' + p.doc_number + ' от ' + (p.doc_date || '') : ''}`]))}
    </div>` : ''}
    <div class="fcard-group">
      <div class="fcard-group-title">Проверка</div>
      ${kvRows([
        ['Статус ФНС', statusLabel(note.fns_status)],
        ['Ответ ФНС', note.fns_message || '—'],
        ['Проверен', note.checked_at ? fmtDate(note.checked_at) : '—'],
        ['Источник данных', note.details_source || '—'],
        ['Кэш', `${note.cache_days || 30} дней — повторная проверка лимит не тратит`],
      ])}
    </div>`;
}

function cardEmpty(ico, text) {
  return `<div class="empty-state"><span class="big-ico">${ico}</span>
    <span class="form-hint">${esc(text)}</span></div>`;
}

function fullCardItemsHTML(d) {
  const items = d.items || [];
  if (!items.length) {
    return cardEmpty('📦', 'Позиции не получены: нужен полный документ ФНС');
  }
  return `<div class="table-wrap"><table class="data" style="min-width:0">
    <thead><tr><th>№</th><th>Наименование</th><th>Тип</th><th>Кол.</th>
      <th>Цена</th><th>Сумма</th><th>НДС</th><th>Группа</th></tr></thead>
    <tbody>${items.map(it => `<tr style="cursor:default">
      <td>${it.pos}</td>
      <td>${esc(it.name)}${(it.marking || []).length
        ? `<div class="form-hint">маркировка: ${esc((it.marking[0].gtin || '') + ' ' + (it.marking[0].serial || ''))}</div>` : ''}
        ${(it.supplier && it.supplier.name) ? `<div class="form-hint">поставщик: ${esc(it.supplier.name)}${it.supplier.inn ? ', ИНН ' + esc(it.supplier.inn) : ''}</div>` : ''}
        ${(it.industry_details || []).length ? `<div class="form-hint">ФОИВ ${esc(it.industry_details[0].foiv)}: ${esc(it.industry_details[0].value || '')}</div>` : ''}</td>
      <td>${esc(it.product_type_name || '—')}</td>
      <td>${qtyText(it.quantity)}${it.measure ? ' ' + esc(it.measure) : ''}</td>
      <td class="cell-sum">${fmtSum(it.price)}</td>
      <td class="cell-sum">${fmtSum(it.total)}</td>
      <td>${esc(it.nds_name || '—')}${Number(it.nds_sum) ? `<div class="form-hint">${fmtSum(it.nds_sum)}</div>` : ''}</td>
      <td>${esc(it.group || '—')}</td></tr>`).join('')}</tbody></table></div>`;
}

function fullCardRawHTML(card) {
  const d = card.document || {};
  const document = { ...d };
  delete document.raw; delete document.fns_answer; delete document.extra;
  const saved = card.saved_data_json || JSON.stringify({
    fns_answer: d.fns_answer || {}, extra: d.extra || {},
  }, null, 2);
  return `<div class="fcard-note">Карточка содержит нормализованные и вычисленные поля.
    Ниже отдельно показаны сохранённые данные. Исходный документ ФНС доступен
    в fns_answer.ticket, только если он был сохранён; ответ из кэша может содержать
    лишь служебные сведения. Сохранённые данные отображаются без потери точности больших чисел.</div>
    <h4>Сформированная карточка</h4>
    <pre class="fcard-json">${esc(JSON.stringify(document, null, 2))}</pre>
    <h4>Сохранённые данные</h4><pre class="fcard-json">${esc(saved)}</pre>`;
}

export { money0, qtyText, shopReceiptHTML, fullCardDetailsHTML,
         fullCardItemsHTML, fullCardRawHTML };
