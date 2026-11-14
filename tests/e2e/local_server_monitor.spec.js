import { expect, test } from '@playwright/test';

test('локальный монитор работает без облачного блока и фоновых запросов', async ({ page, request }) => {
  test.setTimeout(120000);
  const login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  expect(login.ok()).toBeTruthy();
  const token = (await login.json()).access_token;
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.28');
  }, token);
  const cloudRequests = [];
  let performanceRequests = 0;
  await page.route('**/api/v1/admin/server-monitor/history?**', async route => {
    performanceRequests++;
    await route.fulfill({ status: 200, json: { status: 'available', period: '1h', interface: null,
      current_timestamp: 120, last_sample_at: 120, retention_paused: false, buckets: [
        { timestamp: 0, expected: 1, samples: 1, cpu_pct: 30, memory_pct: 50, cpu_samples: 1, memory_samples: 1 },
        { timestamp: 60, expected: 1, samples: 0, cpu_pct: null, memory_pct: null, cpu_samples: 0, memory_samples: 0 },
        { timestamp: 120, expected: 1, samples: 1, cpu_pct: 60, memory_pct: 70, cpu_samples: 1, memory_samples: 1 },
      ] } });
  });
  let mountRequests = 0;
  await page.route('**/api/v1/admin/server-monitor/mounts', async route => {
    mountRequests++;
    await route.fulfill({ status: 200, json: { status: 'available', partial: false,
      collected_at: '2026-11-08T12:00:00+00:00', mounts: [
        { path: '/', type: 'ext4', device: '8:1', status: 'available',
          disk: { pct: 92, available_bytes: 1024, total_bytes: 8192 }, inodes: { pct: 88 } },
        { path: '/net', type: 'nfs4', device: '0:42', status: 'not_probed', disk: null, inodes: null },
      ] } });
  });
  let processRequests = 0;
  await page.route('**/api/v1/admin/server-monitor/processes', async route => {
    processRequests++;
    await route.fulfill({ status: 200, json: { status: 'available', visible: 1, zombies: 0, partial: false,
      top_cpu: [{ pid: 101, name: 'worker', state: 'R', cpu_pct: 12.5 }],
      top_memory: [{ pid: 101, name: 'worker', state: 'R', rss_bytes: 1024, memory_pct: 0.1 }] } });
  });
  let historyRequests = 0;
  await page.route('**/api/v1/admin/server-monitor/traffic', async route => {
    historyRequests++;
    await route.fulfill({ status: 200, json: { status: 'freshness_unknown', current_period: '2026-11', data: {
      interface: 'ens3', created_date: '2026-10-17', updated_at: '2026-11-08T12:00:00', billing_verified: false,
      months: [{ period: '2026-11', rx_bytes: 400000000000, tx_bytes: 450000000000 }],
      days: [{ date: '2026-11-08', rx_bytes: 1000000000, tx_bytes: 2000000000 }] },
      scenario: { projected_bytes: 1200000000000, sample_days: 7, as_of: '2026-11-08T12:00:00',
        billing_verified: false, completeness_verified: false } } });
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
  await expect(page.locator('#server-monitor-details')).toContainText('inode файловой системы приложения');
  await expect(page.locator('#server-monitor-details')).toContainText('I/O устройства файловой системы приложения');
  await expect(page.locator('#server-monitor-details')).toContainText('включая потоки');
  await expect(page.locator('#server-monitor-note')).toContainText('История производительности');
  await expect(page.locator('#server-monitor-status')).toContainText('Локальный замер');
  expect(mountRequests).toBe(0);
  await card.locator('summary').filter({ hasText: 'Монтирования' }).click();
  await card.locator('#server-mounts-refresh').click();
  await expect(card.locator('#server-mounts')).toContainText('92,00 %');
  await expect(card.locator('#server-mounts')).toContainText('высокая занятость');
  await expect(card.locator('#server-mounts')).toContainText('не опрашивается');
  expect(mountRequests).toBe(1);
  expect(performanceRequests).toBe(0);
  await card.locator('summary').filter({ hasText: 'История производительности' }).click();
  await card.locator('#server-history-refresh').click();
  await expect(card.locator('#server-history svg[aria-label*="CPU"]')).toHaveCount(1);
  await expect(card.locator('#server-history svg line')).toHaveCount(0);
  await expect(card.locator('#server-history')).toContainText('Пропуски');
  expect(performanceRequests).toBe(1);
  expect(processRequests).toBe(0);
  await card.locator('summary').filter({ hasText: 'Процессы' }).click();
  await card.locator('#server-processes-refresh').click();
  await expect(card.locator('#server-processes')).toContainText('worker');
  await expect(card.locator('#server-processes')).toContainText('12,50 %');
  expect(processRequests).toBe(1);
  expect(historyRequests).toBe(0);
  await card.locator('summary').filter({ hasText: 'Трафик интерфейса' }).click();
  await card.locator('#server-traffic-refresh').click();
  await expect(card.locator('#server-traffic')).toContainText('850,00 ГБ');
  await expect(card.locator('#server-traffic')).toContainText('не счёт провайдера');
  await expect(card.locator('#server-traffic')).toContainText('80 %');
  await expect(card.locator('#server-traffic')).toContainText('Свежесть источника не подтверждена');
  await expect(card.locator('#server-traffic')).toContainText('Условный сценарий');
  await expect(card.locator('#server-traffic')).toContainText('не биллинг');
  await expect(card.locator('#server-traffic')).toContainText('1200,00 ГБ');
  expect(historyRequests).toBe(1);
  await expect(page.locator('#st-openstack')).toHaveCount(0);
  expect(cloudRequests).toEqual([]);
});
