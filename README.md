# OncoPulse — Weekly Oncology Literature Dashboard

Automated weekly digest of oncology literature: Phase 2/3 trials, FDA approvals, and key reviews — summarized by Claude and served as a searchable web dashboard, with optional personalized email digests by disease site.

---

## How It Works

```
PubMed API ──┐
FDA RSS     ──┼──► fetch_and_summarize.py ──► data.json ──► index.html (dashboard)
Journal RSS ──┘          (Claude API)
```

Every Monday at 6:00 AM UTC, GitHub Actions:
1. Pulls recent articles from PubMed, FDA, 10 major journal RSS feeds, and any journals viewers have added
2. Filters for clinical relevance (Phase 2+, FDA actions, reviews)
3. Calls Claude to generate structured summaries (design, endpoints, results, clinical takeaway)
4. Commits updated `data.json` to the repo
5. GitHub Pages serves the dashboard automatically
6. 30 minutes later, a second workflow emails each subscriber their per-disease-site digests

---

## Features at a Glance

| Feature | Where | Stored in |
|---------|-------|-----------|
| Weekly literature dashboard | `dashboard/index.html` | `dashboard/data.json` (regenerated weekly) |
| Filters, journals, ★ saved and 🗑 deleted articles | ⚙ Settings, ★ Saved, 🗑 Deleted | This browser; **plus your account when signed in** |
| Sign-in with email (code or link) | 👤 Sign in | Supabase Auth + `user_settings` |
| Add any PubMed journal | ⚙ Settings → Add a journal | Your settings + shared `added_journals` list |
| Weekly email digest, one email per disease site | ✉ Email Digest (`preferences.html`) | Supabase `subscribers` |

---

## Sign-in and Sync Across Devices

The **👤 Sign in** button in the dashboard header signs you in with your email:

1. Enter your email and press **Email me a sign-in code**.
2. Type the 6-digit code from the email into OncoPulse — on any device — or click the link in the email on the device you're signing in on.

Each browser signs in once and stays signed in (until you sign out, clear that browser's data, or use a private window). Every browser counts separately, e.g. Safari and Chrome on the same phone.

**What follows you when signed in**

- **Dashboard:** your filters, added journals, ★ saved and 🗑 deleted articles are kept in Supabase (`user_settings`, one row per user) and are the same on every browser you sign in on. Changes are saved within about a second. The first time you sign in on a browser, any stars and deletions already in it are added to your account, so nothing is lost. If you change something on another device while a tab is open, that tab shows a bar offering to update.
- **Email digest:** the ✉ Email Digest page recognises your account and loads your digest choices (sites and abstract counts) on any device, with your email filled in and locked to your account.

Each user can only ever read or change their own settings (row-level security), and the digest lookup uses the email from your verified sign-in, never one typed by the caller.

Not signed in, everything still works, but is remembered only in that one browser. The digest page also remembers you in the browser you subscribed from, and the **Manage preferences** link at the bottom of every digest email opens your choices on any device.

## Adding Journals

In the dashboard's **⚙ Settings → Add a journal** box you can add **any journal indexed in PubMed**:

- Suggestions come live from PubMed's journal list (the NLM Catalog) as you type, e.g. "radiation onc".
- You can also type a full title (`Gynecologic oncology`) or its NLM abbreviation (`Int J Radiat Oncol Biol Phys`).
- A name PubMed doesn't recognise can still be added, but only filters articles OncoPulse already has.

An added journal is also registered in a shared Supabase list (`added_journals`, via the `add_journal` function). From the next weekly update, the pipeline pulls that journal's oncology articles (any study type) from PubMed. The public website key can add to this list but cannot read it.

## Email Digest Subscriptions

Anyone can subscribe to personalized weekly emails via the **✉ Email Digest** button in the dashboard header (`dashboard/preferences.html`):

- **Each disease site is its own email.** Every site starts at **0** (no email). Enter how many top-ranked abstracts (1–20) you want for a site and it is ticked; set it back to 0 to stop that site.
- **Sent about 30 minutes after the weekly update** finishes (Mondays).
- **Your choices are remembered** — in the browser you subscribed from, on any device you're signed in on, and via the **Manage preferences** link in every email.
- **Unsubscribe** anytime from the same page or the link in each email.

```
preferences.html ──► Supabase (subscribers table)
                                   │
data.json ──► send_digests.py ◄───┘   (reads active subscribers)
                     │
                     ▼
              Resend API ──► subscriber's inbox (one email per site)
```

Each site's email holds that site's top-N articles by `data.json`'s `importance_score`, the same ranking the dashboard uses. A site with no articles that week sends no email. Each article link prefers a free PMC full-text copy (via NCBI's id-converter) when one exists, falling back to the PubMed or publisher page.

Subscriber data lives in a Supabase table locked down with row-level security. The public preferences page can only reach it through server-side functions that check the caller's own private token or verified sign-in; there's no way for the page's public key to read or export the subscriber list. The weekly GitHub Actions jobs are the only things with full read access, via a separate, non-public service-role key.

Sending is a separate workflow, `.github/workflows/send_digests.yml`. It starts when the weekly update finishes successfully, waits 30 minutes, checks out the freshly committed `data.json`, and runs `pipeline/send_digests.py`. If any email fails to send, the step fails (red in Actions) and the run summary lists the Resend error.

**Testing:** **Actions → OncoPulse Digest Emails → Run workflow**, with your own (already subscribed) address in *test_to*. That sends only your emails, for the sites and counts you saved, with no 30-minute wait. Locally, `python pipeline/send_digests.py --dry-run` writes HTML previews to `digest_preview/` without sending anything.

---

## Supabase Setup

All of these are done once, on the **Supabase** website, inside your project. The free plan is enough.

### Database (SQL Editor)

Run each file once, in order. Each is safe to re-run.

| File | Adds |
|------|------|
| Schema in [Setting up the digest feature](#setting-up-the-digest-feature) below | `subscribers` table and the original digest functions |
| [`supabase/002_per_site_top_n.sql`](supabase/002_per_site_top_n.sql) | Per-site abstract counts (`site_top_n`) and the `*_v2` digest functions |
| [`supabase/003_added_journals.sql`](supabase/003_added_journals.sql) | Shared `added_journals` list and `add_journal()` |
| [`supabase/004_user_settings.sql`](supabase/004_user_settings.sql) | `user_settings` table for sign-in sync |
| [`supabase/005_digest_for_signed_in_user.sql`](supabase/005_digest_for_signed_in_user.sql) | `my_digest_token()`, so the digest page follows your sign-in |

If a page reports "Could not find the function …" right after running a file, run `notify pgrst, 'reload schema';` in the SQL Editor and reload the page.

### Sign-in emails (Authentication)

Do these in this order — Supabase locks the email templates until custom SMTP is on.

1. **Authentication → Emails → SMTP Settings**: turn on **custom SMTP**. (Supabase's built-in sender only delivers to your project's team members, a few emails an hour.) With Resend:

   | Field | Value |
   |-------|-------|
   | Host | `smtp.resend.com` |
   | Port | `465` |
   | Username | `resend` |
   | Password | a Resend API key (starts `re_`) |
   | Sender email | any address **on your verified Resend domain**, e.g. `signin@digest.yourdomain.com` |
   | Sender name | `OncoPulse` |

   The sender must end in exactly the domain shown as **Verified** under resend.com → Domains; otherwise sending fails with "domain is not verified".
2. **Authentication → Emails → Templates**: in both **Magic Link** and **Confirm signup**, set the subject to `Your OncoPulse sign-in code` and paste [`supabase/magic_link_email_body.html`](supabase/magic_link_email_body.html) as the body. It contains `{{ .Token }}` (the code) and `{{ .ConfirmationURL }}` (the link).
3. **Authentication → URL Configuration**: set **Site URL** to your dashboard address (e.g. `https://YOURUSERNAME.github.io/oncopulse/dashboard/`) and add the same address followed by `**` under **Redirect URLs**.

If a sign-in email doesn't arrive, **Logs → Auth** in Supabase shows the reason.

---

### Setting up the digest feature

1. **Create a free [Supabase](https://supabase.com) project.** In its SQL editor, run the schema below once:

   <details>
   <summary>supabase/schema.sql</summary>

   ```sql
   create extension if not exists pgcrypto;

   create table if not exists subscribers (
     id                uuid primary key default gen_random_uuid(),
     email             text not null unique,
     disease_sites     text[] not null default '{}',
     top_n             int not null default 5 check (top_n between 1 and 20),
     unsubscribe_token uuid not null default gen_random_uuid(),
     is_active         boolean not null default true,
     created_at        timestamptz not null default now(),
     updated_at        timestamptz not null default now()
   );

   create index if not exists subscribers_active_idx on subscribers (is_active);
   alter table subscribers enable row level security;

   create or replace function subscribe(p_email text, p_disease_sites text[], p_top_n int)
   returns uuid language plpgsql security definer set search_path = public as $$
   declare v_token uuid;
   begin
     insert into subscribers (email, disease_sites, top_n)
     values (lower(trim(p_email)), p_disease_sites, p_top_n)
     on conflict (email) do update
       set disease_sites = excluded.disease_sites, top_n = excluded.top_n,
           is_active = true, updated_at = now()
     returning unsubscribe_token into v_token;
     return v_token;
   end; $$;

   create or replace function get_subscriber(p_token uuid)
   returns table (email text, disease_sites text[], top_n int, is_active boolean)
   language plpgsql security definer set search_path = public as $$
   begin
     return query select s.email, s.disease_sites, s.top_n, s.is_active
       from subscribers s where s.unsubscribe_token = p_token;
   end; $$;

   create or replace function update_preferences(p_token uuid, p_disease_sites text[], p_top_n int)
   returns boolean language plpgsql security definer set search_path = public as $$
   begin
     update subscribers set disease_sites = p_disease_sites, top_n = p_top_n,
       is_active = true, updated_at = now() where unsubscribe_token = p_token;
     return found;
   end; $$;

   create or replace function unsubscribe(p_token uuid)
   returns boolean language plpgsql security definer set search_path = public as $$
   begin
     update subscribers set is_active = false, updated_at = now()
       where unsubscribe_token = p_token;
     return found;
   end; $$;

   revoke all on subscribers from anon, authenticated;
   grant execute on function subscribe(text, text[], int)          to anon;
   grant execute on function get_subscriber(uuid)                  to anon;
   grant execute on function update_preferences(uuid, text[], int) to anon;
   grant execute on function unsubscribe(uuid)                     to anon;
   ```
   </details>

   Then run the numbered files in [`supabase/`](supabase/) as listed under [Supabase Setup](#supabase-setup).

   From **Settings → API**, copy the **Project URL** and **publishable (anon) key** into the two constants near the top of `dashboard/preferences.html` and `dashboard/index.html`. Separately copy the **secret (service_role) key** — that one only ever goes into a GitHub secret, never into `preferences.html` or any client-side file.

2. **Create a [Resend](https://resend.com) account** and verify a sending domain (or subdomain, e.g. `digest.yourdomain.com`) under **Domains**, following the DNS records it generates. Then create an API key under **API Keys**.

3. **Add 5 more GitHub secrets** (same place as the pipeline secrets above — **Settings → Secrets and variables → Actions**):

   | Secret | Value |
   |--------|-------|
   | `SUPABASE_URL` | Your Supabase project URL |
   | `SUPABASE_SERVICE_ROLE_KEY` | The secret/service-role key — never the publishable one |
   | `RESEND_API_KEY` | From Resend → API Keys |
   | `RESEND_FROM` | e.g. `OncoPulse <digest@yourdomain.com>`, matching your verified Resend domain |
   | `SITE_URL` | Where `dashboard/` is actually served — e.g. `https://YOURUSERNAME.github.io/oncopulse/dashboard` (check your Pages setting; if Pages serves the repo root rather than `/dashboard`, include the `/dashboard` suffix here) |

Run **OncoPulse Digest Emails** once manually with *test_to* set to your address to confirm emails arrive.

---

## Setup

### 1. Fork / clone this repo

```bash
git clone https://github.com/YOURUSERNAME/oncopulse.git
cd oncopulse
```

### 2. Add secrets to GitHub

Go to **Settings → Secrets and variables → Actions → New repository secret**:

| Secret | Value | Required |
|--------|-------|----------|
| `ANTHROPIC_API_KEY` | Your Anthropic API key from console.anthropic.com | ✅ Yes |
| `PUBMED_API_KEY` | From ncbi.nlm.nih.gov/account (free) — raises rate limit from 3 to 10 req/sec | Optional |

### 3. Enable GitHub Pages

Go to **Settings → Pages**:
- Source: **Deploy from a branch**
- Branch: `main` / folder: `/ (root)`
- Save

Your dashboard will be live at: `https://YOURUSERNAME.github.io/oncopulse/dashboard/`

### 4. Run the first update manually

Go to **Actions → OncoPulse Weekly Literature Update → Run workflow**

Or run locally:

```bash
cd pipeline
pip install -r requirements.txt

export ANTHROPIC_API_KEY="sk-ant-..."
export OUTPUT_DIR="../dashboard"

python fetch_and_summarize.py --days 7 --max-articles 60
```

---

## Configuration

Edit environment variables in `.github/workflows/weekly_update.yml` or set locally:

| Variable | Default | Description |
|----------|---------|-------------|
| `LOOKBACK_DAYS` | `7` | Days of literature to fetch |
| `MAX_ARTICLES` | `60` | Max articles to summarize per run (cost control) |
| `OUTPUT_DIR` | `../dashboard` | Where to write `data.json` |
| `CACHE_DIR` | `.cache` | Cache directory (avoids re-summarizing seen articles) |

### Cron schedule

Edit `.github/workflows/weekly_update.yml`:
```yaml
- cron: "0 6 * * 1"   # Monday 6 AM UTC — change to suit your timezone
```

---

## Cost Estimate

Using Claude Sonnet at ~$3/MTok input + $15/MTok output:

| Articles/week | Est. tokens | Est. cost |
|--------------|-------------|-----------|
| 30 | ~120K | ~$0.50 |
| 60 | ~240K | ~$1.00 |
| 100 | ~400K | ~$1.65 |

The `.cache/` directory prevents re-summarizing articles already seen, keeping costs low on re-runs.

---

## Project Structure

```
oncopulse/
├── pipeline/
│   ├── fetch_and_summarize.py   # Main pipeline script
│   ├── send_digests.py          # Weekly email digest sender
│   ├── disease_groups.json      # Broad disease-site taxonomy (shared with preferences.html)
│   ├── requirements.txt
│   └── .cache/                  # Auto-created, cached summaries
├── dashboard/
│   ├── index.html               # The web dashboard (incl. sign-in, sync, add-a-journal)
│   ├── preferences.html         # Email digest subscribe/manage page
│   └── data.json                # Generated weekly by pipeline
├── supabase/
│   ├── 002_per_site_top_n.sql            # Per-site digest counts
│   ├── 003_added_journals.sql            # Shared list of added journals
│   ├── 004_user_settings.sql             # Signed-in users' synced settings
│   ├── 005_digest_for_signed_in_user.sql # Digest page follows sign-in
│   └── magic_link_email_body.html        # Sign-in email template body
├── .github/
│   └── workflows/
│       ├── weekly_update.yml    # GitHub Actions schedule (literature pipeline)
│       └── send_digests.yml     # Digest emails, 30 min after the weekly update
└── README.md
```

---

## Sources

| Source | What it pulls |
|--------|--------------|
| **PubMed** | Phase 2/3 RCTs, meta-analyses published in last 7 days |
| **FDA RSS** | FDA oncology drug approvals and regulatory actions |
| **NEJM** | New England Journal of Medicine |
| **Lancet Oncol** | The Lancet Oncology |
| **JCO** | Journal of Clinical Oncology |
| **Blood** | ASH Blood journal |
| **Cancer Cell** | Cell Press Cancer Cell |
| **Nature Cancer** | Nature Cancer |
| **JAMA Oncol** | JAMA Oncology |
| **Ann Oncol** | Annals of Oncology |
| **Cancer Discov** | Cancer Discovery (AACR) |
| **Journals you add** | Any PubMed journal added in ⚙ Settings: its oncology articles, any study type |

---

## Local Development

Open `dashboard/index.html` in a browser. If no `data.json` exists yet, you'll see a helpful error message. Run the pipeline first to generate it.

For local testing with a small sample:
```bash
python fetch_and_summarize.py --days 3 --max-articles 10 --skip-pubmed
```
