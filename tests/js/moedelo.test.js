/* v1.70.107 (T-71, ADR-37): «Моё дело» — чистые функции отрисовки панели, карточки документа и пакета (Vitest). */
import { describe, expect, it } from 'vitest';
import {
  MD_DOCS_URL, mdBatchSummaryHtml, mdDocHtml, mdPanelHtml, mdResultSummary, mdWhen,
} from '../../app/static/js/moedelo.js';

describe('«Моё дело»: панель в карточке компании (v1.70.107)', () => {
  it('не подключено: поле ключа открыто, кнопка «Подключить и проверить», ссылка на описание API', () => {
    const html = mdPanelHtml({ status: 'none', key_set: false });
    expect(html).toContain('chip unknown');
    expect(html).toContain('не подключено');
    expect(html).toContain('<details open>');
    expect(html).toContain('Подключить и проверить');
    expect(html).not.toContain('id="mdc-del"');
    expect(html).toContain(`href="${MD_DOCS_URL}"`);
    expect(html).toContain('«Настройки → Интеграция с партнёрами»');
  });

  it('подключено: виден только хвост ключа, есть «Проверить ключ» и «Отключить»', () => {
    const html = mdPanelHtml({ status: 'active', key_set: true, key_tail: '950e', last_ok_at: '2026-11-05T18:40:00' });
    expect(html).toContain('chip verified');
    expect(html).toContain('ключ …950e');
    expect(html).toContain('проверен ');
    expect(html).toContain('<details >');
    expect(html).toContain('id="mdc-check"');
    expect(html).toContain('id="mdc-del"');
    expect(html).toContain('задан — вставьте новый, чтобы заменить');
    expect(html).not.toMatch(/value="[^"]+"/);           // ключ в поле не подставляется никогда
  });

  it('ошибка подключения экранируется', () => {
    const html = mdPanelHtml({ status: 'error', key_set: true, key_tail: '<b>', last_error: '<script>x</script>' });
    expect(html).toContain('chip failed');
    expect(html).toContain('&lt;script&gt;x&lt;/script&gt;');
    expect(html).not.toContain('<script>');
    expect(html).toContain('…&lt;b&gt;');
  });

  it('время с сервера без пометки зоны считается UTC', () => {
    expect(mdWhen('')).toBe('');
    expect(mdWhen('не дата')).toBe('');
    expect(mdWhen('2026-11-05T18:40:00')).toMatch(/^05\.11 \d\d:40$/);
  });
});

describe('«Моё дело»: карточка документа (v1.70.107)', () => {
  const ready = { connection: { status: 'active' }, link: null,
    plan: { kind: 'services', kind_title: 'услуги — уйдёт актом покупки', blockers: [], can_send: true } };

  it('услуги: кнопка доступна сразу, отметки «это услуги» нет', () => {
    const html = mdDocHtml(ready);
    expect(html).toContain('услуги — уйдёт актом покупки');
    expect(html).toContain('id="md-send">📒 В «Моё дело»');
    expect(html).not.toContain('md-as-services');
  });

  it('товары: кнопка ждёт отметки «Это услуги»', () => {
    const html = mdDocHtml({ ...ready, plan: { ...ready.plan, kind: 'goods', kind_title: 'товары — накладные появятся следующим этапом' } });
    expect(html).toContain('id="md-as-services"');
    expect(html).toContain('id="md-send" disabled');
    expect(html).toContain('следующим этапом');
  });

  it('причины «не отправится» — списком, экранированы', () => {
    const html = mdDocHtml({ ...ready, plan: { ...ready.plan, blockers: ['У документа нет номера', 'ИНН <нет>'] } });
    expect(html).toContain('не отправится');
    expect(html).toContain('<li>У документа нет номера</li>');
    expect(html).toContain('ИНН &lt;нет&gt;');
    expect(html).not.toContain('md-send');
  });

  it('не подключено или ключ не проверен — подсказка вместо кнопки', () => {
    expect(mdDocHtml({ connection: { status: 'none' }, plan: ready.plan })).toContain('ключ API вставляется в карточке компании');
    expect(mdDocHtml({ connection: { status: 'error', status_title: 'ошибка подключения' }, plan: ready.plan }))
      .toContain('ошибка подключения');
  });

  it('отправлен: отметка, расхождение суммы и «Забыть отправку»', () => {
    const html = mdDocHtml({ ...ready, link: { title: 'акт покупки № 15 (отправлен, 05.11.2026)', kontragent_new: true,
      message: 'В «Моём деле» сумма 101,50 ₽, в документе 100,00 ₽ — проверьте акт там' } });
    expect(html).toContain('акт покупки № 15');
    expect(html).toContain('поставщик добавлен по ИНН');
    expect(html).toContain('form-error');
    expect(html).toContain('id="md-forget"');
    expect(html).not.toContain('md-send');
  });
});

describe('«Моё дело»: пакет (v1.70.107)', () => {
  it('итог проверки: сколько уйдёт, сколько уже было и почему остальное не уйдёт', () => {
    const html = mdBatchSummaryHtml({ count: 50, more: 3, limit: 50, already: 2, skipped_total: 1,
      skipped: [{ title: 'УПД № 7 <x>', reason: 'товары — накладные появятся следующим этапом' }] });
    expect(html).toContain('Уйдёт актами покупки: 50');
    expect(html).toContain('ещё 3 — следующим заходом');
    expect(html).toContain('Уже в «Моём деле»: 2');
    expect(html).toContain('Не уйдут (1)');
    expect(html).toContain('УПД № 7 &lt;x&gt;');
    expect(mdBatchSummaryHtml(null)).toBe('');
  });

  it('итог отправки одной строкой', () => {
    expect(mdResultSummary({ sent: 2, found: 1, mismatch: 1, failed: [{}] }))
      .toBe('отправлено: 3 · уже были в «Моём деле»: 1 · сумма не совпала: 1 · не ушло: 1');
    expect(mdResultSummary({})).toBe('отправлено: 0');
  });
});
