/* v1.70.60: юнит-тесты раскроя термопечати (Vitest).
   Геометрия листа и колонок, нарезка длинного чека по границам блоков,
   best-fit раскрой кусков по страницам. */
import { describe, expect, it } from 'vitest';
import {
  COL_H, COL_W, COLS, MARGIN_X, PAGE_H, PAGE_W, SCALE,
  columnX, packCut, splitBlocks,
} from '../../app/static/js/printpack.js';

describe('геометрия листа', () => {
  it('А4 и колонки', () => {
    expect(PAGE_W).toBe(210);
    expect(PAGE_H).toBe(297);
    expect(COLS).toBe(2);
    expect(COL_W).toBe(72);
    expect(COL_H).toBe(281);
    expect(MARGIN_X).toBe(20);
  });
  it('масштаб термоленты', () => {
    expect(SCALE).toBe(1);
  });
  it('колонки не наезжают: вторая заканчивается в поле листа', () => {
    expect(columnX(0)).toBe(20);
    expect(columnX(1)).toBe(118);
    expect(columnX(1) + COL_W).toBeLessThanOrEqual(PAGE_W - MARGIN_X);
  });
});

describe('splitBlocks', () => {
  it('короткий чек — одна часть', () => {
    expect(splitBlocks([10, 20, 30])).toEqual([[0, 3]]);
  });
  it('длинный режется по границам блоков с резервом', () => {
    // 200+100 не влезают в 273 (281 минус резерв первой части)
    expect(splitBlocks([200, 100, 200])).toEqual([[0, 1], [1, 2], [2, 3]]);
  });
  it('блок выше колонки печатается как есть, цикл не зависает', () => {
    expect(splitBlocks([500])).toEqual([[0, 1]]);
  });
  it('все блоки суммарно входят в колонку с резервом', () => {
    const parts = splitBlocks([100, 100, 100, 100]);
    expect(parts).toEqual([[0, 2], [2, 4]]);
    // покрытие без пропусков: начало первой 0, концы смежны, конец последней = 4
    expect(parts[0][0]).toBe(0);
    for (let i = 1; i < parts.length; i++) {
      expect(parts[i][0]).toBe(parts[i - 1][1]);
    }
    expect(parts[parts.length - 1][1]).toBe(4);
  });
});

describe('packCut', () => {
  it('три куска умещаются на страницу best-fit', () => {
    const pages = packCut([100, 100, 100]);
    expect(pages.length).toBe(1);
    const pieces = pages[0].map((x) => x.piece).sort();
    expect(pieces).toEqual([0, 1, 2]);
  });
  it('кусок выше колонки — честная ошибка', () => {
    expect(() => packCut([COL_H + 1])).toThrow(/выше колонки/);
  });
  it('переполнение открывает вторую страницу', () => {
    const pages = packCut([200, 200, 200, 200]);
    expect(pages.length).toBe(2);
  });
  it('координаты укладки не выходят за колонку', () => {
    const pages = packCut([50, 60, 70, 80, 90, 100]);
    for (const page of pages) {
      for (const x of page) {
        expect(x.y).toBeGreaterThanOrEqual(0);
        expect(x.y + 0.01).toBeLessThanOrEqual(COL_H);
      }
    }
  });
});

it('физически не выходит за лист и учитывает зазоры на границах', () => {
  const heights = [COL_H, 100, 100, 78, 60, 100];
  const pages = packCut(heights);
  for (const page of pages) {
    for (const p of page) {
      expect(p.y + heights[p.piece]).toBeLessThanOrEqual(COL_H + 0.0001);
      expect(columnX(p.col) + COL_W).toBeLessThanOrEqual(PAGE_W - MARGIN_X);
    }
  }
});
