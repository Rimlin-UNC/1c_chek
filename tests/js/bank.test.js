/* v1.70.98 (T-45): экран «Банк: выписка» — чистые функции отрисовки и проверки ввода (Vitest, jsdom). */
import { describe, expect, it } from 'vitest';
import {
  bankDate, bankOpsHtml, bankRefundHtml, bankSettingsBody, bankSettingsHtml, bankStatementsHtml, bankUploadSummary,
} from '../../app/static/js/bank.js';
import { DREQ_KINDS } from '../../app/static/js/docreq.js';

const op = (extra = {}) => ({
  id: 'op1', date: '2026-10-06', number: '12', amount: 1200, kind: 'card', card_tail: '1234', card_owner: 'Сотрудник Картов',
  counterparty_name: 'ПАО Банк', counterparty_inn: '', purpose: 'Оплата товаров по карте *1234 <KRASKI>', status: 'open',
  match_note: '', decided_by: '', ...extra,
});

describe('банковская выписка: отрисовка (v1.70.98)', () => {
  it('дата и итог загрузки одной строкой', () => {
    expect(bankDate('2026-10-06')).toBe('06.10.2026');
    expect(bankDate('2026-10-06T12:00:00')).toBe('06.10.2026');
    expect(bankDate('')).toBe('—');
    const text = bankUploadSummary({ operations: 7, saved: 4, matched: 2, open: 2, duplicates: 1, requests: 2,
      kinds: { card: 2, transfer: 2, tax: 1, salary: 1, income: 1 } });
    expect(text).toBe('операций: 7 · нужны документы: 4 · документ найден: 2 · без документов: 2 · '
      + 'уже были в прошлых выписках: 1 · не требуют документов: налоги и взносы 1, зарплата 1, поступления 1 · '
      + 'в «Что нужно от клиента» добавлено: 2');
  });

  it('операции: действия по статусу, держатель карты, экранирование назначения', () => {
    const open = bankOpsHtml([op()], 'open');
    expect(open).toContain('💳 покупка по карте *1234');
    expect(open).toContain('карта: Сотрудник Картов');
    expect(open).toContain('data-action="ignore"');
    expect(open).toContain('&lt;KRASKI&gt;');
    expect(open).not.toContain('<KRASKI>');
    const matched = bankOpsHtml([op({ status: 'matched', match_note: 'чек на 1 200,00 ₽ от 06.10.2026' })], 'matched');
    expect(matched).toContain('↺ Отменить сопоставление');
    expect(matched).toContain('чек на 1 200,00 ₽ от 06.10.2026');
    const transfer = bankOpsHtml([op({ kind: 'transfer', card_tail: '', counterparty_name: 'ООО «Песок»', counterparty_inn: '7702345678', number: '14' })], 'open');
    expect(transfer).toContain('🏢 оплата поставщику');
    expect(transfer).toContain('платёж № 14');
    expect(transfer).toContain('ИНН 7702345678');
    expect(bankOpsHtml([], 'open')).toContain('Платежей без документов нет');
  });

  it('к возмещению: без выписки — подсказка, группы по сотрудникам с решениями', () => {
    expect(bankRefundHtml({ covered: false, employees: [] })).toContain('Загрузите выписку');
    expect(bankRefundHtml({ covered: true, employees: [] })).toContain('возмещать нечего');
    const html = bankRefundHtml({ covered: true, employees: [{ name: 'Сотрудник Картов', pending_total: 1700, paid_total: 0, items: [
      { receipt_id: 'r1', date: '2026-10-08T09:00:00', merchant: 'ООО «Гвозди»', payment: 'наличными', amount: 1700, total_sum: 2000,
        status: 'pending', note: '' },
      { receipt_id: 'r2', date: '2026-10-09T09:00:00', merchant: 'ИП Краски', payment: '', amount: 500, total_sum: 500,
        status: 'declined', note: 'из подотчётных' }] }] });
    expect(html).toContain('Сотрудник Картов');
    expect(html).toContain('data-status="paid"');
    expect(html).toContain('↺ Вернуть');
    expect(html).toContain('из подотчётных');
    expect(html).toContain('наличными');
  });

  it('выписки: период, счёт, кнопка удаления', () => {
    const html = bankStatementsHtml([{ id: 's1', date_from: '2026-10-01', date_to: '2026-10-31', account_tail: '0001',
      operations: 7, saved: 4, uploaded_by: 'Бухгалтер', created_at: '2026-11-01T10:00:00' }]);
    expect(html).toContain('01.10.2026 — 31.10.2026');
    expect(html).toContain('счёт …0001');
    expect(html).toContain('data-id="s1"');
    expect(bankStatementsHtml([])).toContain('Выписок пока нет');
  });

  it('настройки: карты — ровно 4 цифры, срок хранения 30–3650 дней', () => {
    document.body.innerHTML = bankSettingsHtml({ retention_days: 1095, cards: [{ tail: '1234', user_id: 'u1' }] },
      [{ id: 'u1', name: 'Сотрудник Картов' }]);
    const get = (id) => document.getElementById(id);
    expect(get('bank-s-count').value).toBe('3');
    expect(get('bank-s-user-0').value).toBe('u1');
    expect(bankSettingsBody(get, 'c1')).toEqual({ company_id: 'c1', retention_days: 1095, cards: [{ tail: '1234', user_id: 'u1' }] });
    get('bank-s-tail-1').value = '12';
    expect(bankSettingsBody(get, '').error).toBe('Карта — ровно 4 последние цифры номера');
    get('bank-s-tail-1').value = '';
    get('bank-s-days').value = '10';
    expect(bankSettingsBody(get, '').error).toBe('Срок хранения — от 30 до 3650 дней');
  });

  it('«Что нужно от клиента» знает новый вид запроса', () => {
    expect(DREQ_KINDS.payment).toBe('🏦 платёж без документов');
  });
});
