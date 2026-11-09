/* T-88: договор клиента — безопасная разметка и напоминание в карточке. */
import { describe, expect, it } from 'vitest';
import { clientsCardHtml, clientsContractFormHtml } from '../../app/static/js/clients.js';

const row = { id: 'abc', name: 'Клиент', level: 'ok', contract: {
  number: '<тест>', date: '2026-01-01', service_since: '2026-01-02', service_until: '2026-12-31',
  fee: '120.50', currency: 'RUB', frequency: 'month', manager: '', reminder: { days: 7, level: 'urgent' },
} };

describe('договор и тариф клиента', () => {
  it('карточка показывает договор и напоминание без HTML-инъекции', () => {
    const html = clientsCardHtml(row);
    expect(html).toContain('&lt;тест&gt;');
    expect(html).not.toContain('<тест>');
    expect(html).toContain('7 дней');
    expect(html).toContain('данным карточки');
    expect(html).not.toContain('Договор заканчивается');
    expect(html).toContain('data-contract="abc"');
  });
  it('форма помечает обязательные поля и не заставляет вносить окончание', () => {
    const html = clientsContractFormHtml(null);
    expect(html).toContain('id="contract-number"');
    expect(html).toContain('id="contract-fee"');
    expect(html).toContain('RUB');
    expect(html).toContain('не отправляется');
    expect(html).toContain('пролонгацию');
    const root = document.createElement('div');
    root.innerHTML = html;
    expect(root.querySelector('#contract-number').required).toBe(true);
    expect(root.querySelector('#contract-fee').required).toBe(true);
    expect(root.querySelector('#contract-until').required).toBe(false);
  });
});
