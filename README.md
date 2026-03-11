# OncoPulse — Weekly Oncology Literature Dashboard

Automated weekly digest of oncology literature: Phase 2/3 trials, FDA approvals, and key reviews — summarized by Claude and served as a searchable web dashboard.

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
│   ├── requirements.txt
│   └── .cache/                  # Auto-created, cached summaries
├── dashboard/
│   ├── index.html               # The web dashboard
│   └── data.json                # Generated weekly by pipeline
├── .github/
│   └── workflows/
│       └── weekly_update.yml    # GitHub Actions schedule
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
