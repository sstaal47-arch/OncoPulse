-- OncoPulse: dashboard settings that follow a signed-in user across devices.
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- One row per signed-in user holding their dashboard filters, saved (★) and
-- deleted (🗑) articles. Row-level security means each user can only ever
-- read or change their own row; nobody can list other users' rows.

create table if not exists user_settings (
  user_id    uuid primary key references auth.users (id) on delete cascade,
  settings   jsonb,                                  -- dashboard filters (null = defaults)
  favorites  jsonb not null default '{}'::jsonb,     -- ★ saved articles
  deleted    jsonb not null default '{}'::jsonb,     -- 🗑 deleted articles
  updated_at timestamptz not null default now()
);

alter table user_settings enable row level security;

drop policy if exists "own settings: read"   on user_settings;
drop policy if exists "own settings: insert" on user_settings;
drop policy if exists "own settings: update" on user_settings;
drop policy if exists "own settings: delete" on user_settings;

create policy "own settings: read"   on user_settings for select to authenticated using (user_id = auth.uid());
create policy "own settings: insert" on user_settings for insert to authenticated with check (user_id = auth.uid());
create policy "own settings: update" on user_settings for update to authenticated using (user_id = auth.uid()) with check (user_id = auth.uid());
create policy "own settings: delete" on user_settings for delete to authenticated using (user_id = auth.uid());

revoke all on user_settings from anon;
grant select, insert, update, delete on user_settings to authenticated;
