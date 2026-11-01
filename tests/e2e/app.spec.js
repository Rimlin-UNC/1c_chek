/* v1.70.60: браузерные сценарии пользователя (Playwright).
   Покрывают типичный путь: вход, отказ входа, настройки с читаемыми
   оглавлениями блоков и сворачиванием. Сервер поднимает конфиг. */
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

async function login(page) {
  await page.goto('/');
  await page.fill('#login-username', 'admin');
  await page.fill('#login-password', password);
  await page.click('#login-submit');
  await expect(page.locator('#page-title')).toHaveText('Дашборд',
    { timeout: 15000 });
}

test.describe('вход', () => {
  test('верные учётные данные открывают дашборд', async ({ page }) => {
    await login(page);
    await expect(page.locator('#login-form')).toBeHidden();
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
    const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
    await welcome.click();
    await page.locator('a[href="#/settings"]').click();
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
    const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
    await welcome.click();
    await page.locator('a[href="#/settings"]').click();
    const osm = page.locator('#st-openstack .card-title > .ct-text');
    await expect(osm).toContainText('Мониторинг', { timeout: 15000 });
  });
});


test('выбор и перенос чеков разных участников через браузер', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/poolpick"]').click();
  const company = page.locator('#poolpk-comp');
  await expect(company.locator('option')).toHaveCount(2);
  await company.selectOption({ index: 1 });
  const rows = page.locator('#poolpk-body tbody tr');
  await expect(rows.filter({ hasText: 'E2E магазин 1' })).toBeVisible();
  await expect(rows.filter({ hasText: 'E2E магазин 2' })).toBeVisible();
  await rows.filter({ hasText: 'E2E магазин 1' }).getByRole('checkbox').check();
  await rows.filter({ hasText: 'E2E магазин 2' }).getByRole('checkbox').check();
  await expect(page.locator('#pk-cnt')).toHaveText('2');
  const responsePromise = page.waitForResponse(response =>
    response.url().includes('/pool-company/pick-and-transfer') && response.request().method() === 'POST');
  await page.locator('#pk-pick-transfer').click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  expect((await response.json()).created).toBe(2);
  await expect(page.locator('#pk-cnt')).toHaveText('0');
  await page.locator('#poolpk-tab-mine').click();
  await expect(page.locator('#poolpk-body')).toContainText('E2E магазин 1');
  await expect(page.locator('#poolpk-body')).toContainText('E2E магазин 2');
});
