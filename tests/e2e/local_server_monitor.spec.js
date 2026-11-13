import { expect, test } from '@playwright/test';

test('локальный монитор работает без облачного блока и фоновых запросов', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  expect(login.ok()).toBeTruthy();
  const token = (await login.json()).access_token;
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.19');
  }, token);
  const cloudRequests = [];
  page.on('request', req => { if (new URL(req.url()).pathname.startsWith('/api/v1/openstack')) cloudRequests.push(req.url()); });
  await page.goto('/#/settings');
  const card = page.locator('#st-server-monitor');
  await expect(card).toBeAttached({ timeout: 30000 });
  if (!(await page.locator('#server-monitor-refresh').isVisible())) await card.locator('.card-title').click();
  const response = page.waitForResponse(r => r.url().endsWith('/api/v1/admin/server-monitor'));
  await page.locator('#server-monitor-refresh').click();
  expect((await response).status()).toBe(200);
  await expect(page.locator('#server-monitor-kpi')).toContainText('CPU сервера');
  await expect(page.locator('#server-monitor-kpi')).toContainText('Память сервера');
  await expect(page.locator('#server-monitor-status')).toContainText('Локальный замер');
  await expect(page.locator('#st-openstack')).toHaveCount(0);
  expect(cloudRequests).toEqual([]);
});
