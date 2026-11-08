// T-80 (#65, v1.71.0): карточка контрагента — контракт интерфейса.
// Только синтетика (ADR-14).
import { describe, expect, it } from 'vitest';
import {
  COUNTERPARTY_BLOCKS,
  counterpartyCardHtml,
  counterpartyCreateBody,
  counterpartyAccountBody,
  counterpartyPatchBody,
} from '../../app/static/js/counterparties.js';

const card = {
  id: 'cp-1',
  name_full: 'ООО «Ромашка»',
  inn: '7704123450',
  kpp: '770401001',
  ogrn: '1027700132195',
  address: 'г. Москва, ул. Ленина, 1',
  signatory_post: 'Директор',
  signatory_name: 'Иван Иванов',
  signatory_basis: 'Устава',
  genitive: 'ООО «Ромашка»',
  accounts: [],
  aliases: [],
  risk: { level: 'yellow', reasons: ['Массовый адрес'] },
  blocks: {
    'Реквизиты стороны': 'ИНН/КПП: 7704123450/770401001',
    'Преамбула договора': 'ООО «Ромашка», именуемое в дальнейшем…',
    'Для платёжного поручения': 'БИК: 044525225',
  },
};

describe('карточка контрагента T-80', () => {
  it('три блока в фиксированном порядке', () => {
    expect(COUNTERPARTY_BLOCKS).toEqual([
      'Реквизиты стороны', 'Преамбула договора', 'Для платёжного поручения',
    ]);
  });

  it('показывает светофор и три кнопки копирования', () => {
    const html = counterpartyCardHtml(card);
    expect(html).toContain('cp-risk-yellow');
    expect(html).toContain('Массовый адрес');
    for (const name of COUNTERPARTY_BLOCKS) {
      expect(html).toContain(`data-copy="${name}"`);
      expect(html).toContain(name);
    }
    expect(html).toContain('проверьте перед вставкой');
  });

  it('тела запросов содержат только поля формы', () => {
    expect(counterpartyCreateBody({
      inn: '7704123450', name: 'ООО «Ромашка»', kpp: '770401001', companyId: 'c1',
    })).toEqual({
      inn: '7704123450', name: 'ООО «Ромашка»', kpp: '770401001', company_id: 'c1',
    });
    expect(counterpartyAccountBody({
      account: '40702810100000000001', bik: '044525225', companyId: 'c1',
    })).toEqual({
      account: '40702810100000000001', bik: '044525225', company_id: 'c1',
    });
    expect(counterpartyPatchBody({
      signatoryPost: 'Директор', signatoryName: 'Иван Иванов',
      signatoryBasis: 'Устава', genitive: 'ООО «Ромашка»', companyId: 'c1',
    })).toEqual({
      signatory_post: 'Директор', signatory_name: 'Иван Иванов',
      signatory_basis: 'Устава', genitive: 'ООО «Ромашка»', company_id: 'c1',
    });
  });
});
