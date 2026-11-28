/* v1.74.0: браузерная проверка центра платных подписок и прайс-листа администратора (#st-billing). */
import { expect, test } from '@playwright/test';

const password = 'playwright-test-password';

test.beforeAll(async ({ request }) => {
  const alreadySet = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password },
  });
  if (alreadySet.ok()) return;
  const loginResponse = await request.post('/api/v1/auth/login', {
    data: { username: 'admin', password: 'admin123' },
  });
  expect(loginResponse.ok()).toBeTruthy();
  const { access_token: token } = await loginResponse.json();
  const response = await request.post('/api/v1/auth/change-password', {
    headers: { Authorization: `Bearer ${token}` },
    data: { old_password: 'admin123', new_password: password },
  });
  expect(response.ok()).toBeTruthy();
});

test('центр платных подписок и тарифов #st-billing открывается, переключает вкладки и не ломает вёрстку', async ({ page }) => {
  await page.setViewportSize({ width: 1280, height: 900 });
  await page.goto('/');
  await page.fill('#login-username', 'admin');
  await page.fill('#login-password', password);
  await page.click('#login-submit');
  await expect(page.locator('#login-screen')).toBeHidden({ timeout: 15000 });
  const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
  if (await welcome.isVisible().catch(() => false)) await welcome.click();

  await page.goto('/#/settings/personal/st-billing');
  await expect(page.locator('#st-billing')).toBeVisible({ timeout: 15000 });
  await expect(page.locator('#st-billing [data-btab="catalog"]')).toBeVisible({ timeout: 10000 });

  await page.locator('#st-billing [data-btab="catalog"]').click();
  await expect(page.locator('#st-billing')).toContainText('Чеки и авансовые отчёты');
  await expect(page.locator('#st-billing')).toContainText('Бухгалтерия ПРО');

  await page.locator('#st-billing [data-btab="companies"]').click();
  await expect(page.locator('#st-billing')).toContainText('Организация');

  const overflow = await page.evaluate(() =>
    document.documentElement.scrollWidth - document.documentElement.clientWidth);
  expect(overflow).toBeLessThanOrEqual(1);
});
