// «Единая база», шаг 4в (T-39, v1.70.91): данные единой записи в карточке чека.
// Q-28 (в): своё значение копии — как есть, из единой записи — с пометкой;
// «Подставить» заполняет только пустые поля и позиции, если своих нет.
import { describe, expect, it } from 'vitest';
import { applyFill, fillCallout, fillFields, fillItems, fillItemsTable, hasFill, sellerRows }
  from '../../app/static/js/unified_fill.js';

const qrCopy = {
  merchant_name: 'Строймаркет', cashier: '', items: [],
  unified_fill: {
    fields: { cashier: 'Кассир <Пулов>', merchant_address: 'г. Пул, ул. Полная, 1' },
    items: [{ name: 'Цемент М500', quantity: 2, price: 450, total: 900 }],
  },
};

describe('unified_fill', () => {
  it('берёт из единой записи только то, чего нет в копии', () => {
    const r = { ...qrCopy, unified_fill: { fields: { ...qrCopy.unified_fill.fields, merchant_name: 'Другое' } } };
    expect(fillFields(r)).toEqual({ cashier: 'Кассир <Пулов>', merchant_address: 'г. Пул, ул. Полная, 1' });
  });

  it('своё — без пометки, из единой записи — с пометкой и экранированием', () => {
    const html = sellerRows(qrCopy);
    expect(html).toContain('<dt>Продавец</dt><dd>Строймаркет</dd>');
    expect(html).toContain('Кассир &lt;Пулов&gt; <span class="uf-mark"');
    expect(html).not.toContain('<Пулов>');
    expect(html.match(/uf-mark/g)).toHaveLength(2);
  });

  it('позиции единой записи — только если у копии своих нет', () => {
    expect(fillItems(qrCopy)).toHaveLength(1);
    expect(fillItems({ ...qrCopy, items: [{ name: 'своя' }] })).toEqual([]);
    expect(fillItemsTable(qrCopy)).toContain('Цемент М500');
    expect(fillItemsTable({ ...qrCopy, items: [{ name: 'своя' }] })).toBe('');
  });

  it('подсказка в форме перечисляет, что можно перенести, и даёт кнопку', () => {
    const html = fillCallout(qrCopy);
    expect(html).toContain('адрес расчётов, кассир, позиции (1)');
    expect(html).toContain('id="er-uf-apply"');
    expect(fillCallout({ merchant_name: 'x', items: [] })).toBe('');
    expect(hasFill({})).toBe(false);
  });

  it('«Подставить» заполняет только пустые поля и добавляет позиции', () => {
    const inputs = { cashier: { value: '' }, merchant_address: { value: 'своё' }, merchant_name: { value: '' } };
    const added = [];
    const done = applyFill(qrCopy, inputs, (it) => added.push(it));
    expect(inputs.cashier.value).toBe('Кассир <Пулов>');
    expect(inputs.merchant_address.value).toBe('своё');          // заполненное не трогаем
    expect(added).toEqual([{ name: 'Цемент М500', quantity: 2, price: 450, total: 900 }]);
    expect(done).toEqual(['Кассир', 'позиции (1)']);
    expect(applyFill(qrCopy, inputs, null)).toEqual([]);         // выгружен — позиции не трогаем
  });
});
