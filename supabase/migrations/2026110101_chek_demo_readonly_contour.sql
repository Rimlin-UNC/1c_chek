-- Отдельный синтетический read-only контур. Не заменяет рабочие таблицы приложения.
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
revoke all on public.chek_demo_companies, public.chek_demo_participants, public.chek_demo_receipts, public.chek_demo_items from public, anon, authenticated;
grant select on public.chek_demo_companies, public.chek_demo_participants, public.chek_demo_receipts, public.chek_demo_items to anon, authenticated;

create or replace function public.chek_demo_snapshot()
returns jsonb language sql stable security definer
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
notify pgrst, 'reload schema';
