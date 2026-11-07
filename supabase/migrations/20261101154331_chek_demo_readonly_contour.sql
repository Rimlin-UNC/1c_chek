create schema if not exists chek_demo;
revoke all on schema chek_demo from public, anon, authenticated;
grant usage on schema chek_demo to service_role;

create table if not exists chek_demo.companies (
  id uuid primary key,
  slug text not null unique,
  name text not null,
  is_demo boolean not null default true check (is_demo),
  created_at timestamptz not null default now()
);
create table if not exists chek_demo.participants (
  id uuid primary key,
  company_id uuid not null references chek_demo.companies(id),
  code text not null,
  label text not null,
  is_demo boolean not null default true check (is_demo),
  created_at timestamptz not null default now(),
  unique (company_id, code)
);
create table if not exists chek_demo.receipts (
  id uuid primary key,
  company_id uuid not null references chek_demo.companies(id),
  participant_id uuid references chek_demo.participants(id),
  receipt_no text not null,
  receipt_date date not null,
  merchant_name text not null,
  total numeric(12,2) not null check (total >= 0),
  status text not null check (status in ('new','verified','exported')),
  source text not null default 'supabase_demo',
  is_demo boolean not null default true check (is_demo),
  created_at timestamptz not null default now(),
  unique (company_id, receipt_no)
);
create table if not exists chek_demo.receipt_items (
  id uuid primary key,
  receipt_id uuid not null references chek_demo.receipts(id) on delete cascade,
  name text not null,
  quantity numeric(12,3) not null check (quantity > 0),
  price numeric(12,2) not null check (price >= 0),
  total numeric(12,2) not null check (total >= 0),
  position integer not null check (position > 0),
  unique (receipt_id, position)
);
create index if not exists idx_chek_demo_receipts_company_date on chek_demo.receipts(company_id, receipt_date desc);
create index if not exists idx_chek_demo_receipts_participant on chek_demo.receipts(participant_id);
create index if not exists idx_chek_demo_items_receipt on chek_demo.receipt_items(receipt_id);

alter table chek_demo.companies enable row level security;
alter table chek_demo.participants enable row level security;
alter table chek_demo.receipts enable row level security;
alter table chek_demo.receipt_items enable row level security;
alter table chek_demo.companies force row level security;
alter table chek_demo.participants force row level security;
alter table chek_demo.receipts force row level security;
alter table chek_demo.receipt_items force row level security;
revoke all on all tables in schema chek_demo from public, anon, authenticated;
grant select, insert, update, delete on all tables in schema chek_demo to service_role;

create or replace view public.chek_demo_companies with (security_barrier=true) as
select id, slug, name from chek_demo.companies where is_demo;
create or replace view public.chek_demo_participants with (security_barrier=true) as
select p.id, p.company_id, p.code, p.label
from chek_demo.participants p join chek_demo.companies c on c.id=p.company_id
where p.is_demo and c.is_demo;
create or replace view public.chek_demo_receipts with (security_barrier=true) as
select r.id, r.company_id, c.name as company_name, r.participant_id,
       coalesce(p.label, 'Без участника') as participant_label,
       r.receipt_no, r.receipt_date, r.merchant_name, r.total, r.status, r.source
from chek_demo.receipts r
join chek_demo.companies c on c.id=r.company_id
left join chek_demo.participants p on p.id=r.participant_id
where r.is_demo and c.is_demo;
create or replace view public.chek_demo_items with (security_barrier=true) as
select i.id, i.receipt_id, i.name, i.quantity, i.price, i.total, i.position
from chek_demo.receipt_items i join chek_demo.receipts r on r.id=i.receipt_id
where r.is_demo;
revoke all on public.chek_demo_companies, public.chek_demo_participants, public.chek_demo_receipts, public.chek_demo_items from public;
grant select on public.chek_demo_companies, public.chek_demo_participants, public.chek_demo_receipts, public.chek_demo_items to anon, authenticated;

create or replace function public.chek_demo_snapshot()
returns jsonb
language sql
stable
security definer
set search_path = chek_demo, public
as $$
  select jsonb_build_object(
    'generated_at', now(),
    'mode', 'read_only_demo',
    'companies', (select coalesce(jsonb_agg(jsonb_build_object('id', id, 'slug', slug, 'name', name) order by name), '[]'::jsonb) from chek_demo.companies where is_demo),
    'participants', (select coalesce(jsonb_agg(jsonb_build_object('id', p.id, 'company_id', p.company_id, 'code', p.code, 'label', p.label) order by p.label), '[]'::jsonb) from chek_demo.participants p join chek_demo.companies c on c.id=p.company_id where p.is_demo and c.is_demo),
    'receipts', (select coalesce(jsonb_agg(jsonb_build_object('id', r.id, 'company_id', r.company_id, 'company_name', c.name, 'participant_id', r.participant_id, 'participant_label', coalesce(p.label, 'Без участника'), 'receipt_no', r.receipt_no, 'receipt_date', r.receipt_date, 'merchant_name', r.merchant_name, 'total', r.total, 'status', r.status, 'source', r.source) order by r.receipt_date desc, r.receipt_no), '[]'::jsonb)
      from chek_demo.receipts r join chek_demo.companies c on c.id=r.company_id left join chek_demo.participants p on p.id=r.participant_id where r.is_demo and c.is_demo),
    'items', (select coalesce(jsonb_agg(jsonb_build_object('id', i.id, 'receipt_id', i.receipt_id, 'name', i.name, 'quantity', i.quantity, 'price', i.price, 'total', i.total, 'position', i.position) order by i.receipt_id, i.position), '[]'::jsonb) from chek_demo.receipt_items i join chek_demo.receipts r on r.id=i.receipt_id where r.is_demo)
  );
$$;
revoke all on function public.chek_demo_snapshot() from public;
grant execute on function public.chek_demo_snapshot() to anon, authenticated;

insert into chek_demo.companies(id, slug, name) values
('11111111-1111-4111-8111-111111111101','demo-alfa','Демо-компания Альфа'),
('11111111-1111-4111-8111-111111111102','demo-beta','Демо-компания Бета')
on conflict (id) do nothing;
insert into chek_demo.participants(id, company_id, code, label) values
('22222222-2222-4222-8222-222222222201','11111111-1111-4111-8111-111111111101','demo-a','Участник A (демо)'),
('22222222-2222-4222-8222-222222222202','11111111-1111-4111-8111-111111111101','demo-b','Участник B (демо)'),
('22222222-2222-4222-8222-222222222203','11111111-1111-4111-8111-111111111102','demo-c','Участник C (демо)')
on conflict (id) do nothing;
insert into chek_demo.receipts(id, company_id, participant_id, receipt_no, receipt_date, merchant_name, total, status) values
('33333333-3333-4333-8333-333333333301','11111111-1111-4111-8111-111111111101','22222222-2222-4222-8222-222222222201','DEMO-0001','2026-10-20','Демо-маркет №1',1540.00,'exported'),
('33333333-3333-4333-8333-333333333302','11111111-1111-4111-8111-111111111101','22222222-2222-4222-8222-222222222201','DEMO-0002','2026-10-21','Демо-канцтовары',890.50,'verified'),
('33333333-3333-4333-8333-333333333303','11111111-1111-4111-8111-111111111101','22222222-2222-4222-8222-222222222202','DEMO-0003','2026-10-22','Демо-кафе',1260.00,'verified'),
('33333333-3333-4333-8333-333333333304','11111111-1111-4111-8111-111111111101',null,'DEMO-0004','2026-10-23','Демо-аптека',430.00,'new'),
('33333333-3333-4333-8333-333333333305','11111111-1111-4111-8111-111111111101','22222222-2222-4222-8222-222222222201','DEMO-0005','2026-10-24','Демо-маркет №1',2210.00,'verified'),
('33333333-3333-4333-8333-333333333306','11111111-1111-4111-8111-111111111101','22222222-2222-4222-8222-222222222202','DEMO-0006','2026-10-25','Демо-хозтовары',780.00,'exported'),
('33333333-3333-4333-8333-333333333307','11111111-1111-4111-8111-111111111101',null,'DEMO-0007','2026-10-26','Демо-кафе',950.00,'new'),
('33333333-3333-4333-8333-333333333308','11111111-1111-4111-8111-111111111101','22222222-2222-4222-8222-222222222201','DEMO-0008','2026-10-27','Демо-канцтовары',1320.00,'verified'),
('33333333-3333-4333-8333-333333333309','11111111-1111-4111-8111-111111111102','22222222-2222-4222-8222-222222222203','DEMO-0009','2026-10-28','Демо-маркет №2',3410.00,'verified'),
('33333333-3333-4333-8333-333333333310','11111111-1111-4111-8111-111111111102','22222222-2222-4222-8222-222222222203','DEMO-0010','2026-10-29','Демо-кафе',640.00,'new'),
('33333333-3333-4333-8333-333333333311','11111111-1111-4111-8111-111111111102',null,'DEMO-0011','2026-10-30','Демо-аптека',520.00,'verified'),
('33333333-3333-4333-8333-333333333312','11111111-1111-4111-8111-111111111102','22222222-2222-4222-8222-222222222203','DEMO-0012','2026-10-31','Демо-хозтовары',1180.00,'exported')
on conflict (id) do nothing;
insert into chek_demo.receipt_items(id, receipt_id, name, quantity, price, total, position)
select ('44444444-4444-4444-8444-' || lpad((((substring(r.id::text from 30 for 3))::int - 300) * 2 + 1)::text, 12, '0'))::uuid,
       r.id, 'Демо-товар 1', 1, round(r.total * 0.6, 2), round(r.total * 0.6, 2), 1
from chek_demo.receipts r
on conflict (id) do nothing;
insert into chek_demo.receipt_items(id, receipt_id, name, quantity, price, total, position)
select ('44444444-4444-4444-8444-' || lpad((((substring(r.id::text from 30 for 3))::int - 300) * 2 + 2)::text, 12, '0'))::uuid,
       r.id, 'Демо-товар 2', 1, r.total - round(r.total * 0.6, 2), r.total - round(r.total * 0.6, 2), 2
from chek_demo.receipts r
on conflict (id) do nothing;

notify pgrst, 'reload schema';
