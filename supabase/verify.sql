-- Проверка после применения миграций. Выполнять в SQL-редакторе Supabase.
select (select count(*) from chek_demo.companies) as companies,
       (select count(*) from chek_demo.participants) as participants,
       (select count(*) from chek_demo.receipts) as receipts,
       (select count(*) from chek_demo.receipt_items) as items,
       (select sum(total) from chek_demo.receipts) as total_sum,
       (select count(*) from chek_demo.receipts r
         where abs(r.total - coalesce((select sum(i.total) from chek_demo.receipt_items i
                                        where i.receipt_id=r.id),0)) > 0.001) as item_total_mismatches,
       has_schema_privilege('anon','chek_demo','USAGE') as anon_can_use_schema,
       has_table_privilege('anon','chek_demo.receipts','SELECT') as anon_can_read_base_table,
       has_table_privilege('anon','chek_demo.receipts','INSERT') as anon_can_insert_base_table,
       has_table_privilege('anon','public.chek_demo_receipts','SELECT') as anon_can_read_view,
       has_table_privilege('anon','public.chek_demo_receipts','INSERT') as anon_can_insert_view,
       has_table_privilege('authenticated','public.chek_demo_receipts','INSERT') as authenticated_can_insert_view,
       has_function_privilege('anon','public.chek_demo_snapshot()','EXECUTE') as anon_can_call_snapshot;
-- Ожидание: 2 / 3 / 12 / 24 / 15130.50 / 0 / false / false / false / true / false / false / true.
