delete from chek_demo.receipt_items;
insert into chek_demo.receipt_items(id, receipt_id, name, quantity, price, total, position)
select md5(r.id::text || ':1')::uuid, r.id, 'Демо-товар 1', 1, round(r.total * 0.6, 2), round(r.total * 0.6, 2), 1
from chek_demo.receipts r;
insert into chek_demo.receipt_items(id, receipt_id, name, quantity, price, total, position)
select md5(r.id::text || ':2')::uuid, r.id, 'Демо-товар 2', 1, r.total - round(r.total * 0.6, 2), r.total - round(r.total * 0.6, 2), 2
from chek_demo.receipts r;
notify pgrst, 'reload schema';
