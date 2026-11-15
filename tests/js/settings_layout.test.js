/* v1.72.36: раскладка страницы «Настройки» — чистые функции без DOM.
   Проверяют порядок карточек, широкие карточки, начало разделов,
   модель селекта-навигации и порционную выдачу длинных списков. */
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_ORDER, jumpModel, jumpValue, layoutPlan, orderOf, pageSlice, parseJumpValue,
} from '../../app/static/js/settings_layout.js';

describe('orderOf', () => {
  it('читает число из inline-стиля', () => {
    expect(orderOf('12')).toBe(12);
    expect(orderOf(12)).toBe(12);
    expect(orderOf('12;grid-column:1/-1')).toBe(12);
  });
  it('без порядка — в конец', () => {
    expect(orderOf(undefined)).toBe(DEFAULT_ORDER);
    expect(orderOf('')).toBe(DEFAULT_ORDER);
    expect(orderOf('abc')).toBe(DEFAULT_ORDER);
  });
});

describe('layoutPlan', () => {
  const card = (id, sec, order, extra = {}) => ({ id, sec, order, ...extra });

  it('пропускает скрытые ролью и безымянные карточки', () => {
    const plan = layoutPlan([
      card('a', 'data', 10),
      card('b', 'data', 11, { hidden: true }),
      { sec: 'data', order: 12 },
      null,
    ]);
    expect(plan.map((p) => p.id)).toEqual(['a']);
    expect(plan[0]).toEqual({ id: 'a', sec: 'data', order: 10, wide: false, sectionStart: true });
  });

  it('широкими считаются карточки с таблицами и графиками', () => {
    const plan = layoutPlan([
      card('table', 'data', 10, { hasTable: true }),
      card('chart', 'integr', 38, { hasChart: true }),
      card('plain', 'data', 11),
    ]);
    const by = Object.fromEntries(plan.map((p) => [p.id, p]));
    expect(by.table.wide).toBe(true);
    expect(by.chart.wide).toBe(true);
    expect(by.plain.wide).toBe(false);
  });

  it('разделы не перемешиваются, порядок внутри раздела — по order', () => {
    const plan = layoutPlan([
      card('checks-fns', 'checks', 20),
      card('data-upd', 'data', 11),
      card('checks-mail', 'checks', 23),
      card('data-backups', 'data', 10),
      card('device-pwa', 'device', 51),
      card('data-server', 'data', 13),
    ]);
    expect(plan.map((p) => p.id)).toEqual([
      'data-backups', 'data-upd', 'data-server', 'checks-fns', 'checks-mail', 'device-pwa',
    ]);
    expect(plan.filter((p) => p.sectionStart).map((p) => p.id))
      .toEqual(['data-backups', 'checks-fns', 'device-pwa']);
  });

  it('раздел, у которого раньше первая карточка, идёт первым', () => {
    const plan = layoutPlan([
      card('device', 'device', 48),
      card('data', 'data', 10),
    ]);
    expect(plan.map((p) => p.id)).toEqual(['data', 'device']);
  });

  it('на пустом входе возвращает пустой план', () => {
    expect(layoutPlan([])).toEqual([]);
    expect(layoutPlan(null)).toEqual([]);
  });
});

describe('навигация селектом', () => {
  it('строит пункты по непустым группам и карточкам', () => {
    const model = jumpModel([
      { key: 'data', title: 'Данные и восстановление', items: [
        { id: 'st-backups', label: 'Резервные копии', ico: '💾' },
      ] },
      { key: 'checks', title: 'Проверка чеков', items: [] },
      { key: '', title: 'Без ключа', items: [{ id: 'x', label: 'X' }] },
    ]);
    expect(model).toEqual([{
      key: 'data',
      title: 'Данные и восстановление',
      items: [{ value: 'data|st-backups', label: '💾 Резервные копии' }],
    }]);
  });

  it('значение селекта и обратный разбор согласованы', () => {
    expect(jumpValue('checks', 'st-fns')).toBe('checks|st-fns');
    expect(parseJumpValue(jumpValue('checks', 'st-fns'))).toEqual({ group: 'checks', card: 'st-fns' });
    expect(jumpValue('', 'st-fns')).toBe('');
    expect(parseJumpValue('')).toEqual({ group: '', card: '' });
    expect(parseJumpValue('без-разделителя')).toEqual({ group: '', card: '' });
    expect(parseJumpValue('|st-fns')).toEqual({ group: '', card: '' });
    expect(parseJumpValue('checks|')).toEqual({ group: '', card: '' });
    expect(parseJumpValue('a|b|c')).toEqual({ group: 'a', card: 'b|c' });
  });
});

describe('pageSlice', () => {
  const rows = Array.from({ length: 18 }, (_, i) => i);

  it('делит список на видимую часть и остаток', () => {
    const page = pageSlice(rows, 10);
    expect(page.shown).toHaveLength(10);
    expect(page.rest).toHaveLength(8);
    expect(page.total).toBe(18);
    expect(page.hasMore).toBe(true);
  });

  it('короткий список не режется и не обещает продолжения', () => {
    const page = pageSlice([1, 2, 3], 10);
    expect(page.shown).toEqual([1, 2, 3]);
    expect(page.rest).toEqual([]);
    expect(page.hasMore).toBe(false);
  });

  it('некорректный лимит — показать всё; пустой вход безопасен', () => {
    expect(pageSlice(rows, 0).shown).toHaveLength(18);
    expect(pageSlice(rows, NaN).hasMore).toBe(false);
    expect(pageSlice(null, 10)).toEqual({ shown: [], rest: [], total: 0, hasMore: false });
  });
});
