// ======================================================================
// Ямастер Чек — v1.7.0: раскрой листов A4 под чеки 80 мм (печать PDF).
// Разработчик и владелец идеи: ООО «Ямастер» | ymaster.ru | info@ymaster.ru
//
// Чистая геометрия без DOM: легко тестировать (node).
// Единицы — миллиметры печати (CSS mm), а не пиксели.
//
// Геометрия А4 (портрет): 210 × 297 мм. Поля страницы 12 мм, между
// колонками зазор 18 мм → две колонки по 80 мм (реальная ширина чека
// термопринтера), высота колонки = 297 − 2×12 = 273 мм.
// ======================================================================

export const PAGE_W = 210;      // ширина А4, мм
export const PAGE_H = 297;      // высота А4, мм
export const MARGIN = 12;       // поле страницы, мм
export const COL_W = 80;        // ширина кассового чека, мм
export const COL_GAP = 18;      // зазор между колонками, мм
export const COLS = 2;
export const COL_H = PAGE_H - 2 * MARGIN;      // 273 мм
export const PIECE_GAP = 4;    // минимальный воздух между чеками, мм

// X-координаты колонок (левый край чека), мм
export function columnX(col) {
  return MARGIN + col * (COL_W + COL_GAP);
}

/**
 * Нарезка длинного чека по высоте колонки.
 * blocksH — высоты «блоков» чека по порядку (мм): шапка, разделитель,
 * строки позиций, итоги, футер. Возвращает массив [отIdx, доIdx) —
 * интервалы блоков, которые войдут в каждую часть. Часть 1 получает
 * резерв reserveMm снизу (под строку «продолжение на части 2»).
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
 * Близкий к оптимальному раскрой кусков по страницам (first-fit).
 * heights — высоты кусков (мм), каждый ≤ COL_H.
 * Возвращает страницы: [{ col, y, piece }] — piece = индекс куска.
 * Куски идут в исходном порядке: чеки не «перемешиваются» — каждый
 * кладётся в первую колонку, где помещается ЦЕЛИКОМ.
 */
export function packCut(heights) {
  for (const h of heights) {
    if (h > COL_H + 0.01) {
      throw new Error(`Кусок ${h.toFixed(1)} мм выше колонки ${COL_H} мм — нарежьте splitBlocks`);
    }
  }
  const pages = [];                       // [ [freeCol0, freeCol1], ... ]
  const placed = [];
  for (let piece = 0; piece < heights.length; piece++) {
    const h = heights[piece] + PIECE_GAP;
    let page = 0;
    // ищем первое место, куда кусок входит целиком
    for (;; page++) {
      if (page >= pages.length) {
        pages.push([COL_H, COL_H]);       // новая страница, обе колонки свободны
      }
      const cols = pages[page];
      const col = cols.findIndex(free => free >= h);
      if (col !== -1) {
        const used = COL_H - cols[col];   // y = сколько уже занято сверху
        cols[col] -= h;
        placed.push({ page, col, y: used, piece });
        break;
      }
    }
  }
  // группируем по страницам для рендера
  const result = pages.map((_, p) => placed.filter(x => x.page === p));
  return result;
}
