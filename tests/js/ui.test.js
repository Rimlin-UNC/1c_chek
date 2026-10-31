/* v1.70.60: юнит-тесты модуля интерфейса (Vitest, среда jsdom).
   Проверяют чистые функции: экранирование, форматы чисел и дат,
   подписи статусов и ролей, разметку значка. */
import { describe, expect, it } from 'vitest';
import {
  chip, esc, fmtDate, fmtInt, fmtSum, roleLabel, statusLabel,
} from '../../app/static/js/ui.js';

const nbsp = (s) => s.replace(/\u00A0/g, ' ');

describe('esc', () => {
  it('экранирует все опасные символы', () => {
    expect(esc('<b class="x">&\'</b>'))
      .toBe('&lt;b class=&quot;x&quot;&gt;&amp;&#39;&lt;/b&gt;');
  });
  it('null и undefined становятся пустой строкой', () => {
    expect(esc(null)).toBe('');
    expect(esc(undefined)).toBe('');
  });
  it('числа приводятся к строке', () => {
    expect(esc(42)).toBe('42');
  });
});

describe('форматы', () => {
  it('fmtSum: две копейки и знак рубля', () => {
    expect(nbsp(fmtSum(1234.5))).toBe('1 234,50 ₽');
    expect(nbsp(fmtSum(0))).toBe('0,00 ₽');
  });
  it('fmtInt: группы разрядов по-русски', () => {
    expect(nbsp(fmtInt(1234567))).toBe('1 234 567');
  });
  it('fmtDate: пусто — прочерк, дата — дд.мм.гг чч:мм', () => {
    expect(fmtDate('')).toBe('—');
    expect(fmtDate(null)).toBe('—');
    expect(fmtDate('2026-10-27T10:05:00Z'))
      .toMatch(/\d{2}\.\d{2}\.\d{2}, \d{2}:\d{2}/);
  });
});

describe('подписи', () => {
  it('статусы чеков и ЕГРЮЛ', () => {
    expect(statusLabel('verified')).toBe('Проверен');
    expect(statusLabel('failed')).toBe('Отклонён ФНС');
    expect(statusLabel('not_found')).toBe('Не найден в ФНС');
  });
  it('неизвестный статус проходит как есть, пусто — прочерк', () => {
    expect(statusLabel('xyz')).toBe('xyz');
    expect(statusLabel('')).toBe('—');
  });
  it('роли', () => {
    expect(roleLabel('admin')).toBe('Администратор');
    expect(roleLabel('manager')).toBe('Руководитель');
    expect(roleLabel('')).toBe('—');
  });
});

describe('chip', () => {
  it('значок несёт класс статуса и подпись', () => {
    const html = chip('verified');
    expect(html).toContain('class="chip verified');
    expect(html).toContain('Проверен');
  });
  it('статус-инъекция экранируется', () => {
    const html = chip('<img src=x onerror=alert(1)>');
    expect(html).not.toContain('<img');
    expect(html).toContain('&lt;img');
  });
});
