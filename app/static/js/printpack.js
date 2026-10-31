// ======================================================================
// Ямастер Чек — v1.24.0: нарезка чеков на листы A4 (печать «Напечатать чек»).
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | chek@ymaster.ru
//
// Чистая геометрия без DOM: легко тестировать (node).
// Единицы — миллиметры печати (CSS mm), а не пиксели.
//
// A4 portrait: две колонки по 72 мм без масштабирования. Три такие
// колонки на листе шириной 210 мм физически не помещаются.
// Небольшие куски заполняют свободное место в колонках предыдущих листов.
// ======================================================================

export const PAGE_W = 210;
export const PAGE_H = 297;
export const RCPT_W = 72;
export const COL_W = 72;
export const SCALE = 1;
export const MARGIN_X = 20;
export const MARGIN_Y = 8;
export const COL_GAP = 26;
export const COLS = 2;
export const COL_H = PAGE_H - 2 * MARGIN_Y;
export const PIECE_GAP = 3;

// X-координата колонки (левый край куска), мм
export function columnX(col) {
  return MARGIN_X + col * (COL_W + COL_GAP);
}

/**
 * Нарезка длинного чека по высоте колонки — ТОЛЬКО по границам блоков,
 * т.е. между строками позиций (позиционно), шапка и итоги не режутся.
 * blocksH — высоты блоков чека по порядку (мм): шапка, разделитель,
 * строки позиций, итоги, футер, QR. Возвращает массив [отIdx, доIdx) —
 * интервалы блоков, которые войдут в каждую часть. Для частей длинного чека резервируется место для подписей продолжения.
 */
export function splitBlocks(blocksH, reserveMm = 8) {
  const total = blocksH.reduce((a, b) => a + b, 0);
  if (total <= COL_H) return [[0, blocksH.length]];
  const parts = [];
  let i = 0;
  while (i < blocksH.length) {
    let free = COL_H - reserveMm;
    let j = i, used = 0;
    while (j < blocksH.length && used + blocksH[j] <= free) {
      used += blocksH[j];
      j++;
    }
    if (j === i) j = i + 1;            // один блок выше колонки — печатаем как есть
    parts.push([i, j]);
    i = j;
  }
  return parts;
}

/**
 * Близкий к оптимальному раскрой кусков по страницам — v1.24.0: best-fit.
 * heights — эффективные высоты кусков (мм), каждый ≤ COL_H.
 * Возвращает страницы: [{ col, y, piece }] — piece = индекс куска.
 * Кусок кладётся в колонку с МИНИМАЛЬНЫМ достаточным остатком (меньше
 * воздуха после укладки); при равенстве остатка — раньше страница и
 * левее колонка. Порядок страниц/колонок в результате стабильный.
 */
export function packCut(heights) {
  for (const h of heights) {
    if (h > COL_H + 0.01) {
      throw new Error(`Кусок ${h.toFixed(1)} мм выше колонки ${COL_H} мм — нарежьте splitBlocks`);
    }
  }
  const pages = [];                       // остатки в колонках
  const placed = [];
  for (let piece = 0; piece < heights.length; piece++) {
    const h = heights[piece];
    let best = null;                      // { page, col, used }
    for (let page = 0; page < pages.length && !best; page++) {
      const cols = pages[page];
      let colBest = null;
      for (let col = 0; col < cols.length; col++) {
        if (cols[col] >= h + (cols[col] < COL_H ? PIECE_GAP : 0) && (colBest === null || cols[col] < cols[colBest])) {
          colBest = col;                  // минимальный достаточный остаток
        }
      }
      if (colBest !== null) {
        best = { page, col: colBest, used: COL_H - cols[colBest] + (cols[colBest] < COL_H ? PIECE_GAP : 0) };
      }
    }
    if (!best) {
      best = { page: pages.length, col: 0, used: 0 };
      pages.push(Array(COLS).fill(COL_H));
    }
    pages[best.page][best.col] = COL_H - best.used - h;
    placed.push({ page: best.page, col: best.col, y: best.used, piece });
  }
  // группируем по страницам для рендера
  const result = pages.map((_, p) => placed.filter(x => x.page === p));
  return result;
}
