/* v1.70.60: браузерные сценарии пользователя (Playwright).
   Покрывают типичный путь: вход, отказ входа, настройки с читаемыми
   оглавлениями блоков и сворачиванием. Сервер поднимает конфиг. */
import { readFileSync } from 'node:fs';
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
  test.setTimeout(90_000);   // два входа и два экрана — дольше обычного сценария
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
  await page.setInputFiles('#prim-file', { name: 'upd_print.pdf', mimeType: 'application/pdf', buffer: pdf });
  const modal = page.locator('.modal-slot');
  await expect(modal).toContainText('Сверьте реквизиты: upd_print.pdf');
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
