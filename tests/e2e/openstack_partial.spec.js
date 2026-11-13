import { expect, test } from '@playwright/test';

test('OpenStack: частичный доступ не превращается в нулевую загрузку', async ({ page, request }) => {
  test.setTimeout(120000);
  let login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  if (!login.ok()) {
    login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'admin123' } });
    expect(login.ok()).toBeTruthy();
    const token = (await login.json()).access_token;
    const changed = await request.post('/api/v1/auth/change-password', {
      headers: { Authorization: `Bearer ${token}` },
      data: { old_password: 'admin123', new_password: 'playwright-test-password' },
    });
    expect(changed.ok()).toBeTruthy();
    login = await request.post('/api/v1/auth/login', { data: { username: 'admin', password: 'playwright-test-password' } });
  }
  const token = (await login.json()).access_token;
  await page.addInitScript(token => {
    localStorage.setItem('ymaster_check_token', token);
    localStorage.setItem('ymaster_seen_version', '1.72.16');
  }, token);
  await page.route('**/api/v1/openstack/test', route => route.fulfill({ json: {
    ok: true, authorized: true, notice: 'Keystone принял учётные данные, но статистика гипервизоров недоступна.',
    services: { compute: 'доступен', metric: 'доступен', load_balancer: 'нет в каталоге' },
  } }));
  await page.route('**/api/v1/openstack/metrics/current', route => route.fulfill({ json: {
    available: true, cpu: { total: null, used: null, pct: null },
    ram: { total_mb: null, used_mb: null, free_mb: null, pct: null },
    disk: { total_gb: null, used_gb: null, free_gb: null, pct: null },
    quotas: { used_cores: 3, max_cores: 20, used_ram_mb: 2048, max_ram_mb: 8192, used_instances: 2, max_instances: 10 },
    notes: { hypervisor_error: 'Недоступно' },
  } }));
  await page.route('**/api/v1/openstack/metrics/history**', route => route.fulfill({ json: { points: [], thresholds: {} } }));
  await page.goto('/#/settings');
  const card = page.locator('#st-openstack');
  await expect(card).toBeAttached({ timeout: 30000 });
  // Свёрнутая карточка настроек может требовать раскрытия.
  if (!(await page.locator('#osm-test').isVisible())) await card.locator('.card-title').click();
  await page.locator('#osm-test').click();
  await expect(page.locator('#osm-test-result')).toContainText('не сохраняет настройки');
  await expect(page.locator('#osm-test-result')).toContainText('metric — есть в каталоге');
  await page.locator('#osm-tab-metrics').click();
  await expect(page.locator('#osm-warnings')).toContainText('Статистика гипервизоров недоступна');
  await expect(page.locator('#osm-kpi')).toContainText('3 / 20');
  await expect(page.locator('#osm-kpi')).toContainText('Это не фактическая загрузка');
  await expect(page.locator('#osm-kpi')).not.toContainText('0,00 %');
});
