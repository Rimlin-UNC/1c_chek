/* v1.72.37: раскладка страницы «Настройки» — чистые функции без DOM.
   Проверяют порядок карточек, широкие карточки, начало разделов, модель
   горизонтальных табов (таб = раздел, панель = карточки раздела), выбор
   активного таба по маршруту, маску токена и порционную выдачу списков.
   Селект-навигация (jumpModel/jumpValue/parseJumpValue) удалена вместе с
   вертикальным меню — её тесты заменены на табы. */
import { describe, expect, it } from 'vitest';
import {
  DEFAULT_ORDER, layoutPlan, maskToken, orderOf, pageSlice, tabForRoute, tabsModel,
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

describe('tabsModel', () => {
  it('таб = раздел, панель = его карточки', () => {
    const model = tabsModel([
      { key: 'data', title: 'Данные и восстановление', icon: '🗄', items: [
        { id: 'st-backups', label: 'Резервные копии', ico: '💾' },
        { id: 'st-update', label: 'Обновления', ico: '🔄' },
      ] },
      { key: 'personal', title: 'Личное и доступ', items: [
        { id: 'st-profile', label: 'Мой профиль' },
      ] },
    ]);
    expect(model).toHaveLength(2);
    expect(model[0]).toEqual({
      key: 'data', title: 'Данные и восстановление', icon: '🗄',
      cards: [
        { id: 'st-backups', label: 'Резервные копии', ico: '💾' },
        { id: 'st-update', label: 'Обновления', ico: '🔄' },
      ],
    });
    expect(model[1].cards[0]).toEqual({ id: 'st-profile', label: 'Мой профиль', ico: '' });
  });

  it('пустые разделы и пункты без id не рисуются', () => {
    const model = tabsModel([
      { key: 'checks', title: 'Проверка чеков', items: [] },
      { key: '', title: 'Без ключа', items: [{ id: 'x', label: 'X' }] },
      { key: 'device', title: 'Устройство', items: [{ label: 'без id' }] },
      null,
    ]);
    expect(model).toEqual([]);
  });

  it('подпись карточки подстрахована её id, вход любого вида безопасен', () => {
    expect(tabsModel([{ key: 'a', items: [{ id: 'st-fns' }] }])[0].cards[0].label).toBe('st-fns');
    expect(tabsModel(null)).toEqual([]);
    expect(tabsModel('мусор')).toEqual([]);
  });
});

describe('tabForRoute', () => {
  const model = tabsModel([
    { key: 'data', title: 'Данные', items: [{ id: 'st-backups', label: 'Копии' }] },
    { key: 'integr', title: 'Интеграции', items: [{ id: 'st-max', label: 'MAX' }] },
  ]);

  it('узнаёт раздел из адреса #/settings/<раздел>', () => {
    expect(tabForRoute(model, 'integr')).toBe('integr');
    expect(tabForRoute(model, 'data')).toBe('data');
  });

  it('неизвестный раздел и пустой маршрут — первый таб', () => {
    expect(tabForRoute(model, 'nope')).toBe('data');
    expect(tabForRoute(model, '')).toBe('data');
    expect(tabForRoute(model, undefined)).toBe('data');
  });

  it('пустая модель — пустая строка (табов нет)', () => {
    expect(tabForRoute([], 'data')).toBe('');
    expect(tabForRoute(null, 'data')).toBe('');
  });
});

describe('maskToken', () => {
  it('оставляет начало и конец длинного токена', () => {
    expect(maskToken('hj6abcdefghijklmnopqrstuvwxyz4f2a')).toBe('hj6abc\u2022\u2022\u2022\u2022\u2022\u20224f2a');
  });

  it('длину можно задать, хвост можно убрать', () => {
    expect(maskToken('0123456789abcdef', { head: 4, tail: 2 })).toBe('0123\u2022\u2022\u2022\u2022\u2022\u2022ef');
    expect(maskToken('0123456789abcdef', { head: 4, tail: 0 })).toBe('0123\u2022\u2022\u2022\u2022\u2022\u2022');
  });

  it('короткие строки не раскрываются', () => {
    expect(maskToken('abc')).toBe('\u2022\u2022\u2022\u2022\u2022\u2022');
    expect(maskToken('abcdefgh')).toBe('ab\u2022\u2022\u2022\u2022\u2022\u2022gh');
    expect(maskToken('')).toBe('');
    expect(maskToken(null)).toBe('');
  });

  it('маска всегда короче исходной строки', () => {
    const long = 'x'.repeat(400);
    expect(maskToken(long).length).toBeLessThan(long.length);
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
