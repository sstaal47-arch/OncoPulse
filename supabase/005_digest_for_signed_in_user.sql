-- OncoPulse: let a signed-in user reach their email-digest subscription from
-- any device, without needing the private "Manage preferences" link.
-- Run once in the Supabase SQL editor. Safe to re-run.
--
-- The email comes from the user's verified sign-in (auth.jwt()), never from
-- the caller, so a user can only ever get their own subscription.

create or replace function my_digest_token()
returns uuid language plpgsql security definer set search_path = public as $$
declare v_email text := lower(auth.jwt() ->> 'email'); v_token uuid;
begin
  if v_email is null or v_email = '' then return null; end if;
  select unsubscribe_token into v_token from subscribers where email = v_email;
  return v_token;   -- null if this account has never subscribed
end; $$;

revoke all on function my_digest_token() from public, anon;
grant execute on function my_digest_token() to authenticated;
