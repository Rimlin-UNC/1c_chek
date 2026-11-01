/* v1.70.60: Playwright — браузерные сценарии пользователя.
   Запуск: npm run test:e2e (нужны браузеры: npx playwright install chromium;
   в песочнице без доступа к CDN браузеров конфиг и сценарии проверяются
   перечислением: npx playwright test --list).
   Сервер поднимается сам на порту 8123 с отдельной базой. */
import { defineConfig } from '@playwright/test';
import { mkdtempSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';

// Каждый запуск работает с новой БД, никогда не с БД приложения.
const dbDir = mkdtempSync(join(tmpdir(), 'ymaster-playwright-'));


export default defineConfig({
  testDir: 'tests/e2e',
  timeout: 30_000,
  retries: 0,
  reporter: [['list']],
  use: {
    baseURL: 'http://127.0.0.1:8123',
    headless: true,
    ...(process.env.YM_PLAYWRIGHT_CHROMIUM ? { launchOptions: { executablePath: process.env.YM_PLAYWRIGHT_CHROMIUM } } : {}),
    // следы только при падении — прогон остаётся быстрым
    trace: 'retain-on-failure',
    screenshot: 'only-on-failure',
  },
  webServer: {
    command: '.venv/bin/python -m qa.playwright_seed && .venv/bin/python run.py --host 127.0.0.1 --port 8123',
    env: { DATABASE_URL: `sqlite:///${join(dbDir, 'test.db')}`, YM_OSMON_OFF: '1' },
    url: 'http://127.0.0.1:8123/',
    reuseExistingServer: false,
    timeout: 60_000,
  },
});
