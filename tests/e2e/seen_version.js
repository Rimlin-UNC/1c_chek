/* v1.72.38: сценарии держали в localStorage закреплённую версию «1.72.35».
   После любого нового выпуска у настоящего пользователя и в CI открывается
   окно «Что нового» — оно перекрывало заставку загрузки, заставка не уходила,
   и сценарии падали по таймауту 120 с, ожидая нажатия под оверлеем.
   Причина была в замке, а не в приложении: версия берётся у работающего
   сервера, поэтому больше не требует правки в каждом выпуске.
   Запуск и общий конфиг — playwright.config.js. */
import { expect } from '@playwright/test';

/** Текущая версия приложения по /api/v1/about (без ведущей «v»). */
export async function seenVersion(page) {
  const response = await page.request.get('/api/v1/about');
  expect(response.ok(), '/api/v1/about отвечает').toBeTruthy();
  const { version } = await response.json();
  expect(String(version), 'версия из /api/v1/about').toMatch(/^\d+\.\d+\.\d+$/);
  return String(version);
}
