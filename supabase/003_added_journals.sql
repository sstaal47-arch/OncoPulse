-- OncoPulse: journals added from the dashboard's "Add a journal" box.
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- The dashboard registers a journal here when a viewer adds it; the weekly
-- pipeline (service-role key) reads the list and pulls that journal's oncology
-- articles from PubMed. The public key can only add entries, never read them.

create table if not exists added_journals (
  ta         text primary key,          -- NLM title abbreviation, e.g. "Radiother Oncol"
  title      text not null,             -- full NLM title
  added_at   timestamptz not null default now()
);
alter table added_journals enable row level security;
revoke all on added_journals from anon, authenticated;

create or replace function add_journal(p_ta text, p_title text)
returns boolean language plpgsql security definer set search_path = public as $$
declare v_ta text := trim(p_ta); v_title text := trim(p_title);
begin
  if v_ta is null or length(v_ta) < 2 or length(v_ta) > 120
     or v_title is null or length(v_title) > 400 then
    raise exception 'Invalid journal';
  end if;
  -- Hard cap so the public key can't flood the list.
  if (select count(*) from added_journals) >= 500 then
    raise exception 'Journal list is full';
  end if;
  insert into added_journals (ta, title) values (v_ta, coalesce(nullif(v_title, ''), v_ta))
  on conflict (ta) do nothing;
  return true;
end; $$;

grant execute on function add_journal(text, text) to anon;
