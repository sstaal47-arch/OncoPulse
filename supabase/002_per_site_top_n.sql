-- OncoPulse: per-disease-site abstract counts.
-- Run once in the Supabase SQL editor, AFTER the original schema in README.md.
-- Safe to re-run.
--
-- Adds subscribers.site_top_n, a JSON object of { "<site id>": <1-20>, ... }.
-- Each key is a disease site the subscriber follows; each value is how many
-- abstracts go in that site's email. disease_sites is kept in step with the
-- keys so anything that still reads it keeps working.

alter table subscribers
  add column if not exists site_top_n jsonb not null default '{}'::jsonb;

-- Backfill existing subscribers: every site they picked gets their old single top_n.
update subscribers s
   set site_top_n = coalesce(
         (select jsonb_object_agg(site, s.top_n) from unnest(s.disease_sites) as site),
         '{}'::jsonb)
 where s.site_top_n = '{}'::jsonb
   and coalesce(array_length(s.disease_sites, 1), 0) > 0;

-- Clamp every value to 1..20 and drop anything that isn't a number.
create or replace function _clean_site_top_n(p jsonb)
returns jsonb language sql immutable as $$
  select coalesce(jsonb_object_agg(key, least(20, greatest(1, (value #>> '{}')::int))), '{}'::jsonb)
    from jsonb_each(coalesce(p, '{}'::jsonb))
   where jsonb_typeof(value) = 'number';
$$;

create or replace function subscribe_v2(p_email text, p_site_top_n jsonb)
returns uuid language plpgsql security definer set search_path = public as $$
declare v_token uuid; v_counts jsonb := _clean_site_top_n(p_site_top_n);
begin
  if v_counts = '{}'::jsonb then raise exception 'Pick at least one disease site'; end if;
  insert into subscribers (email, disease_sites, site_top_n)
  values (lower(trim(p_email)), array(select jsonb_object_keys(v_counts)), v_counts)
  on conflict (email) do update
    set disease_sites = excluded.disease_sites, site_top_n = excluded.site_top_n,
        is_active = true, updated_at = now()
  returning unsubscribe_token into v_token;
  return v_token;
end; $$;

create or replace function get_subscriber_v2(p_token uuid)
returns table (email text, disease_sites text[], top_n int, site_top_n jsonb, is_active boolean)
language plpgsql security definer set search_path = public as $$
begin
  return query select s.email, s.disease_sites, s.top_n, s.site_top_n, s.is_active
    from subscribers s where s.unsubscribe_token = p_token;
end; $$;

create or replace function update_preferences_v2(p_token uuid, p_site_top_n jsonb)
returns boolean language plpgsql security definer set search_path = public as $$
declare v_counts jsonb := _clean_site_top_n(p_site_top_n);
begin
  if v_counts = '{}'::jsonb then raise exception 'Pick at least one disease site'; end if;
  update subscribers
     set disease_sites = array(select jsonb_object_keys(v_counts)), site_top_n = v_counts,
         is_active = true, updated_at = now()
   where unsubscribe_token = p_token;
  return found;
end; $$;

revoke all on function _clean_site_top_n(jsonb) from public, anon, authenticated;
grant execute on function subscribe_v2(text, jsonb)          to anon;
grant execute on function get_subscriber_v2(uuid)            to anon;
grant execute on function update_preferences_v2(uuid, jsonb) to anon;
