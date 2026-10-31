/* v1.70.60: браузерные сценарии пользователя (Playwright).
   Покрывают типичный путь: вход, отказ входа, настройки с читаемыми
   оглавлениями блоков и сворачиванием. Сервер поднимает конфиг. */
import { expect, test } from '@playwright/test';

async function login(page) {
  await page.goto('/');
  await page.fill('#login-username', 'admin');
  await page.fill('#login-password', 'admin123');
  await page.click('#login-submit');
  await expect(page.locator('#page-title')).toHaveText('Дашборд',
    { timeout: 15000 });
}

test.describe('вход', () => {
  test('верные учётные данные открывают дашборд', async ({ page }) => {
    await login(page);
    await expect(page.locator('#login-form')).toHaveCount(0);
  });

  test('неверный пароль остаётся на входе с понятной ошибкой', async ({ page }) => {
    await page.goto('/');
    await page.fill('#login-username', 'admin');
    await page.fill('#login-password', 'wrong-pass-1');
    await page.click('#login-submit');
    const err = page.locator('#login-error');
    await expect(err).not.toHaveClass(/hidden/, { timeout: 15000 });
    await expect(err).not.toHaveText('');
  });
});

test.describe('настройки: оглавления блоков', () => {
  test('текст оглавления — отдельный пролёт, сворачивание работает', async ({ page }) => {
    await login(page);
    await page.goto('/#/set');
    const heads = page.locator('.settings-grid .card-title > .ct-text');
    await expect(heads.first()).toBeVisible({ timeout: 15000 });
    expect(await heads.count()).toBeGreaterThan(3);

    const card = page.locator('.settings-grid .glass.card.foldable').first();
    const head = card.locator('> .card-title');
    await head.click();
    await expect(card).toHaveClass(/collapsed/);
    await head.click();
    await expect(card).not.toHaveClass(/collapsed/);
  });

  test('оглавление мониторинга облака читается целиком', async ({ page }) => {
    await login(page);
    await page.goto('/#/set');
    const osm = page.locator('#st-openstack .card-title > .ct-text');
    await expect(osm).toContainText('Мониторинг', { timeout: 15000 });
  });
});
