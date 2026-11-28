// ======================================================================
// Ямастер Чек — раскладка и мелочи страницы «Настройки» (v1.72.37)
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
 * Модель горизонтальных табов настроек (v1.72.37).
 * Таб = раздел, выпадающая панель таба = карточки этого раздела (один уровень).
 * @param {Array<{key: string, title: string, icon?: string,
 *                items: Array<{id: string, label: string, ico?: string}>}>} groups
 * @returns {Array<{key: string, title: string, icon: string,
 *                  cards: Array<{id: string, label: string, ico: string}>}>}
 */
export function tabsModel(groups) {
  return (Array.isArray(groups) ? groups : [])
    .filter((g) => g && g.key)
    .map((g) => ({
      key: String(g.key),
      title: String(g.title || ''),
      icon: String(g.icon || ''),
      cards: (Array.isArray(g.items) ? g.items : [])
        .filter((it) => it && it.id)
        .map((it) => ({
          id: String(it.id),
          label: String(it.label || it.id).trim(),
          ico: String(it.ico || ''),
        })),
    }))
    .filter((g) => g.cards.length);
}

/**
 * Активный таб для маршрута #/settings/<раздел>/<карточка>.
 * Если раздел неизвестен (или маршрута нет) — первый доступный таб.
 * @returns {string} ключ раздела или '' для пустой модели
 */
export function tabForRoute(model, group) {
  const list = Array.isArray(model) ? model : [];
  if (!list.length) return '';
  const found = list.find((t) => t.key === String(group || ''));
  return found ? found.key : list[0].key;
}

/**
 * Маска токена для показа в интерфейсе: первые `head` и последние `tail`
 * символов, между ними фиксированное число точек. Строка любой длины
 * превращается в предсказуемо короткую — она физически не может растянуть
 * карточку (дефект со снимка 15.11.2026: токен MAX выезжал на соседнюю карточку).
 * @param {string} value
 * @param {{head?: number, tail?: number}} [opts]
 * @returns {string}
 */
export function maskToken(value, opts) {
  const s = String(value == null ? '' : value).trim();
  if (!s) return '';
  const o = opts || {};
  const head = Number.isFinite(o.head) ? Math.max(0, o.head) : 6;
  const tail = Number.isFinite(o.tail) ? Math.max(0, o.tail) : 4;
  const dots = '\u2022'.repeat(6);
  if (s.length <= head + tail) return s.length <= 4 ? dots : `${s.slice(0, 2)}${dots}${s.slice(-2)}`;
  return `${s.slice(0, head)}${dots}${tail ? s.slice(-tail) : ''}`;
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

function normSearchText(s) {
  return String(s || '')
    .toLowerCase()
    .replace(/ё/g, 'е')
    .replace(/\s+/g, ' ')
    .trim();
}

/**
 * v1.74.2: чистая фильтрация карточек настроек для быстрого поиска из меню «⋯ Ещё».
 * Ищет все слова запроса по заголовку карточки, подсказке, разделу и идентификатору;
 * совпадения в заголовке поднимаются выше совпадений в описании.
 * @param {Array<{id?: string, label?: string, hint?: string, secTitle?: string}>} cards
 * @param {string} query
 */
export function filterSettingsCards(cards, query) {
  const list = Array.isArray(cards) ? cards : [];
  const q = normSearchText(query);
  if (!q) return list.slice();
  const tokens = q.split(' ').filter(Boolean);
  const scored = [];
  for (let idx = 0; idx < list.length; idx++) {
    const item = list[idx];
    if (!item) continue;
    const title = normSearchText(item.label);
    const hint = normSearchText(item.hint);
    const sec = normSearchText(item.secTitle);
    const id = normSearchText(item.id);
    const hay = `${title} ${sec} ${hint} ${id}`;
    if (!tokens.every((t) => hay.includes(t))) continue;
    let rank = 2;
    if (title.startsWith(q)) rank = 0;
    else if (tokens.every((t) => title.includes(t))) rank = 1;
    scored.push({ item, rank, idx });
  }
  scored.sort((a, b) => (a.rank - b.rank) || (a.idx - b.idx));
  return scored.map((s) => s.item);
}
