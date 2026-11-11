/* v1.70.60: браузерные сценарии пользователя (Playwright).
   Покрывают типичный путь: вход, отказ входа, настройки с читаемыми
   оглавлениями блоков и сворачиванием. Сервер поднимает конфиг. */
import { mkdirSync, mkdtempSync, readFileSync, writeFileSync } from 'node:fs';
import { tmpdir } from 'node:os';
import { join } from 'node:path';
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
  // v1.70.98: «Дашборд» — статичный текст заголовка в index.html, он есть и до входа; ждём,
  // пока экран входа скроется (вход завершён, токен сохранён)
  await expect(page.locator('#login-screen')).toBeHidden({ timeout: 15000 });
  await expect(page.locator('#page-title')).toHaveText('Дашборд',
    { timeout: 15000 });
}

test.describe('вход', () => {
  test('верные учётные данные открывают дашборд', async ({ page }) => {
    await login(page);
    await expect(page.locator('#login-form')).toBeHidden();
  });

  test('неверный пароль остаётся на входе с понятной ошибкой', async ({ page }) => {
    await page.goto('/');
    await page.fill('#login-username', 'admin');
    await page.fill('#login-password', 'wrong-pass-1');
    await page.click('#login-submit');
    const err = page.locator('#login-error');
    await expect(err).not.toHaveClass(/hidden/, { timeout: 15000 });
    await expect(err).not.toHaveText('');
  });
});

test.describe('настройки: оглавления блоков', () => {
  test('текст оглавления — отдельный пролёт, сворачивание работает', async ({ page }) => {
    await login(page);
    const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
    await welcome.click();
    await page.locator('a[href="#/settings"]').click();
    const heads = page.locator('.settings-grid .card-title > .ct-text');
    await expect(heads.first()).toBeVisible({ timeout: 15000 });
    expect(await heads.count()).toBeGreaterThan(3);

    const card = page.locator('.settings-grid .glass.card.foldable').first();
    const head = card.locator('> .card-title');
    await head.click();
    await expect(card).toHaveClass(/collapsed/);
    await head.click();
    await expect(card).not.toHaveClass(/collapsed/);
  });

  test('оглавление мониторинга облака читается целиком', async ({ page }) => {
    await login(page);
    const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
    await welcome.click();
    await page.locator('a[href="#/settings"]').click();
    const osm = page.locator('#st-openstack .card-title > .ct-text');
    await expect(osm).toContainText('Мониторинг', { timeout: 15000 });
  });
});


test('выбор и перенос чеков разных участников через браузер', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/poolpick"]').click();
  const company = page.locator('#poolpk-comp');
  // v1.70.83: в сиде есть и компании спора о владельце (E2E Альфа и Бета) —
  // компанию подбора выбираем по имени, а не по числу пунктов списка.
  const yamaster = company.locator('option', { hasText: 'Ямастер' });
  await expect(yamaster).toHaveCount(1);
  await company.selectOption(await yamaster.getAttribute('value'));
  const rows = page.locator('#poolpk-body tbody tr');
  await expect(rows.filter({ hasText: 'E2E магазин 1' })).toBeVisible();
  await expect(rows.filter({ hasText: 'E2E магазин 2' })).toBeVisible();
  await rows.filter({ hasText: 'E2E магазин 1' }).getByRole('checkbox').check();
  await rows.filter({ hasText: 'E2E магазин 2' }).getByRole('checkbox').check();
  await expect(page.locator('#pk-cnt')).toHaveText('2');
  const responsePromise = page.waitForResponse(response =>
    response.url().includes('/pool-company/pick-and-transfer') && response.request().method() === 'POST');
  await page.locator('#pk-pick-transfer').click();
  const response = await responsePromise;
  expect(response.status()).toBe(200);
  expect((await response.json()).created).toBe(2);
  await expect(page.locator('#pk-cnt')).toHaveText('0');
  await page.locator('#poolpk-tab-mine').click();
  await expect(page.locator('#poolpk-body')).toContainText('E2E магазин 1');
  await expect(page.locator('#poolpk-body')).toContainText('E2E магазин 2');
  // После переноса доступна обезличенная группировка участников компании.
  const participant = page.locator('#pk-m-participant');
  await expect(participant.locator('option')).toHaveCount(3);
  const mineRows = page.locator('#poolpk-body tbody tr');
  await expect(mineRows).toHaveCount(2);
  await participant.selectOption({ index: 1 });
  await expect(mineRows).toHaveCount(1);
  await expect(page.locator('#poolpk-body')).toContainText('по фильтрам: 1');
  await page.locator('#pk-m-participant-reset').click();
  await expect(mineRows).toHaveCount(2);
  await expect(page.locator('#poolpk-body')).toContainText('по фильтрам: 2');
});


test('Supabase демо: read-only снимок и фильтры', async ({ page }) => {
  await login(page);
  const welcome = page.getByRole('button', { name: 'Понятно, работаем' });
  await welcome.click();
  await page.route('**/api/v1/supabase-demo/config', route => route.fulfill({
    json: { enabled: true, mode: 'read_only_demo', url: 'https://example.supabase.co',
      publishable_key: 'sb_publishable_e2e', rpc: 'chek_demo_snapshot' },
  }));
  await page.route('https://example.supabase.co/rest/v1/rpc/chek_demo_snapshot', route => route.fulfill({
    json: { mode: 'read_only_demo', generated_at: '2026-11-01T00:00:00Z',
      companies: [{ id: 'c1', name: 'Демо-Альфа' }, { id: 'c2', name: 'Демо-Бета' }],
      participants: [{ id: 'p1', company_id: 'c1', label: 'Участник A' }],
      receipts: [
        { id: 'r1', company_id: 'c1', participant_id: 'p1', receipt_no: 'DEMO-1', receipt_date: '2026-10-20',
          company_name: 'Демо-Альфа', participant_label: 'Участник A', merchant_name: 'Демо-маркет', total: 100, status: 'verified' },
        { id: 'r2', company_id: 'c2', participant_id: null, receipt_no: 'DEMO-2', receipt_date: '2026-10-21',
          company_name: 'Демо-Бета', participant_label: 'Без участника', merchant_name: 'Демо-кафе', total: 200, status: 'new' },
      ],
      items: [{ receipt_id: 'r1', name: 'Демо-товар', quantity: 1 }] },
  }));
  await page.locator('a[href="#/supabase"]').click();
  await expect(page.locator('#page-title')).toHaveText('Supabase демо');
  await expect(page.locator('#sb-demo-out')).toContainText('DEMO-1');
  await expect(page.locator('#sb-demo-out tbody tr')).toHaveCount(2);
  await page.locator('#sb-demo-company').selectOption('c1');
  await expect(page.locator('#sb-demo-out tbody tr')).toHaveCount(1);
  await expect(page.locator('#sb-demo-out')).toContainText('Демо-маркет');
  await page.locator('#sb-demo-reset').click();
  await expect(page.locator('#sb-demo-out tbody tr')).toHaveCount(2);
});

// v1.70.83: разбор конфликтов единого реестра (#9). Сид кладёт спор о владельце:
// одна фискальная тройка в реестре «E2E Альфа» и в Чек-Пуле у «E2E Бета».
test('разбор конфликтов: администратор закрепляет спорный чек за компанией', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await expect(page.locator('#nav-conflicts-badge')).toHaveText('1');
  await page.locator('a[href="#/conflicts"]').click();
  await expect(page.locator('#page-title')).toHaveText('Разбор конфликтов');
  const row = page.locator('#ur-out tbody tr', { hasText: 'E2E спорный магазин' });
  await expect(row).toBeVisible();
  await expect(row.locator('.chip')).toHaveText('Владелец');
  await row.getByRole('button', { name: 'Открыть' }).click();
  const card = page.locator('.modal-slot');
  await expect(card.locator('#ur-copies')).toContainText('E2E Альфа');
  await expect(card.locator('#ur-copies')).toContainText('E2E Бета');
  await card.locator('#ur-company').selectOption({ label: 'E2E Альфа' });
  await card.locator('#ur-reason').fill('Чек оплачен картой компании Альфа');
  await card.locator('#ur-resolve').click();
  await expect(page.locator('#ur-out')).toContainText('Конфликтов нет');
  await expect(page.locator('#nav-conflicts-badge')).toBeHidden();
});

// v1.70.86 («Единая база», фаза 3): число организаций у чека. Сид кладёт чек
// «E2E Альфы» с отклонённой заявкой «E2E Беты» и бухгалтера Альфы.
test('число организаций: администратор разрешает показ, бухгалтер видит только число', async ({ page }) => {
  // Локальный Chromium из npm на 2 ядрах и при полном pytest работает существенно медленнее.
  // CI сохраняет прежний строгий предел 90 секунд; проверки содержимого не меняются.
  test.setTimeout(process.env.YM_PLAYWRIGHT_CHROMIUM ? 150_000 : 90_000);
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/receipts"]').click();
  await page.locator('#rc-view-list').click();
  const claimed = page.locator('#receipts-table tbody tr', { hasText: '9999999999990088' });
  await expect(claimed.locator('.org-count')).toHaveText('🏢 2');
  // фильтр администратора «две организации и больше»: в выборке только такие чеки
  await page.locator('#f-multi').selectOption('true');
  await page.locator('#btn-filter').click();
  await expect(claimed).toBeVisible();
  const rows = page.locator('#receipts-table tbody tr');
  await expect(rows).toHaveCount(await rows.locator('.org-count').count());
  await page.locator('#btn-filter-reset').click();
  // разрешение показа «выбранным компаниям» — только Альфе
  await page.locator('a[href="#/conflicts"]').click();
  await page.locator('#ur-tab-visibility').click();
  await page.locator('#ur-vis-mode-selected').check();
  await page.locator('#ur-vis-companies label', { hasText: 'E2E Альфа' }).locator('input').check();
  await expect(page.locator('#ur-vis-warning')).toHaveText('');
  await page.locator('#ur-vis-save').click();
  await expect(page.locator('#toast-container')).toContainText('Настройки показа сохранены');
  // бухгалтер Альфы видит число у своего чека — без названий других организаций
  await page.locator('#btn-logout').click();
  // выход перезагружает страницу: форму входа заполняем только после загрузки
  const reloaded = page.waitForEvent('load');
  await page.locator('#lo-exit').click();
  await reloaded;
  await page.fill('#login-username', 'e2e_acc_alpha');
  await page.fill('#login-password', 'e2e-accountant-password');
  await page.click('#login-submit');
  await expect(page.locator('#login-form')).toBeHidden({ timeout: 15000 });
  await expect(page.locator('a[href="#/receipts"]')).toBeVisible();
  await page.locator('a[href="#/receipts"]').click();
  await page.locator('#rc-view-list').click();
  const own = page.locator('#receipts-table tbody tr', { hasText: '9999999999990088' });
  await expect(own.locator('.org-count')).toHaveText('🏢 2');
  await expect(page.locator('#f-multi')).toHaveCount(0);
  await own.locator('td.cell-sum').click();
  const drawer = page.locator('.drawer');
  await expect(drawer.locator('#rd-org-count')).toContainText('2');
  await expect(drawer).not.toContainText('E2E Бета');
});

// v1.70.87 (#25): дашборд, догрузившийся после перехода в другой раздел, не
// подменяет открытый раздел. Ответ статистики дашборда задерживаем, пока
// пользователь не откроет «Чеки», — гонка воспроизводится при любой скорости
// браузера, а не только на медленном.
test('дашборд: поздний ответ не подменяет открытый раздел', async ({ page }) => {
  let release;
  const held = new Promise((resolve) => { release = resolve; });
  await page.route('**/api/v1/dashboard/stats**', async (route) => {
    await held;
    await route.continue();
  });
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/receipts"]').click();
  await expect(page.locator('#rc-view-list')).toBeVisible();
  const late = page.waitForResponse((response) => response.url().includes('/api/v1/dashboard/stats'));
  release();
  await late;
  // ответ пришёл: даём дашборду время дорисоваться — до исправления он
  // подменял реестр через доли секунды после ответа
  await page.waitForTimeout(800);
  await expect(page.locator('#page-title')).toHaveText('База чеков');
  await expect(page.locator('#rc-view-list')).toBeVisible();
  await expect(page.locator('#btn-focus')).toHaveCount(0);
});

// v1.70.94 (T-53): первичка — администратор загружает электронный УПД (синтетика) и видит
// итог проверки в реестре и в карточке; тот же файл второй раз — «уже был загружен».
function updXml(number) {
  return '<?xml version="1.0" encoding="utf-8"?>'
    + '<Файл ИдФайл="ON_NSCHFDOPPR_E2E" ВерсФорм="5.03" ВерсПрог="e2e">'
    + '<Документ КНД="1115131" Функция="СЧФДОП" ДатаИнфПр="10.03.2026" ВремИнфПр="10.00.00" НаимЭконСубСост="Продавец">'
    + `<СвСчФакт НомерДок="${number}" ДатаДок="10.03.2026">`
    + '<СвПрод><ИдСв><СвЮЛУч НаимОрг="ООО «E2E Поставщик»" ИННЮЛ="5001007329" КПП="500101001"/></ИдСв></СвПрод>'
    + '<СвПокуп><ИдСв><СвЮЛУч НаимОрг="ООО «E2E Покупатель»" ИННЮЛ="7704001236" КПП="770401001"/></ИдСв></СвПокуп>'
    + '<ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/></СвСчФакт>'
    + '<ТаблСчФакт><СведТов НомСтр="1" НаимТов="E2E цемент М500" ОКЕИ_Тов="796" НаимЕдИзм="шт" КолТов="10" '
    + 'ЦенаТов="500.00" СтТовБезНДС="5000.00" НалСт="22%" СтТовУчНал="6100.00">'
    + '<Акциз><БезАкциз>без акциза</БезАкциз></Акциз><СумНал><СумНал>1100.00</СумНал></СумНал></СведТов>'
    + '<ВсегоОпл СтТовБезНДСВсего="5000.00" СтТовУчНалВсего="6100.00">'
    + '<СумНалВсего><СумНал>1100.00</СумНал></СумНалВсего></ВсегоОпл>'
    + '</ТаблСчФакт>'
    // v1.70.95: сведения о передаче и подписант — обязательные части по схеме ФНС 5.03
    + '<СвПродПер><СвПер СодОпер="Товары переданы" ДатаПер="10.03.2026">'
    + '<ОснПер РеквНаимДок="Договор поставки" РеквНомерДок="12" РеквДатаДок="01.02.2026"/></СвПер></СвПродПер>'
    + '<Подписант СпосПодтПолном="1" Должн="Директор"><ФИО Фамилия="Тестов" Имя="Тест"/></Подписант>'
    + '</Документ></Файл>';
}

test('первичка: администратор загружает УПД и видит итог проверки', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/primary"]').click();
  await expect(page.locator('#page-title')).toHaveText('Первичка');
  const number = `E2E-${Date.now()}`;          // повтор попытки не упрётся в дубль прошлой
  const file = { name: 'upd_e2e.xml', mimeType: 'text/xml', buffer: Buffer.from(updXml(number), 'utf-8') };
  await page.setInputFiles('#prim-file', file);
  await expect(page.locator('#prim-results')).toContainText('загружено 1');
  const row = page.locator('#prim-out tbody tr', { hasText: number });
  await expect(row.locator('.chip')).toHaveText('Без замечаний');
  await row.getByRole('button', { name: 'Открыть' }).click();
  const card = page.locator('.modal-slot');
  await expect(card).toContainText(`УПД № ${number} от 10.03.2026`);
  await expect(card).toContainText('E2E цемент М500');
  await expect(card).toContainText('Файл соответствует схеме ФНС 5.03');   // v1.70.95: сверка с XSD
  await card.locator('.modal-close').click();
  await page.setInputFiles('#prim-file', file);
  await expect(page.locator('#prim-results')).toContainText('уже был загружен');
});

// v1.70.95 (T-61): «комбайн первички» — PDF печатной формы распознаётся на своём сервере (без OCR:
// у PDF из 1С есть текстовый слой), администратор сверяет реквизиты в окне и сохраняет в реестр.
test('первичка: PDF печатной формы распознан, сверен и сохранён', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/primary"]').click();
  await expect(page.locator('#page-title')).toHaveText('Первичка');
  const stamp = Date.now();
  // комментарий в конце PDF меняет хеш файла: повтор попытки не упрётся в дубль прошлой
  const pdf = Buffer.concat([readFileSync('tests/fixtures/primary/upd_print_text.pdf'), Buffer.from(`\n% e2e ${stamp}\n`)]);
  const answer = page.waitForResponse((r) => r.url().includes('/api/v1/primary/recognize'), { timeout: 20_000 });
  await page.setInputFiles('#prim-file', { name: 'upd_print.pdf', mimeType: 'application/pdf', buffer: pdf });
  const response = await answer;
  // при сбое в отчёте сразу видно причину от сервера (так нашёлся лимит частоты запросов в CI)
  expect(response.status(), await response.text()).toBe(200);
  const modal = page.locator('.modal-slot');
  await expect(modal).toContainText('Сверьте реквизиты: upd_print.pdf', { timeout: 10_000 });
  await expect(modal).toContainText('Все основные поля найдены');
  await expect(page.locator('#prim-f-number')).toHaveValue('УТ-215');
  await expect(page.locator('#prim-f-seller-inn')).toHaveValue('5001007329');
  await expect(page.locator('#prim-f-total')).toHaveValue('105530,00');
  await expect(modal).toContainText('Арматура А500С d12 мм');
  const number = `УТ-215-${stamp}`;
  await page.fill('#prim-f-number', number);
  await page.getByRole('button', { name: 'Сохранить в реестр' }).click();
  await expect(page.locator('#prim-results')).toContainText('«upd_print.pdf»: сохранён в реестр');
  const row = page.locator('#prim-out tbody tr', { hasText: number });
  await expect(row).toContainText('PDF');
  await row.getByRole('button', { name: 'Открыть' }).click();
  await expect(page.locator('.modal-slot')).toContainText('Источник: PDF');
  await expect(page.locator('.modal-slot')).toContainText('Реквизиты распознаны из PDF и сверены пользователем');
});

// v1.70.96 (T-60): выгрузка в 1С — администратор отмечает документ в реестре, окно показывает, что уйдёт,
// «Скачать» отдаёт файл УПД 5.03 для типовой кнопки 1С «Загрузить → Из файла»; документ отмечен «выгружен».
test('первичка: отмеченный документ выгружается файлом УПД для 1С', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/primary"]').click();
  await expect(page.locator('#page-title')).toHaveText('Первичка');
  const number = `E2E-1C-${Date.now()}`;
  await page.setInputFiles('#prim-file', { name: 'upd_1c.xml', mimeType: 'text/xml', buffer: Buffer.from(updXml(number), 'utf-8') });
  await expect(page.locator('#prim-results')).toContainText('загружено 1');
  const row = page.locator('#prim-out tbody tr', { hasText: number });
  await row.locator('.prim-pick').check();
  await expect(page.locator('#prim-picked')).toHaveText('Отмечено: 1');
  await page.locator('#prim-export').click();
  const modal = page.locator('.modal-slot');
  await expect(modal.locator('#prim-exp-check')).toContainText('Будет выгружено: 1');
  const download = page.waitForEvent('download');
  await modal.locator('#prim-exp-go').click();
  const file = await download;
  expect(file.suggestedFilename()).toMatch(/^ON_NSCHFDOPPR_7704001236_5001007329_20260310_[0-9a-f-]{36}\.xml$/);
  const body = new TextDecoder('windows-1251').decode(readFileSync(await file.path()));
  expect(body).toContain('encoding="windows-1251"');
  expect(body).toContain('ВерсФорм="5.03"');
  expect(body).toContain(`НомерДок="${number}"`);
  await expect(row).toContainText('✓');
});

// v1.70.97 (T-58): «Что нужно от клиента» — администратор выбирает компанию, добавляет запрос документа,
// отмечает его полученным; в журнале — письменный след обоих событий.
test('что нужно от клиента: запрос документа, отметка «получено» и журнал', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/docreq"]').click();
  await expect(page.locator('#page-title')).toHaveText('Что нужно от клиента');
  await page.locator('#dreq-company').selectOption({ label: 'E2E Альфа' });
  await expect(page.locator('#dreq-head')).toContainText('Срок сдачи за');
  await expect(page.locator('#dreq-period')).toHaveValue(/^\d{4}-\d{2}$/);
  const title = `E2E акт сверки ${Date.now()}`;
  await page.getByRole('button', { name: '＋ Добавить запрос' }).click();
  await page.fill('#dreq-a-title', title);
  await page.fill('#dreq-a-amount', '1 500,00');
  await page.getByRole('button', { name: 'Добавить', exact: true }).click();
  const row = page.locator('#dreq-out tbody tr', { hasText: title });
  await expect(row).toContainText('✍️ запрос бухгалтера');
  await expect(row.locator('.chip')).toHaveText('Ждём');
  await row.getByRole('button', { name: '✓ Получено' }).click();
  await expect(row.locator('.chip')).toHaveText('Получено');
  const journal = page.locator('#dreq-journal');
  await expect(journal).toContainText('Запрос создан');
  await expect(journal).toContainText('Получено');
});

// v1.70.98: заставка загрузки — версия с сервера, финал «Готово к запуску», уход; вход доступен после неё
// v1.70.103 (T-74): гостю по умолчанию заставки нет — стартовая страница видна сразу; показ гостям включает флажок
test('заставка загрузки: версия с сервера, «Готово к запуску» и уход', async ({ page, request }) => {
  const about = await (await request.get('/api/v1/about')).json();
  expect(about.splash_guests).toBe(false);
  await page.goto('/');
  await expect(page.locator('h1.land-h1')).toContainText('Проверка чека онлайн');
  await expect(page.locator('#ym-splash')).toBeHidden();                         // гость: без заставки
  // Прежняя страница ещё может завершить запрос /about и вернуть флаг в false.
  // Для следующей загрузки фиксируем и сохранённое значение, и ответ сервера:
  // это воспроизводит реально включённый администратором режим без гонки.
  await page.route('**/api/v1/about', (route) => route.fulfill({ json: { ...about, splash_guests: true } }));
  await page.addInitScript(() => localStorage.setItem('ym-splash-guests', '1'));
  await page.reload();
  const splash = page.locator('#ym-splash');
  await expect(splash).toHaveClass(/is-ready/, { timeout: 15000 });
  await expect(page.locator('#ym-splash-version')).toHaveText(about.version);
  await expect(page.locator('#ym-splash-pct')).toHaveText('100 %');
  await expect(page.locator('#ym-splash-status')).toHaveText('Готово к запуску');
  await expect(page.locator('.ym-splash__meta')).toContainText('Выпуск 2026');
  await expect(page.locator('.ym-splash__meta')).toContainText('ООО «Ямастер»');
  await expect(splash).toHaveClass(/is-gone/, { timeout: 15000 });
  await expect(splash).toBeHidden();
  await expect(page.locator('#boot-splash')).toHaveCount(0);          // прежней заставки больше нет
  await page.click('#login-username');                                 // клики доходят до формы входа
  await expect(page.locator('#login-username')).toBeFocused();
});

// v1.70.98 (T-45): выписка в формате 1С — платёж без документов попадает в «Что нужно от клиента»
test('банк: платёж без документов из выписки уходит в «Что нужно от клиента»', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/bank"]').click();
  await expect(page.locator('#page-title')).toHaveText('Банк: выписка');
  await page.locator('#bank-company').selectOption({ label: 'E2E Альфа' });
  const stamp = String(Date.now() % 1000000);
  const statementText = ['1CClientBankExchange', 'ВерсияФормата=1.03', 'Кодировка=Windows', 'ДатаНачала=01.10.2026',
    'ДатаКонца=31.10.2026', 'РасчСчет=40702810900000012345', 'СекцияДокумент=Платежное поручение', `Номер=${stamp}`,
    'Дата=12.10.2026', 'Сумма=15000.00', 'ПлательщикСчет=40702810900000012345', 'Плательщик1=E2E Альфа',
    'ПолучательСчет=40702810100000054321', 'ПолучательИНН=7702345678', 'Получатель1=ООО «Песок E2E»',
    'ДатаСписано=12.10.2026', `НазначениеПлатежа=Оплата по счёту № ${stamp} за песок, в т.ч. НДС 22%`,
    'КонецДокумента', 'КонецФайла'].join('\r\n');
  await page.setInputFiles('#bank-file', { name: 'kl_to_1c.txt', mimeType: 'text/plain',
    buffer: Buffer.from(statementText, 'utf8') });
  await expect(page.locator('#bank-result')).toContainText('без документов: 1', { timeout: 15000 });
  const row = page.locator('#bank-out tbody tr', { hasText: 'ООО «Песок E2E»' });
  await expect(row).toContainText('15 000,00');
  await expect(row.locator('.chip')).toHaveText('Нет документов');
  await page.locator('a[href="#/docreq"]').click();
  await page.locator('#dreq-company').selectOption({ label: 'E2E Альфа' });
  await page.locator('#dreq-period').fill('2026-10');
  const req = page.locator('#dreq-out tbody tr', { hasText: 'ООО «Песок E2E»' });
  await expect(req).toContainText('🏦 платёж без документов', { timeout: 15000 });
  await expect(req).toContainText('Закрывающие документы');
});

// v1.70.98: прямая ссылка после перезагрузки — список компаний администратора подтягивается сам
test('прямая ссылка: у администратора список компаний не пустой', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.evaluate(() => { window.location.hash = '#/docreq'; });
  await page.reload();
  await expect(page.locator('#page-title')).toHaveText('Что нужно от клиента', { timeout: 15000 });
  // v1.70.107: список компаний приходит отдельным запросом после перезагрузки — под нагрузкой полного прогона
  // 5 секунд по умолчанию не всегда хватало (в одиночку сценарий проходит); ждём столько же, сколько заголовок
  await expect(page.locator('#dreq-company option', { hasText: 'E2E Альфа' })).toHaveCount(1, { timeout: 15000 });
});

// v1.70.99 (ADR-29): банк по API — в карточке компании выбирается Точка и способ доступа; секрет
// сохраняется, но на страницу не возвращается. «Подключить» не нажимаем: это вход в настоящий банк.
test('банк по API: карточка компании — Точка, OAuth 2.0, секрет не возвращается', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/companies"]').click();
  await expect(page.locator('#page-title')).toHaveText('Компании');
  await page.locator('#cp-table tr', { hasText: 'E2E Бета' }).locator('.cp-card').click();
  const panel = page.locator('#cc-bank');
  await expect(panel).toContainText('Банк по API', { timeout: 15000 });
  await expect(panel).toContainText('не подключён');
  await expect(panel.locator('#bankc-bank')).toHaveValue('tochka');
  await expect(panel.locator('.bank-conn-uri')).toContainText('/api/v1/bank/tochka/callback');
  await panel.locator('#bankc-client').fill('e2e-app-synthetic');
  await panel.locator('#bankc-secret').fill('s3cret-e2e-synthetic');
  await panel.locator('#bankc-save').click();
  await expect(panel.locator('#bankc-secret')).toHaveAttribute('placeholder', /задан/, { timeout: 15000 });
  await expect(panel.locator('#bankc-secret')).toHaveValue('');
  await expect(panel.locator('#bankc-client')).toHaveValue('e2e-app-synthetic');
  expect(await page.content()).not.toContain('s3cret-e2e-synthetic');
  await panel.locator('#bankc-method').selectOption('jwt');
  await expect(panel.locator('#bankc-jwt')).toBeVisible();
  await expect(panel.locator('#bankc-auth')).toBeHidden();
  page.once('dialog', (dialog) => dialog.accept());
  await panel.locator('#bankc-del').click();
  await expect(panel).toContainText('не подключён', { timeout: 15000 });
  await expect(panel.locator('#bankc-del')).toHaveCount(0);
});

// v1.70.102 (ADR-32): несколько банков у компании — в карточке выбирается Т-Банк и вводится ключ API; ключ не
// возвращается, банк встаёт в список, у Сбера обязателен сертификат; банк отключается отдельно. Ключ синтетический,
// в банк сценарий не ходит (только сохранение).
test('банки по API: карточка компании — Т-Банк по ключу API, список банков, ключ не возвращается', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/companies"]').click();
  await expect(page.locator('#page-title')).toHaveText('Компании');
  await page.locator('#cp-table tr', { hasText: 'E2E Бета' }).locator('.cp-card').click();
  const panel = page.locator('#cc-bank');
  await expect(panel).toContainText('Банк по API', { timeout: 15000 });
  await panel.locator('#bankc-bank').selectOption('tbank');
  await expect(panel.locator('#bankc-token')).toBeVisible({ timeout: 15000 });
  await expect(panel.locator('#bankc-auth')).toBeHidden();
  await expect(panel).toContainText('Выпуск токена');
  await panel.locator('#bankc-token').fill('t.e2e-synthetic-token-102');
  await panel.locator('#bankc-save').click();
  await expect(panel.locator('#bankc-token')).toHaveAttribute('placeholder', /ключ задан/, { timeout: 15000 });
  expect(await page.content()).not.toContain('t.e2e-synthetic-token-102');
  await expect(panel.locator('.bank-conn-row', { hasText: 'Т-Банк' })).toHaveCount(1);
  await panel.locator('#bankc-bank').selectOption('sber');
  await expect(panel.locator('#bankc-cert-box')).toContainText('обязательно', { timeout: 15000 });
  await expect(panel.locator('#bankc-refresh')).toBeVisible();
  await panel.locator('.bank-conn-open[data-bank="tbank"]').click();
  await expect(panel.locator('#bankc-bank')).toHaveValue('tbank', { timeout: 15000 });
  page.once('dialog', (dialog) => dialog.accept());
  await panel.locator('#bankc-del').click();
  await expect(panel.locator('.bank-conn-row')).toHaveCount(0, { timeout: 15000 });
});

// v1.70.100 (T-50, ADR-30): рабочее место аутсорсера — клиента бухгалтеру назначает администратор,
// бухгалтер видит все компании в одном окне и переключается между ними без перевхода
test('рабочее место аутсорсера: назначение клиента, «Клиенты» и переключение без перевхода', async ({ page, request }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/users"]').click();
  await expect(page.locator('#page-title')).toHaveText('Пользователи и приглашения');
  await page.locator('tr', { hasText: 'e2e_acc_alpha' }).locator('.u-clients').click();
  const modal = page.locator('.modal-slot');
  await expect(modal).toContainText('Клиенты бухгалтера', { timeout: 15000 });
  await expect(modal.locator('label', { hasText: 'E2E Альфа' })).toHaveCount(0);   // своя компания — всегда доступна
  await modal.locator('label', { hasText: 'E2E Бета' }).locator('input').check();
  await expect(modal.locator('#cla-count')).toHaveText('Выбрано клиентов: 1');
  await modal.locator('#cla-save').click();
  await expect(page.locator('#toast-container')).toContainText('Клиентов у бухгалтера: 1');
  await page.locator('#btn-logout').click();
  const reloaded = page.waitForEvent('load');
  await page.locator('#lo-exit').click();
  await reloaded;
  await page.fill('#login-username', 'e2e_acc_alpha');
  await page.fill('#login-password', 'e2e-accountant-password');
  await page.click('#login-submit');
  await expect(page.locator('#login-form')).toBeHidden({ timeout: 15000 });
  const sw = page.locator('#client-switch');
  await expect(sw).toBeVisible({ timeout: 15000 });
  await expect(sw.locator('option')).toHaveCount(2);
  await page.locator('a[href="#/clients"]').click();
  await expect(page.locator('#page-title')).toHaveText('Клиенты');
  await expect(page.locator('.cl-card')).toHaveCount(2, { timeout: 15000 });
  await page.locator('.cl-card', { hasText: 'E2E Бета' }).locator('[data-open="docreq"]').click();
  await expect(page.locator('#page-title')).toHaveText('Что нужно от клиента', { timeout: 15000 });
  await expect(sw.locator('option:checked')).toHaveText('E2E Бета');
  await sw.selectOption({ label: 'E2E Альфа · своя' });
  await expect(sw.locator('option:checked')).toHaveText('E2E Альфа · своя');
  // уборка: снять назначение, чтобы следующие сценарии видели бухгалтера как прежде
  const auth = await request.post('/api/v1/auth/login', { data: { username: 'admin', password } });
  const { access_token: token } = await auth.json();
  const users = await (await request.get('/api/v1/users', { headers: { Authorization: `Bearer ${token}` } })).json();
  const acc = users.find((u) => u.username === 'e2e_acc_alpha');
  const reset = await request.put(`/api/v1/clients/access/${acc.id}`, {
    headers: { Authorization: `Bearer ${token}` }, data: { company_ids: [] } });
  expect(reset.ok()).toBeTruthy();
});

// v1.71.1 (T-84): бухгалтер заводит клиента сам с экрана «Клиенты» — компания сразу в списке и в шапке,
// без действий администратора и без перевхода
test('создание клиента бухгалтером: кнопка «＋ Новый клиент» и работа в нём сразу', async ({ page, request }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('#btn-logout').click();
  const reloaded = page.waitForEvent('load');
  await page.locator('#lo-exit').click();
  await reloaded;
  await page.fill('#login-username', 'e2e_acc_alpha');
  await page.fill('#login-password', 'e2e-accountant-password');
  await page.click('#login-submit');
  await expect(page.locator('#login-form')).toBeHidden({ timeout: 15000 });
  await page.locator('a[href="#/clients"]').click();
  await expect(page.locator('#page-title')).toHaveText('Клиенты');
  const name = `E2E Новый клиент ${Date.now()}`;
  await page.locator('#cl-new').click();
  const modal = page.locator('.modal-slot');
  await expect(modal).toContainText('Новый клиент');
  await modal.locator('#clc-name').fill(name);
  await modal.locator('#clc-note').fill('договор № 1');
  await modal.locator('#clc-save').click();
  await expect(page.locator('#toast-container')).toContainText('добавлен', { timeout: 15000 });
  // компания сразу в списке и в переключателе — перевхода не было
  await expect(page.locator('.cl-card', { hasText: name })).toHaveCount(1, { timeout: 15000 });
  const sw = page.locator('#client-switch');
  await expect(sw.locator('option', { hasText: name })).toHaveCount(1, { timeout: 15000 });
  await page.locator('.cl-card', { hasText: name }).locator('[data-open="docreq"]').click();
  await expect(page.locator('#page-title')).toHaveText('Что нужно от клиента', { timeout: 15000 });
  await expect(sw.locator('option:checked')).toHaveText(name);
  // уборка: компании, заведённые сценарием, в архив (данные синтетические — ADR-14)
  const auth = await request.post('/api/v1/auth/login', { data: { username: 'admin', password } });
  const { access_token: token } = await auth.json();
  const companies = await (await request.get('/api/v1/companies', { headers: { Authorization: `Bearer ${token}` } })).json();
  const mine = companies.find((c) => c.name === name);
  expect(mine).toBeTruthy();
  const arch = await request.patch(`/api/v1/companies/${mine.id}`,
    { headers: { Authorization: `Bearer ${token}` }, data: { is_active: false } });
  expect(arch.ok()).toBeTruthy();
});

// v1.71.2 (T-85, ADR-50): удаление компании-клиента не оставляет «сирот» — в окне удаления сказано,
// что уйдёт вместе с компанией; после удаления компания пропадает из списка и из базы
test('удаление клиента: пояснение в окне и чистое удаление', async ({ page, request }) => {
  const auth = await request.post('/api/v1/auth/login', { data: { username: 'admin', password } });
  const { access_token: token } = await auth.json();
  const H = { Authorization: `Bearer ${token}` };
  const name = `E2E Удаляемый клиент ${Date.now()}`;
  const created = await request.post('/api/v1/companies', { headers: H, data: { name } });
  expect(created.ok()).toBeTruthy();
  const company = await created.json();

  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/companies"]').click();
  await expect(page.locator('#page-title')).toHaveText('Компании');
  await page.locator('#cp-table tr', { hasText: name }).locator('.cp-card').click();
  await page.locator('#cc-delete').click();
  const modal = page.locator('.modal-slot');
  await expect(modal).toContainText('Удаление компании');
  // пояснение: что уходит вместе с компанией и что остаётся
  await expect(modal).toContainText('банковские выписки');
  await expect(modal).toContainText('история решений');
  await modal.locator('input[name="dl-mode"][value="wipe"]').check();
  page.once('dialog', (d) => d.accept());
  await modal.locator('#dl-go').click();
  await expect(page.locator('#toast-container')).toContainText('удалены безвозвратно', { timeout: 15000 });
  await expect(page.locator('#cp-table tr', { hasText: name })).toHaveCount(0, { timeout: 15000 });

  // в базе компании больше нет, и запрос к ней — 404
  const companies = await (await request.get('/api/v1/companies', { headers: H })).json();
  expect(companies.find((c) => c.id === company.id)).toBeFalsy();
  const again = await request.get(`/api/v1/companies/${company.id}/card`, { headers: H });
  expect(again.status()).toBe(404);
});

// v1.71.3 (T-86): согласования авансовых отчётов видны в сводке клиентов — плитка «Согласования»
// показывает, сколько отчётов ждут бухгалтера, и открывает экран «Согласования» уже в этой компании
test('клиенты: согласования ждут бухгалтера и открываются одной кнопкой', async ({ page, request }) => {
  const auth = await request.post('/api/v1/auth/login', { data: { username: 'admin', password } });
  const { access_token: token } = await auth.json();
  const H = { Authorization: `Bearer ${token}` };
  const companies = await (await request.get('/api/v1/companies', { headers: H })).json();
  const company = companies.find((c) => c.name === 'E2E Согласование');
  expect(company).toBeTruthy();
  // плитка есть только там, где согласование включено: включаем через API, не заходя в настройки
  const settings = await request.put('/api/v1/approvals/settings', {
    headers: H, data: { company_id: company.id, enabled: true, timeout_hours: 72, manager_limit: 0,
      accountant_limit: 0, channels: [] } });
  expect(settings.ok()).toBeTruthy();

  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/clients"]').click();
  await expect(page.locator('#page-title')).toHaveText('Клиенты');
  const card = page.locator('.cl-card', { hasText: 'E2E Согласование' });
  await expect(card).toBeVisible({ timeout: 15000 });
  // согласование в этой компании включено (сценарий согласования идёт выше) — плитка есть и переход работает
  await expect(card.locator('[data-open="approvals"]')).toBeVisible({ timeout: 15000 });
  await card.locator('[data-open="approvals"]').click();
  await expect(page.locator('#page-title')).toHaveText('Согласования', { timeout: 15000 });
});

// v1.70.101 (T-69, ADR-31): понимание чеков — читаемая позиция и категория с причиной; бухгалтер меняет категорию,
// выбор запоминается для компании и виден в «Настройках» → «Память товаров». Чек — синтетика через API.
test('понимание чеков: категория позиции, выбор бухгалтера и «Память товаров»', async ({ page, request }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  const accAuth = await request.post('/api/v1/auth/login',
    { data: { username: 'e2e_acc_alpha', password: 'e2e-accountant-password' } });
  const { access_token: accToken } = await accAuth.json();
  const H = { Authorization: `Bearer ${accToken}` };
  const created = await request.post('/api/v1/receipts/manual', { headers: H, data: {
    date_time: '04.11.2026 12:10', total_sum: 9150, fn: '9999999999990101', fd: '101', fp: '9990101' } });
  expect(created.ok()).toBeTruthy();
  const rid = (await created.json()).receipt.id;
  const patched = await request.patch(`/api/v1/receipts/${rid}`, { headers: H, data: { items: [
    { name: 'ПЕРФОРАТОР BOSCH GBH 2-26 SDS-PLUS', quantity: 1, price: 9000, total: 9000 },
    { name: 'Абвгд ёжз 12', quantity: 1, price: 150, total: 150 }] } });
  expect(patched.ok()).toBeTruthy();
  await page.locator('#btn-logout').click();
  const reloaded = page.waitForEvent('load');
  await page.locator('#lo-exit').click();
  await reloaded;
  await page.fill('#login-username', 'e2e_acc_alpha');
  await page.fill('#login-password', 'e2e-accountant-password');
  await page.click('#login-submit');
  await expect(page.locator('#login-form')).toBeHidden({ timeout: 15000 });
  await page.locator('a[href="#/receipts"]').click();
  await page.locator('#rc-view-list').click();
  await page.locator('#receipts-table tbody tr', { hasText: '9999999999990101' }).locator('td.cell-sum').click();
  const drawer = page.locator('.drawer');
  await expect(drawer.locator('.gd-name').first()).toHaveText('Перфоратор BOSCH GBH 2-26 SDS-PLUS');
  await expect(drawer.locator('[data-gd-item]').first()).toContainText('Инструмент');
  await drawer.locator('[data-gd-item]').nth(1).click();
  await drawer.locator('.gd-pick').selectOption('build_finish');
  await expect(page.locator('#toast-container')).toContainText('запомнено для компании');
  await expect(drawer.locator('[data-gd-item]').nth(1)).toContainText('Отделочные материалы');
  await drawer.locator('.modal-close').click();
  await page.locator('a[href="#/settings"]').click();
  await expect(page.locator('#st-goods')).toContainText('ключ: абвгд ежз', { timeout: 15000 });
});

// v1.70.112 (T-46, ADR-24): согласование авансового отчёта — администратор включает согласование в компании,
// сотрудник собирает отчёт из своих чеков и подписывает простой электронной подписью, руководитель и бухгалтер
// согласуют; в журнале — три подписи с отметкой ПЭП, печати целостности сходятся. Данные — синтетические.
async function approvalsRelogin(page, username) {
  const open = page.locator('#modal-root:not(.hidden) .modal-close');
  if (await open.count()) await open.click();                     // окно отчёта закрывает кнопку выхода
  await expect(page.locator('#modal-root')).toBeHidden();
  await page.locator('#btn-logout').click();
  const reloaded = page.waitForEvent('load');
  await page.locator('#lo-exit').click();
  await reloaded;
  await page.fill('#login-username', username);
  await page.fill('#login-password', 'e2e-approval-password');
  await page.click('#login-submit');
  // локальный Chromium из npm после перезагрузки бывает занят 15 с и дольше (см. playwright.config.js) — запас;
  // в CI экран входа уходит за доли секунды
  await expect(page.locator('#login-screen')).toBeHidden({ timeout: 45000 });
  await page.locator('a[href="#/approvals"]').click();
  await expect(page.locator('#page-title')).toHaveText('Согласования', { timeout: 15000 });
}

async function approvalsDecide(page, button) {
  const card = page.locator('.ap-card', { hasText: 'E2E Сотрудник Согласования' }).first();
  await expect(card).toBeVisible({ timeout: 15000 });
  await card.click();
  const modal = page.locator('.modal-slot');
  await expect(modal).toContainText('Журнал решений', { timeout: 15000 });
  await modal.locator('#ap-agree').check();
  await modal.getByRole('button', { name: button }).click();
  return modal;
}

test('согласование авансового отчёта: сотрудник → руководитель → бухгалтер с простой подписью', async ({ page }) => {
  test.slow();                                                     // четыре входа подряд — тройной лимит времени
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/approvals"]').click();
  await expect(page.locator('#page-title')).toHaveText('Согласования');
  await page.locator('#ap-company').selectOption({ label: 'E2E Согласование' });
  const settings = page.locator('details.ap-conf');
  await expect(settings).toBeVisible();
  if (!(await settings.evaluate((el) => el.open))) await settings.locator('summary').click();
  await page.locator('#ap-enabled').check();
  await page.locator('#ap-settings').getByRole('button', { name: 'Сохранить' }).click();
  await expect(page.locator('#toast-container')).toContainText('Согласование включено', { timeout: 15000 });

  await approvalsRelogin(page, 'e2e_emp_appr');
  await page.getByRole('button', { name: '＋ Авансовый отчёт' }).click();
  const create = page.locator('.modal-slot');
  await create.locator('#ap-from').fill('2026-10-01');
  await create.locator('#ap-to').fill('2026-10-31');
  await create.getByRole('button', { name: 'Показать чеки' }).click();
  await expect(create.locator('#ap-preview')).toContainText('2 чека', { timeout: 15000 });
  await create.getByRole('button', { name: 'Сформировать' }).click();
  const draft = page.locator('.modal-slot');
  await expect(draft).toContainText('ждёт подписи сотрудника', { timeout: 15000 });
  await expect(draft.locator('#ap-do-sign')).toBeDisabled();
  await draft.locator('#ap-agree').check();
  await draft.getByRole('button', { name: 'Подписать и отправить' }).click();
  await expect(page.locator('#toast-container')).toContainText('Отчёт подписан и отправлен', { timeout: 15000 });
  await expect(page.locator('.modal-slot')).toContainText('на согласовании', { timeout: 15000 });

  await approvalsRelogin(page, 'e2e_boss_appr');
  await approvalsDecide(page, 'Согласовать');
  await expect(page.locator('#toast-container')).toContainText('Отчёт согласован', { timeout: 15000 });

  await approvalsRelogin(page, 'e2e_acc_appr');
  const done = await approvalsDecide(page, 'Согласовать');
  await expect(done).toContainText('согласован', { timeout: 15000 });
  await expect(done.locator('.ap-pep')).toHaveCount(3, { timeout: 15000 });
  await expect(done).toContainText('Содержимое и журнал целы');
  await expect(done.locator('.ap-step.ap-done')).toHaveCount(3);
});


// v1.70.113 (T-79, #63): папка с УПД — браузер отбирает УПД среди файлов (титул покупателя и подпись пропускает),
// документы ложатся в компанию по ИНН; затем выписка и вкладка «Закрытие документами»: платёж закрыт двумя УПД.
const CLOSING_INN = '7703001018';
const CLOSING_SUPPLIER = '5002002027';
function closingUpd(number, day, base, id) {
  const vat = base * 22 / 100;
  return `<?xml version="1.0" encoding="utf-8"?>
<Файл ИдФайл="${id}" ВерсФорм="5.03" ВерсПрог="E2E"><Документ КНД="1115131" Функция="СЧФДОП" ДатаИнфПр="${day}" ВремИнфПр="10.00.00" НаимЭконСубСост="Продавец"><СвСчФакт НомерДок="${number}" ДатаДок="${day}"><СвПрод><ИдСв><СвЮЛУч НаимОрг="ООО «E2E Поставщик»" ИННЮЛ="${CLOSING_SUPPLIER}" КПП="500201001"/></ИдСв><Адрес><АдрИнф КодСтр="643" НаимСтран="Россия" АдрТекст="г. Тест"/></Адрес></СвПрод><СвПокуп><ИдСв><СвЮЛУч НаимОрг="E2E Закрытие" ИННЮЛ="${CLOSING_INN}" КПП="770301001"/></ИдСв><Адрес><АдрИнф КодСтр="643" НаимСтран="Россия" АдрТекст="г. Тест"/></Адрес></СвПокуп><ДенИзм КодОКВ="643" НаимОКВ="Российский рубль"/></СвСчФакт><ТаблСчФакт><СведТов НомСтр="1" НаимТов="Материалы" ОКЕИ_Тов="796" НаимЕдИзм="шт" КолТов="1" ЦенаТов="${base.toFixed(2)}" СтТовБезНДС="${base.toFixed(2)}" НалСт="22%" СтТовУчНал="${(base + vat).toFixed(2)}"><Акциз><БезАкциз>без акциза</БезАкциз></Акциз><СумНал><СумНал>${vat.toFixed(2)}</СумНал></СумНал></СведТов><ВсегоОпл СтТовБезНДСВсего="${base.toFixed(2)}" СтТовУчНалВсего="${(base + vat).toFixed(2)}"><СумНалВсего><СумНал>${vat.toFixed(2)}</СумНал></СумНалВсего></ВсегоОпл></ТаблСчФакт><СвПродПер><СвПер СодОпер="Товары переданы" ДатаПер="${day}"><ОснПер РеквНаимДок="Договор поставки" РеквНомерДок="12" РеквДатаДок="01.02.2026"/></СвПер></СвПродПер><Подписант СпосПодтПолном="1" Должн="Директор"><ФИО Фамилия="Тестов" Имя="Тест"/></Подписант></Документ></Файл>`;
}

test('папка с УПД: документы по ИНН компании и «Закрытие документами» в выписке', async ({ page }) => {
  test.slow();
  const root = mkdtempSync(join(tmpdir(), 'ym-upd-folder-'));
  // имена папок — латиницей: Playwright с локальным Chromium зависает на пути папки с кириллицей и пробелом
  // (у пользователя папку выбирает сам браузер — там имена любые)
  const folder = join(root, 'edo-oktyabr');
  mkdirSync(join(folder, 'supplier'), { recursive: true });
  writeFileSync(join(folder, 'supplier', 'ON_NSCHFDOPPR_e2e_1.xml'), closingUpd('E-101', '05.10.2026', 50000, 'ON_NSCHFDOPPR_e2e_1'));
  writeFileSync(join(folder, 'supplier', 'ON_NSCHFDOPPR_e2e_2.xml'), closingUpd('E-102', '15.10.2026', 30000, 'ON_NSCHFDOPPR_e2e_2'));
  writeFileSync(join(folder, 'supplier', 'ON_NSCHFDOPPOK_e2e_1.xml'), closingUpd('E-101', '05.10.2026', 50000, 'ON_NSCHFDOPPOK_e2e_1'));
  writeFileSync(join(folder, 'supplier', 'ON_NSCHFDOPPR_e2e_1.xml.sig'), 'подпись');
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/primary"]').click();
  await expect(page.locator('#page-title')).toHaveText('Первичка');
  await page.locator('#prim-folder').setInputFiles(folder);
  const results = page.locator('#prim-results');
  await expect(results).toContainText('УПД загружено: 2', { timeout: 30000 });
  await expect(results).toContainText('титулы покупателя: 1');
  await expect(results.locator('table')).toContainText('E2E Закрытие');

  await page.locator('a[href="#/bank"]').click();
  await expect(page.locator('#page-title')).toHaveText('Банк: выписка');
  await page.locator('#bank-company').selectOption({ label: 'E2E Закрытие' });
  const account = '40702810900000000777';
  const lines = ['1CClientBankExchange', 'ВерсияФормата=1.03', 'Кодировка=Windows', 'ДатаНачала=01.10.2026',
    'ДатаКонца=31.10.2026', `РасчСчет=${account}`, 'СекцияРасчСчет', 'ДатаНачала=01.10.2026', 'ДатаКонца=31.10.2026',
    `РасчСчет=${account}`, 'НачальныйОстаток=500000.00', 'КонецРасчСчет', 'СекцияДокумент=Платежное поручение', 'Номер=1',
    'Дата=10.10.2026', 'Сумма=97600.00', `ПлательщикСчет=${account}`, `ПлательщикИНН=${CLOSING_INN}`, 'Плательщик1=E2E Закрытие',
    'ПолучательСчет=40702810100000000555', `ПолучательИНН=${CLOSING_SUPPLIER}`, 'Получатель1=ООО «E2E Поставщик»',
    'ДатаСписано=10.10.2026', 'НазначениеПлатежа=Оплата по счёту 12 за материалы', 'КонецДокумента', 'КонецФайла'];
  await page.locator('#bank-file').setInputFiles({ name: 'kl_to_1c.txt', mimeType: 'text/plain', buffer: Buffer.from(lines.join('\r\n'), 'utf-8') });
  await expect(page.locator('#bank-result')).toContainText('Выписка', { timeout: 30000 });
  await page.locator('#bank-tab-closing').click();
  // период — явно: по умолчанию «с начала года по сегодня» зависит от часов машины, где идёт прогон
  await page.locator('#bank-cl-from').fill('2026-10-01');
  await page.locator('#bank-cl-from').dispatchEvent('change');
  await expect(page.locator('#bank-cl-from')).toHaveValue('2026-10-01', { timeout: 15000 });
  await page.locator('#bank-cl-to').fill('2026-10-31');
  await page.locator('#bank-cl-to').dispatchEvent('change');
  await expect(page.locator('#bank-cl-to')).toHaveValue('2026-10-31', { timeout: 15000 });
  const sum = page.locator('#bank-cl-sum');
  await expect(sum).toContainText('Расходы: 1', { timeout: 15000 });
  await expect(sum).toContainText('(100 %)');
  const row = page.locator('.bank-cl-table tbody tr').first();
  await expect(row).toContainText('закрыт');
  await expect(row).toContainText('УПД № E-101 от 05.10.2026');
  await expect(row).toContainText('сумма нескольких документов');
});

// T-88: старый клиент работает без договора; сохранение и предупреждение в карточке.
test('клиенты: договор и тариф сохраняются, срок виден на экране', async ({ page, request }) => {
  const auth = await request.post('/api/v1/auth/login', { data: { username: 'admin', password } });
  const { access_token: token } = await auth.json();
  const H = { Authorization: `Bearer ${token}` };
  const name = `E2E Договор ${Date.now()}`;
  const created = await request.post('/api/v1/companies', { headers: H, data: { name } });
  expect(created.ok()).toBeTruthy();
  const company = await created.json();
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/clients"]').click();
  const card = page.locator('.cl-card', { hasText: name });
  await expect(card).toContainText('Договор не заполнен', { timeout: 15000 });
  await card.locator('[data-contract]').click();
  await page.locator('#contract-number').fill('E2E-1');
  await page.locator('#contract-date').fill('2025-01-01');
  await page.locator('#contract-since').fill('2025-01-01');
  await page.locator('#contract-until').fill('2025-12-31');
  await page.locator('#contract-fee').fill('12500,50');
  await page.locator('#contract-frequency').selectOption('month');
  await page.locator('#contract-form button[type="submit"]').click();
  await expect(card).toContainText('E2E-1', { timeout: 15000 });
  await expect(card).toContainText('Дата окончания по данным карточки прошла');
  const stored = await (await request.get(`/api/v1/clients/${company.id}/contract`, { headers: H })).json();
  expect(stored.contract.fee).toBe('12500.50');
  await card.locator('[data-contract]').click();
  await expect(page.locator('#contract-number')).toHaveValue('E2E-1');
  await page.locator('#contract-cancel').click();
});

// T-90: сформировать защищённую печатную сводку в браузере, без внешних адресатов.
test('клиенты: сводный отчёт открывается для печати и PDF', async ({ page }) => {
  await login(page);
  await page.getByRole('button', { name: 'Понятно, работаем' }).click();
  await page.locator('a[href="#/clients"]').click();
  await expect(page.locator('#page-title')).toHaveText('Клиенты');
  const period = await page.locator('#cl-period').inputValue();
  const popupPromise = page.waitForEvent('popup');
  await page.locator('#cl-report').click();
  const popup = await popupPromise;
  await expect(popup.locator('h1')).toHaveText('Сводка по клиентам', { timeout: 15000 });
  await expect(popup.locator('body')).toContainText('на момент формирования');
  await expect(popup.locator('body')).toContainText('не бухгалтерская отчётность');
  expect(popup.url()).toMatch(/^blob:/);
  expect(popup.url()).not.toContain('token');
  expect((await popup.pdf({ format: 'A4', landscape: true })).length).toBeGreaterThan(500);
  expect(period).toMatch(/^\d{4}-\d{2}$/);
  await popup.close();
});

// T-91: настоящий браузер и настоящий WebSocket, без моков; база Playwright временная и синтетическая.
test('живые обновления: чек выбранного клиента появляется без перезагрузки', async ({ page, request }) => {
  const auth = await request.post('/api/v1/auth/login', { data: { username: 'admin', password } });
  const { access_token: token } = await auth.json();
  const headers = { Authorization: `Bearer ${token}` };
  const users = await (await request.get('/api/v1/users', { headers })).json();
  const acc = users.find((u) => u.username === 'e2e_acc_alpha');
  const previous = await (await request.get(`/api/v1/clients/access/${acc.id}`, { headers })).json();
  const created = await request.post('/api/v1/companies', { headers, data: { name: `E2E живой клиент ${Date.now()}` } });
  expect(created.ok()).toBeTruthy();
  const company = await created.json();
  const granted = await request.put(`/api/v1/clients/access/${acc.id}`, {
    headers, data: { company_ids: [...previous.company_ids, company.id] },
  });
  expect(granted.ok()).toBeTruthy();
  const frames = [];
  page.on('websocket', (socket) => socket.on('framereceived', ({ payload }) => {
    try { frames.push(JSON.parse(String(payload))); } catch { /* heartbeat */ }
  }));
  try {
    await page.goto('/');
    await page.fill('#login-username', 'e2e_acc_alpha');
    await page.fill('#login-password', 'e2e-accountant-password');
    const loginResult = page.waitForResponse((response) => response.url().includes('/api/v1/auth/login')
      && response.request().method() === 'POST');
    await page.click('#login-submit');
    const loginResponse = await loginResult;
    expect(loginResponse.ok(), `Вход бухгалтера: HTTP ${loginResponse.status()} ${await loginResponse.text()}`).toBeTruthy();
    await expect(page.locator('#login-form')).toBeHidden({ timeout: 15000 });
    await page.getByRole('button', { name: 'Понятно, работаем' }).click();
    await expect.poll(() => frames.some((f) => f.type === 'connected'), { timeout: 15000 }).toBe(true);
    const sw = page.locator('#client-switch');
    await expect(sw.locator('option', { hasText: company.name })).toHaveCount(1, { timeout: 15000 });
    await sw.selectOption(company.id);
    await expect.poll(() => frames.some((f) => f.type === 'client_ok' && f.payload.company === company.id),
      { timeout: 15000 }).toBe(true);
    await page.locator('a[href="#/receipts"]').click();
    await expect(page.locator('#page-title')).toHaveText('База чеков');
    await page.locator('#rc-view-list').click();
    await expect(page.locator('#receipts-table')).toBeVisible();
    const fn = `99${String(Date.now()).slice(-14).padStart(14, '0')}`;
    const result = await request.post('/api/v1/receipts/manual', { headers, data: {
      date_time: '04.11.2026 12:10', total_sum: 101.25, fn, fd: '99831', fp: '9983101', company_id: company.id,
    } });
    expect(result.ok()).toBeTruthy();
    const receipt = (await result.json()).receipt;
    expect(receipt.company_id).toBe(company.id);
    await expect.poll(() => frames.some((f) => f.type === 'receipt_created' && f.payload.id === receipt.id),
      { timeout: 15000 }).toBe(true);
    await expect(page.locator('#receipts-table tbody tr', { hasText: fn })).toHaveCount(1, { timeout: 20000 });
  } finally {
    await request.put(`/api/v1/clients/access/${acc.id}`, { headers, data: { company_ids: previous.company_ids } });
    await request.patch(`/api/v1/companies/${company.id}`, { headers, data: { is_active: false } });
  }
});

test.describe('переход к форме входа с главной', () => {
  for (const viewport of [{ width: 1280, height: 800 }, { width: 390, height: 700 }]) {
    test(`ссылка входа и история, экран ${viewport.width}`, async ({ page }) => {
      await page.setViewportSize(viewport);
      await page.goto('/');
      await page.locator('.home-header__login').click();
      await expect(page).toHaveURL(/#land-login$/);
      await expect(page.locator('#login-username')).toBeInViewport();
      await expect(page.locator('#public-screen')).toBeHidden();
      await page.goBack();
      await expect(page.locator('#login-screen')).toBeVisible();
      await page.goForward();
      await expect(page.locator('#login-username')).toBeInViewport();
    });
  }
  test('прямая ссылка с информационной страницы', async ({ page }) => {
    await page.setViewportSize({ width: 390, height: 700 });
    await page.goto('/dlya-buhgalterov');
    await page.getByRole('link', { name: 'Войти', exact: true }).click();
    await expect(page).toHaveURL(/#land-login$/);
    await expect(page.locator('#login-username')).toBeInViewport();
  });
  test('действующая сессия не выбрасывается при открытии ссылки входа', async ({ page }) => {
    await login(page);
    await page.goto('/#land-login');
    await expect(page.locator('#app-shell')).toBeVisible({ timeout: 15000 });
  });
});
