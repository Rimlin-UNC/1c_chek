/* v1.74.1: регрессионные тесты надёжности «Настроек» и модулей клиента:
   1) api.js не падает при заблокированном localStorage (строгий приватный режим);
   2) выбор стартового таба настроек у ролей без раздела «Данные» (accountant, manager, user)
      никогда не выбирает скрытый таб и не оставляет пустой экран;
   3) в billing.js карточка и модальное окно подписки компании используют csub-* вместо коллизии с #cs-save. */
import { describe, expect, it } from 'vitest';
import { getToken, setToken, clearToken } from '../../app/static/js/api.js';
import { tabForRoute, tabsModel } from '../../app/static/js/settings_layout.js';
import { adminBillingCardShellHTML } from '../../app/static/js/billing.js';

describe('api.js — устойчивость к заблокированному localStorage', () => {
  it('getToken / setToken / clearToken не бросают исключений при SecurityError в localStorage', () => {
    const orig = globalThis.localStorage;
    const brokenStorage = {
      getItem() { throw new Error('SecurityError: The operation is insecure.'); },
      setItem() { throw new Error('QuotaExceededError'); },
      removeItem() { throw new Error('SecurityError'); },
    };
    Object.defineProperty(globalThis, 'localStorage', {
      value: brokenStorage,
      configurable: true,
    });
    try {
      expect(() => setToken('demo-jwt')).not.toThrow();
      expect(getToken()).toBe('');
      expect(() => clearToken()).not.toThrow();
    } finally {
      Object.defineProperty(globalThis, 'localStorage', {
        value: orig,
        configurable: true,
      });
    }
  });
});

describe('settings_layout — выбор первого видимого таба для не-админских ролей', () => {
  it('у сотрудника (видимы только personal и device) стартовый таб — personal, а не скрытый data', () => {
    const domTabs = [
      { sec: 'data', label: 'Данные', hidden: true },
      { sec: 'checks', label: 'Проверки', hidden: true },
      { sec: 'integr', label: 'Интеграции', hidden: true },
      { sec: 'personal', label: 'Личное', hidden: false },
      { sec: 'device', label: 'Устройство', hidden: false },
    ];
    const model = tabsModel(
      domTabs.filter((t) => !t.hidden).map((t) => ({
        key: t.sec,
        title: t.label,
        items: [{ id: '__tab__' }],
      })),
    );
    expect(model.map((t) => t.key)).toEqual(['personal', 'device']);
    expect(tabForRoute(model, '')).toBe('personal');
    expect(tabForRoute(model, 'data')).toBe('personal');
    expect(tabForRoute(model, 'device')).toBe('device');
  });

  it('у бухгалтера (без раздела data) стартовый таб — checks, а personal открывает personal', () => {
    const domTabs = [
      { sec: 'data', label: 'Данные', hidden: true },
      { sec: 'checks', label: 'Проверки', hidden: false },
      { sec: 'integr', label: 'Интеграции', hidden: true },
      { sec: 'personal', label: 'Личное', hidden: false },
      { sec: 'device', label: 'Устройство', hidden: false },
    ];
    const model = tabsModel(
      domTabs.filter((t) => !t.hidden).map((t) => ({
        key: t.sec,
        title: t.label,
        items: [{ id: '__tab__' }],
      })),
    );
    expect(model.map((t) => t.key)).toEqual(['checks', 'personal', 'device']);
    expect(tabForRoute(model, '')).toBe('checks');
    expect(tabForRoute(model, 'personal')).toBe('personal');
    expect(tabForRoute(model, 'data')).toBe('checks');
  });
});

describe('billing.js — отсутствие коллизии с #cs-save карточки 152-ФЗ', () => {
  it('оболочка карточки админа содержит #st-billing и не дублирует id="cs-save"', () => {
    const html = adminBillingCardShellHTML();
    expect(html).toContain('id="st-billing"');
    expect(html).not.toContain('id="cs-save"');
  });
});
