"""Печатная сводка T-90: минимум данных, без скриптов и внешних ресурсов."""
from __future__ import annotations

from html import escape

LABELS = {'bad': 'Нужны действия', 'warn': 'Есть вопросы', 'ok': 'Всё в порядке'}


def money(value: float) -> str:
    """Те же округлённые суммы чеков, что в сводке; это не выручка и не тариф."""
    return f'{value:,.2f}'.replace(',', '\u00a0').replace('.', ',')


def render(data: dict) -> str:
    def e(value: object) -> str:
        return escape(str(value), quote=True)

    lines = []
    for row in data['companies']:
        r, p, d, b, a = row['receipts'], row['primary'], row['docreq'], row['bank'], row['approvals']
        lines.append(
            '<tr>'
            f'<td>{e(row["name"])}<br><small>ИНН {e(row["inn"] or "—")}</small></td>'
            f'<td>{e(LABELS.get(row["level"], "—"))}</td>'
            f'<td>{e(r["count"])}</td><td>{e(money(r["sum"]))}</td>'
            f'<td>{e(r["fns_bad"])}</td><td>{e(p["errors"])}</td>'
            f'<td>{e(d["open"])}</td><td>{e(d["overdue"])}</td>'
            f'<td>{e(b["open"])}</td><td>{e(a["todo"]) if a["enabled"] else "—"}</td></tr>')
    rows = ''.join(lines) or '<tr><td colspan="10">Нет доступных активных компаний.</td></tr>'
    totals = data['totals']
    return f'''<!doctype html>
<html lang="ru"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<meta http-equiv="Content-Security-Policy" content="default-src 'none'; style-src 'unsafe-inline'; base-uri 'none'; form-action 'none'">
<title>Сводка клиентов — {e(data['period_title'])}</title>
<style>
body{{font:14px Arial,sans-serif;color:#222;margin:24px}} h1{{font-size:22px}} p{{line-height:1.45}}
table{{border-collapse:collapse;width:100%;font-size:11px}} th,td{{border:1px solid #aaa;padding:5px;vertical-align:top}}
th{{background:#eee;text-align:left}} td:nth-child(n+3){{text-align:right}} small{{color:#555}}
@media print{{@page{{size:landscape;margin:10mm}} body{{margin:0}} tr{{break-inside:avoid}} thead{{display:table-header-group}}}}
</style></head><body>
<h1>Сводка по клиентам</h1>
<p><b>Отчётный месяц:</b> {e(data['period_title'])}. <b>Сформировано:</b> {e(data['generated_at'])} (UTC).<br>
<b>Доступных активных компаний:</b> {e(totals['companies'])}.</p>
<p>Чеки, ошибки ФНС, первичные документы и платежи относятся к выбранному месяцу;
открытые запросы за этот период, статус и согласования — на момент формирования, не исторический снимок.
«Ждут меня» зависит от роли составителя. Общий статус может учитывать срок договора; его реквизиты и тариф не включены.</p>
<table><thead><tr><th>Компания</th><th>Общий статус</th><th>Чеков</th><th>Сумма чеков</th>
<th>Ошибки ФНС</th><th>Ошибки первички</th><th>Открытые запросы</th><th>Просроченные запросы</th>
<th>Платежи без документов</th><th>Согласования: ждут меня</th></tr></thead><tbody>{rows}</tbody>
<tfoot><tr><th>Итого по доступным компаниям</th><td>—</td><td>{e(totals['receipts'])}</td>
<td>{e(money(totals['sum']))}</td><td>{e(totals['fns_bad'])}</td><td>{e(totals['upd_errors'])}</td>
<td>{e(totals['open'])}</td><td>{e(totals['overdue'])}</td><td>{e(totals['bank_open'])}</td>
<td>{e(totals['approvals_todo'])}</td></tr></tfoot></table>
<p>Сумма чеков не является выручкой. Документ содержит сведения о клиентах: перед передачей проверьте полномочия получателя.
Это управленческая сводка, не бухгалтерская отчётность и не подтверждение отправки клиенту.</p>
<p>Для печати или сохранения в PDF используйте команду печати браузера.</p>
</body></html>'''
