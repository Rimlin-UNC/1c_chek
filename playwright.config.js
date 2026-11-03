/* v1.70.60: Playwright — браузерные сценарии пользователя.
   Запуск: npm run test:e2e (нужны браузеры: npx playwright install chromium;
   в песочнице без доступа к CDN браузеров — Chromium из npm: bash
   qa/local_chromium.sh, затем YM_PLAYWRIGHT_CHROMIUM=/tmp/ym-chromium/chrome.sh;
   перечисление сценариев без браузера: npx playwright test --list).
   Сервер поднимается сам на порту 8123 с отдельной базой. */
import { defineConfig } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Каждый запуск работает с новой БД, никогда не с БД приложения.
const dbDir = mkdtempSync(join(tmpdir(), 'ymaster-playwright-'));
// v1.70.86: локальный Chromium 153 из npm (qa/local_chromium.sh) старше того, что
// ждёт Playwright, и после закрытого контекста с приложением создаёт новый ≈ 18 с.
// Под нагрузкой полного прогона сценарий упирался в 30 с, поэтому для своего
// браузера лимит — 60 с. В CI (штатный браузер Playwright) — прежние 30 с.
const localChromium = process.env.YM_PLAYWRIGHT_CHROMIUM;


export default defineConfig({
  testDir: 'tests/e2e',
  timeout: localChromium ? 60_000 : 30_000,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8123',
    headless: true,
    ...(localChromium ? { launchOptions: { executablePath: localChromium } } : {}),
    // следы только при падении — прогон остаётся быстрым
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: '.venv/bin/python -m qa.playwright_seed && .venv/bin/python run.py --host 127.0.0.1 --port 8123',
    env: { DATABASE_URL: `sqlite:///${join(dbDir, 'test.db')}`, YM_OSMON_OFF: '1', SUPABASE_DEMO_ENABLED: '1', SUPABASE_URL: 'https://example.supabase.co', SUPABASE_PUBLISHABLE_KEY: 'sb_publishable_e2e' },
    url: 'http://127.0.0.1:8123/',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
