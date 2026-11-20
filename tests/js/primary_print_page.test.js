/* v1.73.5: страница печатной формы УПД в браузере телефона.
   На Android мобильный Chrome не открывает системное окно печати по вызову из скрипта —
   вызов молча игнорируется. Раньше человек нажимал «🖨 Печать» и ничего не происходило,
   а по «?print=1» не появлялось даже бланка. Теперь вместо этого — подсказка словами,
   а сам бланк остаётся на экране. Проверяем оба пути: телефон и компьютер. */
import { beforeEach, describe, expect, it, vi } from 'vitest';

const PAGE = '../../app/static/js/primary_print_page.js';
const ANDROID_UA = 'Mozilla/5.0 (Linux; Android 14; SM-A546E) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/131.0.0.0 Mobile Safari/537.36';
const DESKTOP_UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
  + '(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36';

function mount({ print = '0', ua = DESKTOP_UA } = {}) {
  const button = { addEventListener: vi.fn((_, fn) => { button.click = fn; }) };
  const hint = { textContent: 'В окне печати выберите «Сохранить как PDF».' };
  const listeners = {};
  globalThis.document = {
    readyState: 'complete',
    body: { dataset: { print } },
    getElementById: (id) => (id === 'pp-print' ? button : (id === 'pp-hint' ? hint : null)),
  };
  globalThis.window = {
    print: vi.fn(),
    addEventListener: (name, fn) => { listeners[name] = fn; },
  };
  globalThis.navigator = { userAgent: ua };
  return { button, hint, listeners };
}

describe('страница печати: компьютер', () => {
  beforeEach(() => { vi.resetModules(); vi.useFakeTimers(); });

  it('кнопка открывает системное окно печати', async () => {
    const { button } = mount();
    await import(PAGE);
    button.click();
    expect(window.print).toHaveBeenCalledTimes(1);
  });

  it('«?print=1» печатает сразу, без нажатия', async () => {
    mount({ print: '1' });
    await import(PAGE);
    vi.advanceTimersByTime(100);
    expect(window.print).toHaveBeenCalledTimes(1);
  });
});

describe('страница печати: Android', () => {
  beforeEach(() => { vi.resetModules(); vi.useFakeTimers(); });

  it('«?print=1» не молчит: показывает, где печать в браузере', async () => {
    const { hint } = mount({ print: '1', ua: ANDROID_UA });
    await import(PAGE);
    vi.advanceTimersByTime(100);
    expect(hint.textContent).toContain('«Поделиться»');
    expect(hint.textContent).toContain('Печать');
  });

  it('кнопка объясняет меню браузера и всё равно пробует печать', async () => {
    const { button, hint } = mount({ ua: ANDROID_UA });
    await import(PAGE);
    button.click();
    expect(hint.textContent).toContain('Сохранить как PDF');
    vi.advanceTimersByTime(100);
    expect(window.print).toHaveBeenCalledTimes(1);
  });
});
