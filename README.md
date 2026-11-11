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
1. Pulls recent articles from PubMed, FDA, and 10 major journal RSS feeds
2. Filters for clinical relevance (Phase 2+, FDA actions, reviews)
3. Calls Claude to generate structured summaries (design, endpoints, results, clinical takeaway)
4. Commits updated `data.json` to the repo
5. GitHub Pages serves the dashboard automatically

---

## Email Digest Subscriptions

Anyone can subscribe to personalized weekly emails: pick which disease sites to follow and, for each site, how many top-ranked abstracts (1–20) it should include. Each site arrives as its own email, about 30 minutes after the weekly update finishes. Subscribe via the **✉ Email Digest** button in the dashboard header, which opens `dashboard/preferences.html`.

```
preferences.html ──► Supabase (subscribers table)
                                   │
data.json ──► send_digests.py ◄───┘   (reads active subscribers)
                     │
                     ▼
              Resend API ──► subscriber's inbox
```

Each site's email holds that site's top-N articles by `data.json`'s `importance_score`, the same ranking the dashboard uses. A site with no articles that week sends no email. Each article link in the email prefers a free PMC full-text copy (resolved via NCBI's id-converter) when one exists for that article, falling back to the PubMed or publisher page when it doesn't (most subscription journals, e.g. NEJM/Lancet, don't have a free full-text copy available).

Subscriber data lives in a Supabase table, locked down with row-level security — the public preferences page can only reach it through four RPC functions (`subscribe`, `get_subscriber`, `update_preferences`, `unsubscribe`), each of which checks the caller's own unique token server-side. There's no way for the page's public key to read or export the subscriber list. The weekly GitHub Actions job is the only thing with full read access, via a separate, non-public service-role key.

Sending is a separate workflow, `.github/workflows/send_digests.yml`. It starts when the weekly update finishes successfully, waits 30 minutes, checks out the freshly committed `data.json`, and runs `pipeline/send_digests.py`. If any email fails to send, the step fails (red in Actions) and the run summary lists the Resend error.

To test: **Actions → OncoPulse Digest Emails → Run workflow**, put your own address in *test_to*. That sends every site's email to you only, with no 30-minute wait. Locally, `python pipeline/send_digests.py --dry-run` writes HTML previews to `digest_preview/` without sending anything.

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

   Then run [`supabase/002_per_site_top_n.sql`](supabase/002_per_site_top_n.sql) once as well. It adds the per-site abstract counts and the `*_v2` functions the preferences page uses.

   From **Settings → API**, copy the **Project URL** and **publishable (anon) key** into the two constants near the top of `dashboard/preferences.html`. Separately copy the **secret (service_role) key** — that one only ever goes into a GitHub secret, never into `preferences.html` or any client-side file.

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
- Branch: `main` / folder: `/dashboard`
- Save

Your dashboard will be live at: `https://YOURUSERNAME.github.io/oncopulse/`

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
│   ├── index.html               # The web dashboard
│   ├── preferences.html         # Email digest subscribe/manage page
│   └── data.json                # Generated weekly by pipeline
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

---

## Local Development

Open `dashboard/index.html` in a browser. If no `data.json` exists yet, you'll see a helpful error message. Run the pipeline first to generate it.

For local testing with a small sample:
```bash
python fetch_and_summarize.py --days 3 --max-articles 10 --skip-pubmed
```
