import { test, expect } from '@playwright/test';

test('запись сохраняется до завершения транзакции и изолируется по владельцу', async ({ page }) => {
  await page.goto('/');
  const item = { ownerId: 'user-a', companyId: 'firm-a', clientId: 'firm-a', qrData: 'qr-a', source: 'camera' };
  const created = await page.evaluate(async item => (await import('/js/offline_outbox.js')).enqueue(item), item);
  await page.reload();
  const result = await page.evaluate(async () => {
    const box = await import('/js/offline_outbox.js');
    return { mine: await box.list('user-a'), other: await box.list('user-b') };
  });
  expect(result.mine).toHaveLength(1);
  expect(result.mine[0]).toMatchObject({ ...item, id: created.id, state: 'pending' });
  expect(result.other).toEqual([]);
});

test('конкурирующие вкладки получают одну аренду; старый владелец не удалит запись', async ({ page, context }) => {
  await page.goto('/');
  const second = await context.newPage();
  await second.goto('/');
  const item = { ownerId: 'user-a', companyId: null, clientId: null, qrData: 'qr-b', source: 'web' };
  const added = await Promise.all([page, second].map(p => p.evaluate(async value =>
    (await import('/js/offline_outbox.js')).enqueue(value), item)));
  expect(added[0].id).toBe(added[1].id);
  expect(await page.evaluate(async () => (await import('/js/offline_outbox.js')).list('user-a'))).toHaveLength(1);
  const [a, b] = await Promise.all([page, second].map(p => p.evaluate(async () =>
    (await import('/js/offline_outbox.js')).claim('user-a', 1000, 1000))));
  expect([a, b].filter(Boolean)).toHaveLength(1);
  const first = a || b;
  const next = await second.evaluate(async () => (await import('/js/offline_outbox.js')).claim('user-a', 2001, 1000));
  expect(next.id).toBe(first.id);
  expect(next.leaseId).not.toBe(first.leaseId);
  expect(await page.evaluate(async first => (await import('/js/offline_outbox.js')).complete(first.id, first.leaseId), first)).toBe(false);
  expect(await second.evaluate(async next => (await import('/js/offline_outbox.js')).complete(next.id, next.leaseId), next)).toBe(true);
  expect(await page.evaluate(async () => (await import('/js/offline_outbox.js')).list('user-a'))).toEqual([]);
});

test('ошибки остаются в очереди; устаревшая аренда не меняет состояние', async ({ page }) => {
  await page.goto('/');
  const result = await page.evaluate(async () => {
    const box = await import('/js/offline_outbox.js');
    await box.enqueue({ ownerId: 'user-a', companyId: null, clientId: null, qrData: 'qr-c', source: 'web' });
    const lease = await box.claim('user-a', 1000, 1000);
    const stale = await box.fail(lease.id, 'wrong', { attention: true, message: '403' });
    const marked = await box.fail(lease.id, lease.leaseId, { attention: true, message: '403' });
    const cannotClaim = await box.claim('user-a', 3000, 1000);
    const rows = await box.list('user-a');
    return { stale, marked, cannotClaim, rows };
  });
  expect(result.stale).toBe(false);
  expect(result.marked).toBe(true);
  expect(result.cannotClaim).toBeNull();
  expect(result.rows).toHaveLength(1);
  expect(result.rows[0]).toMatchObject({ state: 'attention', message: '403', qrData: 'qr-c' });
});

test('нельзя сохранить запись без полного контекста', async ({ page }) => {
  await page.goto('/');
  const result = await page.evaluate(async () => {
    const box = await import('/js/offline_outbox.js');
    try { await box.enqueue({ ownerId: 'user-a', qrData: 'qr' }); return 'ошибка'; }
    catch { return 'отказ'; }
  });
  expect(result).toBe('отказ');
});
