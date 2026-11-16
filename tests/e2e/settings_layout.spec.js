/* v1.72.37: браузерные проверки раздела «Настройки» на реальных ширинах.
   Замок против возврата дефектов со снимков 15.11.2026 (текст выходил за
   рамки блока):
   • таблица копий не выходит за карточку и не перекрывается соседней;
   • карточки стоят строго одна под другой (одна колонка на любой ширине);
   • токен и таблица остаются внутри карточки, даты — не сырой ISO;
   • меню — горизонтальные табы, панель таба открывается и ведёт к карточке;
   • прямая ссылка #/settings/<раздел> открывает нужный раздел и таб;
   • на телефоне строки таблицы — карточки «подпись — значение»;
   • у кнопки создания копии есть состояние aria-busy.
   Сервер и базу поднимает playwright.config.js (одноразовая SQLite). */
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
  await expect(page.locator('#login-screen')).toBeHidden({ timeout: 15000 });
  await expect(page.locator('#page-title')).toHaveText('Дашборд', { timeout: 15000 });
  const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
  if (await welcome.isVisible().catch(() => false)) await welcome.click();
}

async function openSettings(page) {
  await login(page);
  const width = page.viewportSize().width;
  if (width < 1024) {
    // на планшете и телефоне основное меню спрятано в бургер — иначе ссылка
    // «Настройки» вне экрана и клик по ней никогда не срабатывает
    await page.locator('#btn-sidebar').click();
  }
  await page.locator('a[href="#/settings"]').click();
  await expect(page.locator('#st-backups')).toBeVisible({ timeout: 15000 });
}

/* Одноразовая база может не иметь ни одной копии: таблица тогда не
   отрисовывается. Для проверок таблицы создаём копию штатной кнопкой. */
async function ensureBackupRows(page) {
  if (await page.locator('#st-backups table.bk-table').count()) return;
  await page.locator('#btn-bk-create').click();
  await expect(page.locator('#st-backups table.bk-table')).toBeVisible({ timeout: 30000 });
}

const widths = [1600, 1280, 1024, 768, 390];

test.describe('настройки: сетка на ширинах экрана', () => {
  for (const width of widths) {
    test(`${width}px: страница не прокручивается вбок, таблица внутри карточки`, async ({ page }) => {
      await page.setViewportSize({ width, height: 900 });
      await openSettings(page);
      await ensureBackupRows(page);

      // 1. Горизонтальной прокрутки страницы нет: всё лишнее прокручивается
      //    внутри своих контейнеров, а не тянет за собой макет.
      const overflow = await page.evaluate(() =>
        document.documentElement.scrollWidth - document.documentElement.clientWidth);
      expect(overflow).toBeLessThanOrEqual(1);

      // 2. Контейнер таблицы не выходит за карточку (прежний дефект — наезд
      //    таблицы на соседнюю карточку «Обновления»).
      const card = await page.locator('#st-backups').boundingBox();
      const scroll = await page.locator('#st-backups .table-scroll').boundingBox();
      expect(scroll.x).toBeGreaterThanOrEqual(card.x - 1);
      expect(scroll.x + scroll.width).toBeLessThanOrEqual(card.x + card.width + 1);

      // 3. Внутри контейнера широкая таблица прокручивается, а не вылезает.
      const inside = await page.locator('#st-backups .table-scroll').evaluate((el) => ({
        scrollWidth: el.scrollWidth, clientWidth: el.clientWidth, overflowX: getComputedStyle(el).overflowX,
      }));
      if (width > 640) {
        // на планшете и компьютере широкая таблица прокручивается внутри
        expect(['auto', 'scroll']).toContain(inside.overflowX);
      } else {
        // на телефоне строки — карточки, горизонтальной прокрутки быть не должно
        expect(inside.overflowX).toBe('visible');
        expect(inside.scrollWidth).toBeLessThanOrEqual(inside.clientWidth + 1);
      }
      expect(inside.scrollWidth).toBeGreaterThanOrEqual(inside.clientWidth - 1);
    });
  }
});

test.describe('настройки: раскладка карточек', () => {
  test('карточка с таблицей занимает всю строку раздела', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openSettings(page);
    const grid = await page.locator('#st-backups').evaluate((el) => {
      const parent = el.parentElement;
      return {
        section: parent.className,
        card: el.getBoundingClientRect().width,
        row: parent.getBoundingClientRect().width,
      };
    });
    expect(grid.section).toContain('stg-section-grid');
    expect(grid.card / grid.row).toBeGreaterThan(0.95);
  });

  test('разделы собраны по data-sec, карточки не пересозданы', async ({ page }) => {
    await page.setViewportSize({ width: 1600, height: 900 });
    await openSettings(page);
    const sections = await page.locator('.settings-grid > .stg-section').count();
    expect(sections).toBeGreaterThanOrEqual(3);
    // кнопки в карточке копий живы после переноса узлов
    await expect(page.locator('#btn-bk-create')).toBeEnabled();
    await expect(page.locator('#btn-bk-refresh')).toBeEnabled();
  });
});

test.describe('настройки: табы и одна колонка', () => {
  test('меню — полоса табов, на компьютере видна целиком', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openSettings(page);
    const tabs = page.locator('#settings-nav .stg-tab');
    await expect(tabs).toHaveCount(5);
    await expect(tabs.first()).toBeVisible();
    // полоса табов не прокручивается на обычном мониторе
    const row = await page.locator('#settings-nav .stg-tabs-row')
      .evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth }));
    expect(row.sw - row.cw).toBeLessThanOrEqual(1);
    await expect(page.locator('.stg-tab.active')).toHaveCount(1);
  });

  test('телефон: табы прокручиваются по горизонтали, страница — нет', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openSettings(page);
    const row = await page.locator('#settings-nav .stg-tabs-row')
      .evaluate((el) => ({ sw: el.scrollWidth, cw: el.clientWidth, ox: getComputedStyle(el).overflowX }));
    expect(row.ox).toBe('auto');
    expect(row.sw).toBeGreaterThan(row.cw);
    const pageOverflow = await page.evaluate(() =>
      document.documentElement.scrollWidth - document.documentElement.clientWidth);
    expect(pageOverflow).toBeLessThanOrEqual(1);
  });

  test('клик по табу открывает раздел и его панель с карточками', async ({ page }) => {
    await page.setViewportSize({ width: 1440, height: 900 });
    await openSettings(page);
    await page.locator('.stg-tab[data-sec="checks"]').click();
    await expect(page.locator('.stg-tab[data-sec="checks"]')).toHaveAttribute('aria-selected', 'true');
    await expect(page).toHaveURL(/#\/settings\/checks$/);
    const panel = page.locator('.stg-panel:not([hidden])');
    await expect(panel).toBeVisible();
    expect(await panel.locator('.snav-item').count()).toBeGreaterThan(0);
    // виден только выбранный раздел
    const visible = await page.locator('.stg-section:not([hidden])').count();
    expect(visible).toBe(1);
    // пункт панели ведёт к карточке и закрывает панель
    const first = panel.locator('.snav-item').first();
    const id = await first.getAttribute('data-open');
    expect(id).toBeTruthy();
    await first.click();
    await expect(page.locator('.stg-panel:not([hidden])')).toHaveCount(0);
    await expect(page.locator('#' + id)).toBeAttached();
    // прокрутка плавная: ждём, пока карточка действительно попадёт в экран
    await expect.poll(async () => page.locator('#' + id).evaluate((el) => {
      const r = el.getBoundingClientRect();
      const h = window.innerHeight || document.documentElement.clientHeight;
      const content = el.closest('.content');
      return { inView: r.top < h && r.bottom > 0, scrollTop: content ? content.scrollTop : 0 };
    }), { timeout: 5000 }).toEqual({ inView: true, scrollTop: expect.any(Number) });
  });

  test('прямая ссылка открывает нужный раздел и таб', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await login(page);
    await page.goto('/#/settings/data/st-backups');
    await expect(page.locator('#st-backups')).toBeVisible({ timeout: 15000 });
    await expect(page.locator('.stg-tab.active')).toHaveAttribute('data-sec', 'data');
    await expect(page.locator('.stg-section:not([hidden])')).toHaveAttribute('data-sec', 'data');
    await expect(page.locator('#st-backups')).toBeInViewport();
  });

  test('карточки стоят одной колонкой на любой ширине', async ({ page }) => {
    // вход один: повторный login() на уже авторизованном экране не находит форму
    await page.setViewportSize({ width: 1600, height: 900 });
    await openSettings(page);
    for (const width of [1600, 1280, 1024]) {
      await page.setViewportSize({ width, height: 900 });
      await expect(page.locator('#st-backups')).toBeVisible();
      const boxes = await page.locator('#st-backups, #st-update').evaluateAll((els) =>
        els.map((el) => { const r = el.getBoundingClientRect(); return { x: Math.round(r.x), w: Math.round(r.width) }; }));
      expect(boxes[0].x).toBe(boxes[1].x);
      expect(boxes[0].w).toBe(boxes[1].w);
    }
  });

  test('токен и таблица не выходят за карточку, даты читаемые', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openSettings(page);
    await ensureBackupRows(page);
    // таблица копий внутри карточки
    const card = await page.locator('#st-backups').boundingBox();
    const table = await page.locator('#st-backups table.bk-table').boundingBox();
    expect(table.x).toBeGreaterThanOrEqual(card.x - 1);
    expect(table.x + table.width).toBeLessThanOrEqual(card.x + card.width + 1);
    // поле токена 1С не шире карточки (прежний дефект — токен выезжал наружу).
    // Карточка живёт в разделе «Чек-Пул и интеграции», поэтому сначала её таб,
    // затем раскрываем свёрнутые блоки внутри карточки.
    await page.locator('.stg-tab[data-sec="integr"]').click();
    await page.evaluate(() => {
      document.querySelectorAll('.settings-grid details').forEach((d) => { d.open = true; });
    });
    const token = page.locator('#onec-token');
    await expect(token).toBeVisible({ timeout: 10000 });
    const tb = await token.boundingBox();
    const tokenCardRight = await page.locator('#onec-token').evaluate((el) =>
      el.closest('.glass.card').getBoundingClientRect().right);
    expect(tb.x + tb.width).toBeLessThanOrEqual(tokenCardRight + 1);
    // даты в таблицах — не сырой ISO 8601
    const raw = await page.locator('#st-backups table.bk-table tbody').evaluate((el) =>
      (el.textContent.match(/\d{4}-\d{2}-\d{2}T\d/g) || []).length);
    expect(raw).toBe(0);
  });
});

test.describe('настройки: состояния и телефонные строки', () => {
  test('кнопка «Создать копию» показывает загрузку и не дублирует нажатие', async ({ page }) => {
    await page.setViewportSize({ width: 1280, height: 900 });
    await openSettings(page);
    // ответ искусственно замедлен, иначе состояние не успеть увидеть
    await page.route('**/api/v1/admin/backups', async (route) => {
      if (route.request().method() === 'POST') {
        await new Promise((resolve) => setTimeout(resolve, 700));
      }
      await route.continue();
    });
    const btn = page.locator('#btn-bk-create');
    await btn.click();
    await expect(btn).toHaveAttribute('aria-busy', 'true');
    await expect(btn).toBeDisabled();
    await expect(btn).not.toHaveAttribute('aria-busy', 'true', { timeout: 10000 });
    await expect(btn).toBeEnabled();
    // ширина кнопки не прыгает: подпись остаётся в потоке
    const width = await btn.evaluate((el) => el.getBoundingClientRect().width);
    expect(width).toBeGreaterThan(120);
  });

  test('на телефоне строка копии — карточка с подписями полей', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 844 });
    await openSettings(page);
    const table = page.locator('#st-backups table.bk-table');
    await ensureBackupRows(page);
    await expect(table).toBeVisible();
    await expect(page.locator('#st-backups table.bk-table thead')).toBeHidden();

    await ensureBackupRows(page);
    // подпись поля — реальный .td-k (псевдоэлементы-подписи запрещены v1.70.9)
    const name = page.locator('#st-backups table.bk-table td[data-label="Копия"]').first();
    const label = name.locator('.td-k');
    await expect(label).toHaveText('Копия');
    await expect(label).toBeVisible();
    const display = await name.evaluate((el) => getComputedStyle(el).display);
    expect(display).toBe('flex');

    // «Восстановить» внутри карточки, не за краем экрана
    const restore = page.locator('#st-backups button[data-restore]').first();
    if (await restore.count()) {
      const box = await restore.boundingBox();
      expect(box.x).toBeGreaterThanOrEqual(0);
      expect(box.x + box.width).toBeLessThanOrEqual(390 + 1);
    }
  });
});
