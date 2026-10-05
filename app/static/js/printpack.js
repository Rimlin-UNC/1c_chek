// ======================================================================
// Ямастер Чек — v1.24.0: нарезка чеков на листы A4 (печать «Напечатать чек»).
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
//
// Чистая геометрия без DOM: легко тестировать (node).
// Единицы — миллиметры печати (CSS mm), а не пиксели.
//
// v1.24.0 — ТРИ КОЛОНКИ на А4: кассовый чек проектируется шириной 80 мм
// (как на термоленте), а на лист масштабируется коэффициентом SCALE до
// эффективных 62 мм. Геометрия листа: поля X 7 мм, зазор между колонками
// 5 мм → 7 + 62 + 5 + 62 + 5 + 62 + 7 = 210 мм (точно А4).
// Поля по вертикали 8 мм → высота колонки 297 − 16 = 281 мм.
//
// Рациональность раскроя (минимум пустого места):
//  1) длинный чек нарезается ПОЗИЦИОННО (splitBlocks — рез только между
//     строками позиций), каждая часть ≤ колонки;
//  2) укладка best-fit (packCut): каждый кусок кладётся в ту колонку,
//     где он занимает минимум оставшегося места; при равенстве — раньше
//     страница и левее колонка. Порядок чеков сохраняется настолько,
//     насколько позволяет плотность: более поздние мелкие чеки заполняют
//     остатки колонок предыдущих страниц.
// ======================================================================

export const PAGE_W = 210;      // ширина А4, мм
export const PAGE_H = 297;      // высота А4, мм

// v1.24.0: сетка трёх колонок
export const RCPT_W = 80;       // проектная ширина чека (термолента), мм
export const COL_W = 62;        // эффективная ширина колонки на листе, мм
export const SCALE = COL_W / RCPT_W;   // 0.775 — масштаб печати чека
export const MARGIN_X = 7;      // боковое поле листа, мм
export const MARGIN_Y = 8;      // верхнее/нижнее поле листа, мм
export const COL_GAP = 5;       // зазор между колонками, мм
export const COLS = 3;
export const COL_H = PAGE_H - 2 * MARGIN_Y;   // 281 мм
export const PIECE_GAP = 3;     // минимальный воздух между чеками, мм

// X-координата колонки (левый край куска), мм
export function columnX(col) {
  return MARGIN_X + col * (COL_W + COL_GAP);
}

/**
 * Нарезка длинного чека по высоте колонки — ТОЛЬКО по границам блоков,
 * т.е. между строками позиций (позиционно), шапка и итоги не режутся.
 * blocksH — высоты блоков чека по порядку (мм): шапка, разделитель,
 * строки позиций, итоги, футер, QR. Возвращает массив [отIdx, доIdx) —
 * интервалы блоков, которые войдут в каждую часть. Каждая часть, кроме
 * последней, получает резерв reserveMm снизу — под строку
 * «продолжение на части N».
 */
export function splitBlocks(blocksH, reserveMm = 8) {
  const total = blocksH.reduce((a, b) => a + b, 0);
  if (total <= COL_H) return [[0, blocksH.length]];
  const parts = [];
  let i = 0;
  while (i < blocksH.length) {
    let free = COL_H - (parts.length === 0 ? reserveMm : 0);
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
  const pages = [];                       // [ [free0, free1, free2], ... ]
  const placed = [];
  for (let piece = 0; piece < heights.length; piece++) {
    const h = heights[piece] + PIECE_GAP;
    let best = null;                      // { page, col, used }
    for (let page = 0; page < pages.length && !best; page++) {
      const cols = pages[page];
      let colBest = null;
      for (let col = 0; col < cols.length; col++) {
        if (cols[col] >= h && (colBest === null || cols[col] < cols[colBest])) {
          colBest = col;                  // минимальный достаточный остаток
        }
      }
      if (colBest !== null) {
        best = { page, col: colBest, used: COL_H - cols[colBest] };
      }
    }
    if (!best) {
      best = { page: pages.length, col: 0, used: 0 };
      pages.push([COL_H, COL_H, COL_H]);
    }
    pages[best.page][best.col] -= h;
    placed.push({ page: best.page, col: best.col, y: best.used, piece });
  }
  // группируем по страницам для рендера
  const result = pages.map((_, p) => placed.filter(x => x.page === p));
  return result;
}
