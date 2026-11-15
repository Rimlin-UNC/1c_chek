// ======================================================================
// Ямастер Чек — раскладка страницы «Настройки» (v1.72.36)
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
//
// Чистые функции без DOM — их проверяет Vitest (tests/js/settings_layout.test.js).
// Задача: превратить плоский список карточек настроек в предсказуемую сетку —
// карточки с таблицами и графиками занимают всю строку, каждый раздел
// начинается с новой строки, порядок берётся из inline-стиля order, а не из
// порядка разметки (в шаблоне карточки лежат «как удобно», визуальный порядок
// задаёт order).
//
// v1.72.36: почему не masonry. Нативного `grid-template-rows: masonry` в
// браузерах нет (флаги/tech-preview), а падение через CSS-колонки ломает
// порядок чтения и переходы по ссылкам #/settings/<раздел>/<карточка>.
// Поэтому раскладка — обычная сетка с явными пролётами.
// ======================================================================

/** Значение order по умолчанию — «в конец», если порядок не задан. */
export const DEFAULT_ORDER = 1000;

/**
 * Числовой order карточки.
 * Принимает и «12», и «12;grid-column:1/-1» (так пишут inline-стили в app.js).
 * @param {string|number|undefined|null} value
 * @returns {number}
 */
export function orderOf(value) {
  const n = parseInt(String(value ?? '').trim(), 10);
  return Number.isFinite(n) ? n : DEFAULT_ORDER;
}

/**
 * План раскладки карточек настроек.
 * @param {Array<{id?: string, sec?: string, order?: string|number,
 *                hasTable?: boolean, hasChart?: boolean, hidden?: boolean}>} cards
 * @returns {Array<{id: string, sec: string, order: number,
 *                  wide: boolean, sectionStart: boolean}>}
 *
 * Правила:
 *  - скрытые для роли карточки (hidden) в план не попадают — иначе первая
 *    видимая карточка раздела не получила бы «начало строки»;
 *  - wide = карточка с таблицей или графиком: ей нужна вся ширина строки,
 *    иначе таблица шире колонки и наезжает на соседнюю карточку;
 *  - sectionStart = самая первая карточка раздела в визуальном порядке
 *    (разделы не перемешиваются между собой);
 *  - карточки без id в план не попадают: без id их не найти в DOM.
 */
export function layoutPlan(cards) {
  const list = (Array.isArray(cards) ? cards : [])
    .filter((c) => c && c.id && !c.hidden)
    .map((c) => ({
      id: String(c.id),
      sec: String(c.sec || ''),
      order: orderOf(c.order),
      wide: !!(c.hasTable || c.hasChart),
    }));

  // Раздел, который встречается в визуальном порядке раньше, идёт первым.
  const firstOrder = new Map();
  list.forEach((c) => {
    if (!firstOrder.has(c.sec) || c.order < firstOrder.get(c.sec)) {
      firstOrder.set(c.sec, c.order);
    }
  });
  list.sort((a, b) => {
    const sa = firstOrder.get(a.sec);
    const sb = firstOrder.get(b.sec);
    if (sa !== sb) return sa - sb;
    if (a.sec !== b.sec) return a.sec < b.sec ? -1 : 1;
    return a.order - b.order;
  });

  const seen = new Set();
  return list.map((c) => {
    const sectionStart = !seen.has(c.sec);
    seen.add(c.sec);
    return { ...c, sectionStart };
  });
}

/**
 * Модель выпадающего списка навигации (планшет и телефон).
 * На узких экранах колонка меню превращается в селект «Перейти к настройке…».
 * @param {Array<{key: string, title: string, icon?: string,
 *                items: Array<{id: string, label: string, ico?: string}>}>} groups
 * @returns {Array<{key: string, title: string, items: Array<{value: string, label: string}>}>}
 */
export function jumpModel(groups) {
  return (Array.isArray(groups) ? groups : [])
    .filter((g) => g && g.key && Array.isArray(g.items) && g.items.length)
    .map((g) => ({
      key: String(g.key),
      title: String(g.title || ''),
      items: g.items
        .filter((it) => it && it.id)
        .map((it) => ({
          value: `${g.key}|${it.id}`,
          label: `${it.ico ? it.ico + ' ' : ''}${String(it.label || it.id).trim()}`,
        })),
    }))
    .filter((g) => g.items.length);
}

/**
 * Значение селекта навигации для текущего маршрута.
 * Нет выбранной карточки — пустая строка (пункт «Перейти к настройке…»).
 */
export function jumpValue(group, card) {
  if (!group || !card) return '';
  return `${group}|${card}`;
}

/** Разбор значения селекта обратно в пару «раздел / карточка». */
export function parseJumpValue(value) {
  const s = String(value || '');
  const i = s.indexOf('|');
  if (i <= 0 || i === s.length - 1) return { group: '', card: '' };
  return { group: s.slice(0, i), card: s.slice(i + 1) };
}

/**
 * Порция строк для карточки с длинным списком.
 * @param {Array} items
 * @param {number} limit  сколько строк показывать в карточке
 * @returns {{shown: Array, rest: Array, total: number, hasMore: boolean}}
 */
export function pageSlice(items, limit) {
  const arr = Array.isArray(items) ? items : [];
  const n = Number.isFinite(limit) && limit > 0 ? Math.floor(limit) : arr.length;
  return {
    shown: arr.slice(0, n),
    rest: arr.slice(n),
    total: arr.length,
    hasMore: arr.length > n,
  };
}
