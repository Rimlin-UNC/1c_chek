-- Supabase default privileges may grant write on new public views to anon/authenticated.
revoke all on public.chek_demo_companies, public.chek_demo_participants, public.chek_demo_receipts, public.chek_demo_items from public, anon, authenticated;
grant select on public.chek_demo_companies, public.chek_demo_participants, public.chek_demo_receipts, public.chek_demo_items to anon, authenticated;
alter default privileges in schema public revoke insert, update, delete, truncate on tables from anon, authenticated;
notify pgrst, 'reload schema';
