-- Run this once in your Supabase project: SQL Editor -> New query -> paste -> Run.
-- It creates a simple append-only trade log the dashboard writes to.

create table if not exists public.trade_log (
    id          bigint generated always as identity primary key,
    created_at  timestamptz not null default now(),
    event_type  text not null,          -- 'open' | 'close' | 'preview' | 'error'
    payload     jsonb not null default '{}'::jsonb
);

create index if not exists trade_log_created_at_idx
    on public.trade_log (created_at desc);

-- This local single-user app uses the anon key. Enable RLS and allow the anon
-- role to read/insert (fine for a local, single-user dashboard). Tighten later
-- if you ever expose this beyond your machine.
alter table public.trade_log enable row level security;

drop policy if exists "anon can read trade_log" on public.trade_log;
create policy "anon can read trade_log"
    on public.trade_log for select
    to anon using (true);

drop policy if exists "anon can insert trade_log" on public.trade_log;
create policy "anon can insert trade_log"
    on public.trade_log for insert
    to anon with check (true);
