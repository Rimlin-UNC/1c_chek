/* v1.70.72: юнит-тесты полной карточки чека (jsdom, без браузера).
   Проверяем то, что видит человек: вид бумажного чека, все реквизиты,
   таблицу товаров и экранирование внешних данных. */
import { describe, expect, it } from 'vitest';
import {
  money0, qtyText, shopReceiptHTML, fullCardDetailsHTML,
  fullCardItemsHTML, fullCardRawHTML,
} from '../../app/static/js/card.js';

const doc = {
  kind: 'kkt',
  date_time: '2026-10-01T09:08:00',
  operation: 1,
  operation_name: 'Приход',
  seller: {
    name: 'ООО «Тест-Магазин»', inn: '7801234567', kpp: '780101001',
    address: '198000, Санкт-Петербург, Невский пр., 1',
    place: 'Торговый зал', region_code: '78',
    region_name: 'Санкт-Петербург', city: 'Санкт-Петербург',
    postal: '198000', region_source: 'address',
  },
  kkt: {
    reg_id: '0001234567890123', fn: '9988776655443322', fd: '77133',
    fp: '1482926127', shift: 285, request: 12, ffd: '1.05',
  },
  money: { total: 890, cash: 0, ecash: 890, prepaid: 0, credit: 0,
    provision: 0, pay_type: 'безналичные' },
  tax: { taxation: 1, taxation_name: 'УСН (доходы)',
    nds: [{ rate: '20', rate_name: '20 %', sum: 148.34 }] },
  cashier: { name: 'Иванова М. П.', inn: '780100000001' },
  buyer: { name: '', contact: '' },
  items: [
    { pos: 1, name: 'Товар А', quantity: 1, price: 445, total: 445,
      nds: '20', nds_name: '20 %', nds_sum: 74.17, measure: 'шт',
      product_type: 1, product_type_name: 'Товар',
      payment_type: 4, payment_type_name: 'Полный расчёт',
      group: 'Прочее', industry: 'Прочие покупки', marking: [],
      agent: {}, supplier: {}, industry_details: [] },
    { pos: 2, name: 'Товар Б', quantity: 1, price: 445, total: 445,
      nds: '20', nds_name: '20 %', nds_sum: 74.17, measure: 'шт',
      product_type: 1, product_type_name: 'Товар',
      payment_type: 4, payment_type_name: 'Полный расчёт',
      group: 'Прочее', industry: 'Прочие покупки',
      marking: [{ gtin: '04601234567890', serial: 'ABC123' }],
      supplier: { name: 'ООО Поставщик', inn: '7801111111' },
      industry_details: [{ foiv: '020', foiv_name: 'Минпромторг России',
        value: 'RU-430302-0001' }] },
  ],
  properties: [{ name: 'Код заказа', value: 'G637' }],
  industry_details: [],
  industry: { industry: 'Прочие покупки', group: 'Прочее', shares: [
    { group: 'Прочее', industry: 'Прочие покупки', sum: 890, count: 2 }] },
  industry_source: 'по наименованиям позиций',
  region: { code: '78', name: 'Санкт-Петербург', city: 'Санкт-Петербург' },
  taxation_name: 'УСН (доходы)',
  pay_type: 'безналичные',
  receipt_note: { fns_status: 'valid', fns_message: 'Чек найден в ФНС',
    checked_at: '2026-10-31T12:00:00', details_source: 'fns', cache_days: 30 },
  extra: {},
  fns_answer: {},
};

describe('форматирование', () => {
  it('суммы — в рублях с копейками', () => {
    expect(money0(890)).toBe('890,00');
    // разделитель разрядов — неразрывный пробел, как в остальном кабинете
    expect(money0(1234.5)).toBe('1\u00a0234,50');
  });

  it('количество без лишних нулей', () => {
    expect(qtyText(1)).toBe('1');
    expect(qtyText(0.5)).toBe('0,5');
  });
});

describe('чек как в магазине', () => {
  const html = shopReceiptHTML({ ...doc, receipt_id: 'abc' });

  it('содержит шапку, продавца и ИНН', () => {
    expect(html).toContain('КАССОВЫЙ ЧЕК');
    expect(html).toContain('ООО «Тест-Магазин»');
    expect(html).toContain('ИНН 7801234567');
  });

  it('показывает позиции с количеством, ценой и суммой', () => {
    expect(html).toContain('Товар А');
    expect(html).toContain('1 шт × 445,00');
    expect(html).toContain('445,00');
  });

  it('итог, способ оплаты и СНО на месте', () => {
    expect(html).toContain('ИТОГ');
    expect(html).toContain('890,00');
    expect(html).toContain('БЕЗНАЛИЧНЫМИ');
    expect(html).toContain('СНО: УСН (доходы)');
  });

  it('фискальные реквизиты и QR напечатаны как на бумаге', () => {
    expect(html).toContain('ФН');
    expect(html).toContain('9988776655443322');
    expect(html).toContain('ФПД');
    expect(html).toContain('1482926127');
    expect(html).toContain('/receipts/abc/qr.png');
  });

  it('пустые деньги не рисуются пустыми строками', () => {
    // «БЕЗНАЛИЧНЫМИ» содержит «НАЛИЧНЫМИ» — ищем отдельную строку оплаты
    expect(html).not.toContain('>НАЛИЧНЫМИ<');
  });

  it('без позиций — честное объяснение, а не пустая таблица', () => {
    const noItems = shopReceiptHTML({ ...doc, items: [], receipt_id: 'x' });
    expect(noItems).toContain('Позиции недоступны');
  });

  it('внешние данные экранируются (XSS из наименования)', () => {
    const bad = shopReceiptHTML({ ...doc, seller: { name: '<img src=x onerror=alert(1)>' },
      items: [{ name: '<script>alert(1)</script>', quantity: 1, price: 1,
        total: 1, nds_name: '20 %' }], receipt_id: 'x' });
    expect(bad).not.toContain('<script>alert(1)</script>');
    expect(bad).toContain('&lt;script&gt;');
    // опасные символы обезврежены: тег не станет разметкой
    expect(bad).not.toContain('<img src=x');
    expect(bad).toContain('&lt;img src=x');
  });
});

describe('все реквизиты', () => {
  const html = fullCardDetailsHTML(doc);

  it('продавец, регион и как он определён', () => {
    expect(html).toContain('7801234567');
    expect(html).toContain('Санкт-Петербург');
    expect(html).toContain('код 78');
    expect(html).toContain('по адресу места расчётов');
  });

  it('отрасль и честная подпись источника', () => {
    expect(html).toContain('Прочие покупки');
    expect(html).toContain('ФНС её не передаёт');
  });

  it('касса, смена и кассир', () => {
    expect(html).toContain('0001234567890123');
    expect(html).toContain('Смена');
    expect(html).toContain('Иванова М. П.');
  });

  it('дополнительные реквизиты и статус проверки', () => {
    expect(html).toContain('Код заказа');
    expect(html).toContain('G637');
    expect(html).toContain('Действителен');
    expect(html).toContain('Чек найден в ФНС');
  });
});

describe('товары', () => {
  const html = fullCardItemsHTML(doc);

  it('тип предмета расчёта, единица и группа', () => {
    expect(html).toContain('Товар');
    expect(html).toContain('шт');
    expect(html).toContain('Прочее');
  });

  it('маркировка, поставщик и отраслевой реквизит видны', () => {
    expect(html).toContain('04601234567890');
    expect(html).toContain('ООО Поставщик');
    expect(html).toContain('ФОИВ 020');
  });

  it('без позиций — подсказка', () => {
    expect(fullCardItemsHTML({ items: [] })).toContain('Позиции не получены');
  });
});

describe('сырые данные', () => {
  it('сохранённая строка JSON сохраняет большие числа и не дублирует блоки', () => {
    const html = fullCardRawHTML({ document: { ...doc, extra: { message_fiscal_sign: 0 },
      fns_answer: { cached: true } }, saved_data_json: '{"extra":{"message_fiscal_sign":9297247158114454001}}' });
    expect(html).toContain('9297247158114454001');
    expect((html.match(/message_fiscal_sign/g) || []).length).toBe(1);
    expect(html).toContain('Исходный документ ФНС');
  });

  it('выводятся как JSON и тоже экранируются', () => {
    const html = fullCardRawHTML({ document: { ...doc, extra: { a: '<b>' } } });
    expect(html).toContain('<pre class="fcard-json">');
    expect(html).toContain('&lt;b&gt;');
  });
});
