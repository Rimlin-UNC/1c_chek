import { test, expect } from '@playwright/test';

test('счётчики меню на телефоне обновляются после создания компании', async ({ page, request }) => {
  test.setTimeout(120000);
  let login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  if (!login.ok()) {
    login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'admin123' } });
    expect(login.ok()).toBeTruthy();
    const token = (await login.json()).access_token;
    const change = await request.post('/api/v1/auth/change-password', {
      headers: { Authorization: `Bearer ${token}` },
      data: { old_password: 'admin123', new_password: 'playwright-test-password' },
    });
    expect(change.ok()).toBeTruthy();
    login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  }
  const token = (await login.json()).access_token;
  await page.setViewportSize({ width: 390, height: 844 });
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.24');
  }, token);
  await page.goto('/#/dashboard');
  await expect(page.locator('#app-shell')).toBeVisible({ timeout: 30000 });
  await page.locator('#btn-sidebar').click();
  const clients = page.locator('[data-nav-count="clients"]');
  const primary = page.locator('[data-nav-count="primary"]');
  const companies = page.locator('[data-nav-count="companies"]');
  await expect(clients).toBeVisible();
  await expect(primary).toBeVisible();
  await expect(companies).toBeVisible();
  const before = Number((await companies.textContent()).replace(/\s/g, ''));
  const created = await page.evaluate(async () => (await import('/js/api.js')).api.post('/api/v1/companies', { name: 'Тест счётчика меню' }));
  expect(created.id).toBeTruthy();
  await page.locator('a[href="#/companies"]').click();
  await page.locator('#btn-sidebar').click();
  await expect(companies).toHaveText(String(before + 1), { timeout: 15000 });
  for (const badge of [clients, primary, companies]) {
    await expect.poll(async () => {
      const box = await badge.boundingBox();
      return box && box.x >= 0 && box.x + box.width <= 390;
    }).toBeTruthy();
  }
});
