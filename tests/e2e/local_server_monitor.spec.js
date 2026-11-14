import { expect, test } from '@playwright/test';

test('локальный монитор работает без облачного блока и фоновых запросов', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  expect(login.ok()).toBeTruthy();
  const token = (await login.json()).access_token;
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.21');
  }, token);
  const cloudRequests = [];
  let historyRequests = 0;
  await page.route('**/api/v1/admin/server-monitor/traffic', async route => {
    historyRequests++;
    await route.fulfill({ status: 200, json: { status: 'freshness_unknown', current_period: '2026-11', data: {
      interface: 'ens3', created_date: '2026-10-17', updated_at: '2026-11-08T12:00:00', billing_verified: false,
      months: [{ period: '2026-11', rx_bytes: 400000000000, tx_bytes: 450000000000 }],
      days: [{ date: '2026-11-08', rx_bytes: 1000000000, tx_bytes: 2000000000 }] } } });
  });
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
  await expect(page.locator('#server-monitor-kpi')).toContainText('Load average');
  await expect(page.locator('#server-monitor-kpi')).toContainText('Swap');
  await expect(page.locator('#server-monitor-details')).toContainText('Сеть:');
  await expect(page.locator('#server-monitor-note')).toContainText('История производительности');
  await expect(page.locator('#server-monitor-status')).toContainText('Локальный замер');
  expect(historyRequests).toBe(0);
  await card.locator('summary').filter({ hasText: 'Трафик интерфейса' }).click();
  await card.locator('#server-traffic-refresh').click();
  await expect(card.locator('#server-traffic')).toContainText('850,00 ГБ');
  await expect(card.locator('#server-traffic')).toContainText('не счёт провайдера');
  await expect(card.locator('#server-traffic')).toContainText('80 %');
  await expect(card.locator('#server-traffic')).toContainText('Свежесть источника не подтверждена');
  expect(historyRequests).toBe(1);
  await expect(page.locator('#st-openstack')).toHaveCount(0);
  expect(cloudRequests).toEqual([]);
});
