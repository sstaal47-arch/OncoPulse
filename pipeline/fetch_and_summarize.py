#!/usr/bin/env python3
"""
OncoPulse — Weekly Oncology Literature Pipeline
Fetches from PubMed, FDA, and journal RSS feeds,
summarizes each article via Claude, outputs data.json
"""

import os
import json
import time
import hashlib
import logging
import argparse
from datetime import datetime, timedelta, date
from pathlib import Path

import requests
import feedparser
import anthropic

# ─── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oncopulse")

# ─── Config ──────────────────────────────────────────────────────────────────
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]
PUBMED_API_KEY    = os.environ.get("PUBMED_API_KEY", "")   # optional, raises rate limit
OUTPUT_DIR        = Path(os.environ.get("OUTPUT_DIR", "../dashboard"))
CACHE_DIR         = Path(os.environ.get("CACHE_DIR", ".cache"))
LOOKBACK_DAYS     = int(os.environ.get("LOOKBACK_DAYS", "7"))
MAX_ARTICLES      = int(os.environ.get("MAX_ARTICLES", "60"))  # Claude API cost guard

CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

client = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ─── Disease-area keyword map ─────────────────────────────────────────────────
DISEASE_KEYWORDS = {
    "lymphoma":  ["lymphoma", "dlbcl", "follicular lymphoma", "hodgkin", "mantle cell",
                  "marginal zone", "burkitt", "t-cell lymphoma", "ptcl", "ctcl"],
    "leukemia":  ["leukemia", "cll", "aml", "all", "cml", "mds", "myelodysplastic",
                  "myeloproliferative", "mpn", "chronic myeloid"],
    "myeloma":   ["myeloma", "amyloidosis", "plasma cell", "smoldering myeloma", "waldenström"],
    "lung":      ["lung cancer", "nsclc", "sclc", "small cell", "non-small cell",
                  "mesothelioma", "pulmonary carcinoid"],
    "breast":    ["breast cancer", "breast carcinoma", "her2", "triple negative breast",
                  "tnbc", "hormone receptor"],
    "gi":        ["colorectal", "colon cancer", "rectal cancer", "gastric", "esophageal",
                  "pancreatic", "hepatocellular", "cholangiocarcinoma", "gastrointestinal"],
    "gu":        ["prostate cancer", "renal cell", "bladder cancer", "urothelial",
                  "testicular", "kidney cancer", "urologic"],
    "cns":       ["glioblastoma", "gbm", "brain tumor", "cns lymphoma", "medulloblastoma",
                  "meningioma", "glioma"],
    "gynecologic": ["ovarian", "endometrial", "cervical cancer", "uterine", "fallopian tube"],
    "skin":      ["melanoma", "merkel cell", "cutaneous squamous", "basal cell carcinoma",
                  "uveal melanoma"],
    "head_neck": ["head and neck", "squamous cell carcinoma of the head", "nasopharyngeal",
                  "thyroid cancer", "salivary gland"],
    "sarcoma":   ["sarcoma", "gastrointestinal stromal", "gist", "osteosarcoma", "ewing"],
    "other":     []  # catch-all
}

def classify_disease(text: str) -> str:
    text_lower = text.lower()
    for disease, keywords in DISEASE_KEYWORDS.items():
        if disease == "other":
            continue
        if any(kw in text_lower for kw in keywords):
            return disease
    return "other"

def classify_category(disease: str) -> str:
    heme = {"lymphoma", "leukemia", "myeloma"}
    return "heme" if disease in heme else "solid"

# ─── Cache helpers ────────────────────────────────────────────────────────────
def cache_key(uid: str) -> Path:
    return CACHE_DIR / f"{hashlib.md5(uid.encode()).hexdigest()}.json"

def load_cache(uid: str) -> object:
    path = cache_key(uid)
    if path.exists():
        return json.loads(path.read_text())
    return None

def save_cache(uid: str, data: dict):
    cache_key(uid).write_text(json.dumps(data, indent=2))

# ─── PubMed fetcher ───────────────────────────────────────────────────────────
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

ONCOLOGY_PUBMED_QUERY = """
(cancer[tiab] OR oncol*[tiab] OR tumor[tiab] OR tumour[tiab] OR carcinoma[tiab]
 OR lymphoma[tiab] OR leukemia[tiab] OR myeloma[tiab] OR sarcoma[tiab]
 OR melanoma[tiab] OR glioblastoma[tiab])
AND
(clinical trial[pt] OR randomized controlled trial[pt] OR meta-analysis[pt]
 OR systematic review[tiab] OR phase 2[tiab] OR phase 3[tiab]
 OR phase II[tiab] OR phase III[tiab] OR FDA approv*[tiab])
AND
(humans[mesh])
"""

def fetch_pubmed_ids(days_back: int = 7, max_ids: int = 200) -> list[str]:
    """Search PubMed for recent oncology trial publications."""
    mindate = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    maxdate = date.today().strftime("%Y/%m/%d")

    params = {
        "db": "pubmed",
        "term": ONCOLOGY_PUBMED_QUERY,
        "retmax": max_ids,
        "mindate": mindate,
        "maxdate": maxdate,
        "datetype": "pdat",
        "retmode": "json",
        "sort": "relevance",
    }
    if PUBMED_API_KEY:
        params["api_key"] = PUBMED_API_KEY

    log.info("Searching PubMed (last %d days)…", days_back)
    try:
        r = requests.get(f"{PUBMED_BASE}/esearch.fcgi", params=params, timeout=30)
        r.raise_for_status()
        ids = r.json()["esearchresult"]["idlist"]
        log.info("  Found %d PubMed IDs", len(ids))
        return ids
    except Exception as e:
        log.error("PubMed search failed: %s", e)
        return []

def fetch_pubmed_abstracts(pmids: list[str]) -> list[dict]:
    """Fetch abstracts for a list of PubMed IDs in batches."""
    if not pmids:
        return []

    articles = []
    batch_size = 50

    for i in range(0, len(pmids), batch_size):
        batch = pmids[i:i + batch_size]
        params = {
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "json",
            "rettype": "abstract",
        }
        if PUBMED_API_KEY:
            params["api_key"] = PUBMED_API_KEY

        try:
            r = requests.get(f"{PUBMED_BASE}/efetch.fcgi", params=params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error("PubMed fetch batch failed: %s", e)
            continue

        for pmid, article_data in data.get("PubmedArticle", {}).items() if isinstance(data.get("PubmedArticle"), dict) else []:
            pass

        # Parse the JSON structure
        pubmed_articles = data.get("PubmedArticleSet", {})
        if isinstance(pubmed_articles, dict):
            pubmed_articles = pubmed_articles.get("PubmedArticle", [])
        if not isinstance(pubmed_articles, list):
            pubmed_articles = [pubmed_articles] if pubmed_articles else []

        for art in pubmed_articles:
            try:
                medline = art.get("MedlineCitation", {})
                pmid = str(medline.get("PMID", {}).get("#text", medline.get("PMID", "")))
                article = medline.get("Article", {})
                title = article.get("ArticleTitle", "")
                if isinstance(title, dict):
                    title = title.get("#text", str(title))

                # Abstract
                abstract_obj = article.get("Abstract", {})
                abstract_texts = abstract_obj.get("AbstractText", "")
                if isinstance(abstract_texts, list):
                    abstract = " ".join(
                        (t.get("#text", str(t)) if isinstance(t, dict) else str(t))
                        for t in abstract_texts
                    )
                elif isinstance(abstract_texts, dict):
                    abstract = abstract_texts.get("#text", "")
                else:
                    abstract = str(abstract_texts) if abstract_texts else ""

                # Journal
                journal = article.get("Journal", {}).get("Title", "")
                pub_date_obj = article.get("Journal", {}).get("JournalIssue", {}).get("PubDate", {})
                year = pub_date_obj.get("Year", str(date.today().year))
                month = pub_date_obj.get("Month", "Jan")
                pub_date = f"{year} {month}"

                if not title or not abstract:
                    continue

                articles.append({
                    "uid": f"pmid_{pmid}",
                    "pmid": pmid,
                    "title": title,
                    "abstract": abstract,
                    "journal": journal,
                    "pub_date": pub_date,
                    "source": "pubmed",
                })
            except Exception as e:
                log.debug("Error parsing article: %s", e)
                continue

        time.sleep(0.34)  # NCBI rate limit: 3 req/sec without key, 10/sec with key

    log.info("  Parsed %d abstracts from PubMed", len(articles))
    return articles

# ─── FDA fetcher ──────────────────────────────────────────────────────────────
FDA_RSS = "https://api.fda.gov/drug/drugsfda.json"

def fetch_fda_approvals(days_back: int = 7) -> list[dict]:
    """Fetch recent FDA oncology drug approvals."""
    min_date = (date.today() - timedelta(days=days_back)).strftime("%Y%m%d")
    url = (
        f"https://api.fda.gov/drug/drugsfda.json"
        f"?search=submissions.submission_type:ORIG+AND+submissions.submission_status:AP"
        f"+AND+submissions.submission_status_date:[{min_date}+TO+99991231]"
        f"&limit=20"
    )
    log.info("Fetching FDA approvals…")
    try:
        r = requests.get(url, timeout=20)
        r.raise_for_status()
        results = r.json().get("results", [])
    except Exception as e:
        log.warning("FDA API fetch failed (%s), trying RSS fallback…", e)
        results = []

    # Fallback: FDA oncology news RSS
    fda_rss_url = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
    articles = []
    try:
        feed = feedparser.parse(fda_rss_url)
        cutoff = datetime.now() - timedelta(days=days_back)
        for entry in feed.entries:
            published = datetime(*entry.published_parsed[:6]) if hasattr(entry, 'published_parsed') and entry.published_parsed else datetime.now()
            if published < cutoff:
                continue
            title = entry.get("title", "")
            summary = entry.get("summary", "")
            link = entry.get("link", "")
            # Filter for oncology/cancer approvals
            text = (title + " " + summary).lower()
            if any(kw in text for kw in ["cancer", "tumor", "oncol", "lymphoma", "leukemia",
                                          "myeloma", "carcinoma", "melanoma", "approv"]):
                uid = f"fda_{hashlib.md5(link.encode()).hexdigest()[:8]}"
                articles.append({
                    "uid": uid,
                    "pmid": uid,
                    "title": title,
                    "abstract": summary,
                    "journal": "FDA",
                    "pub_date": published.strftime("%Y %b"),
                    "source": "fda",
                    "link": link,
                })
    except Exception as e:
        log.warning("FDA RSS fallback failed: %s", e)

    log.info("  Found %d FDA items", len(articles))
    return articles

# ─── Journal RSS fetcher ──────────────────────────────────────────────────────
JOURNAL_FEEDS = {
    "NEJM":         "https://www.nejm.org/action/showFeed?jc=nejmoa&type=etoc&feed=rss",
    "Lancet Oncol": "https://www.thelancet.com/rssfeed/lanonc_current.xml",
    "JCO":          "https://ascopubs.org/action/showFeed?type=etoc&feed=rss&jc=jco",
    "Blood":        "https://ashpublications.org/rss/site_6/1.xml",
    "Cancer Cell":  "https://www.cell.com/cancer-cell/current.rss",
    "Nature Cancer":"https://www.nature.com/natcancer.rss",
    "JAMA Oncol":   "https://jamanetwork.com/rss/site_375/67.xml",
    "Ann Oncol":    "https://www.annalsofoncology.org/rssfeed/ANNONC.xml",
    "JHO":          "https://jhoonline.biomedcentral.com/articles/most-recent/rss.xml",
    "Cancer Discov":"https://aacrjournals.org/cancerdiscovery/pages/rss",
}

def fetch_journal_rss(days_back: int = 7) -> list[dict]:
    """Fetch recent articles from major oncology journal RSS feeds."""
    cutoff = datetime.now() - timedelta(days=days_back)
    articles = []

    for journal_name, feed_url in JOURNAL_FEEDS.items():
        log.info("  Fetching %s…", journal_name)
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                # Parse date
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    published = datetime(*entry.published_parsed[:6])
                else:
                    published = datetime.now()

                if published < cutoff:
                    continue

                title = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                link = entry.get("link", "")

                if not title:
                    continue

                # Filter for oncology relevance
                text = (title + " " + summary).lower()
                oncology_terms = [
                    "cancer", "tumor", "carcinoma", "oncol", "lymphoma", "leukemia",
                    "myeloma", "melanoma", "sarcoma", "chemotherapy", "immunotherapy",
                    "checkpoint inhibitor", "car-t", "bispecific", "adc",
                    "antibody-drug conjugate", "clinical trial", "phase", "survival",
                ]
                if not any(term in text for term in oncology_terms):
                    continue

                uid = f"rss_{hashlib.md5(link.encode()).hexdigest()[:10]}"
                articles.append({
                    "uid": uid,
                    "pmid": uid,
                    "title": title,
                    "abstract": summary,
                    "journal": journal_name,
                    "pub_date": published.strftime("%Y %b"),
                    "source": "rss",
                    "link": link,
                })
        except Exception as e:
            log.warning("  Failed to fetch %s: %s", journal_name, e)

    log.info("  Found %d articles from journal RSS feeds", len(articles))
    return articles

# ─── Deduplication ────────────────────────────────────────────────────────────
def deduplicate(articles: list[dict]) -> list[dict]:
    """Remove duplicates by title similarity."""
    seen_titles = {}
    unique = []
    for art in articles:
        # Normalize title for comparison
        norm = "".join(c.lower() for c in art["title"] if c.isalnum())[:80]
        if norm not in seen_titles:
            seen_titles[norm] = True
            unique.append(art)
    log.info("After dedup: %d unique articles", len(unique))
    return unique

# ─── Relevance filter ─────────────────────────────────────────────────────────
def is_clinically_relevant(article: dict) -> bool:
    """
    Filter for articles that are likely practice-changing or clinically significant.
    Returns True if the article should be included.
    """
    text = (article["title"] + " " + article["abstract"]).lower()

    # Always include FDA items
    if article["source"] == "fda":
        return True

    # Must be clinical (not basic science only)
    clinical_terms = [
        "phase", "clinical trial", "randomized", "rct", "patients", "survival",
        "overall survival", "progression-free", "response rate", "fda", "approval",
        "efficacy", "safety", "cohort", "study", "trial", "meta-analysis",
        "systematic review", "real-world"
    ]
    if not any(term in text for term in clinical_terms):
        return False

    # Exclude pure basic science
    basic_only = ["in vitro", "cell line", "mouse model", "xenograft", "in vivo model"]
    if any(term in text for term in basic_only) and "patients" not in text:
        return False

    return True

# ─── Claude summarizer ────────────────────────────────────────────────────────
SUMMARIZE_SYSTEM = """You are an expert hematology-oncology physician with deep knowledge of clinical trials, cancer therapeutics, and evidence-based medicine. Your task is to analyze oncology literature and produce structured, clinically focused summaries for a weekly literature review dashboard used by oncologists.

You must respond ONLY with a valid JSON object — no markdown, no preamble, no explanation outside the JSON.

Produce concise, information-dense summaries suitable for a busy oncologist. Prioritize clinical implications over statistical minutiae."""

SUMMARIZE_PROMPT = """Analyze this oncology article and return a JSON object with these exact fields:

{{
  "type": "<phase3|phase2|fda|review>",
  "impact": <1-5 integer, 5=practice-changing>,
  "disease": "<lymphoma|leukemia|myeloma|lung|breast|gi|gu|cns|gynecologic|skin|head_neck|sarcoma|other>",
  "category": "<heme|solid>",
  "design": "<1-2 sentences: study design, N, population, arms — be specific about phase, randomization, blinding>",
  "endpoints": "<1 sentence: primary and key secondary endpoints with definitions if relevant>",
  "results": "<2-3 sentences: key quantitative results with HR/ORR/mOS/mPFS and p-values where available>",
  "takeaway": "<2-3 sentences: clinical bottom line — what this means for practice, patient selection, how it changes treatment decisions, any important caveats>"
}}

Type guidance:
- "fda" = FDA approval, accelerated approval, or regulatory action
- "phase3" = Phase 3 or pivotal trial
- "phase2" = Phase 2 (include only if results are compelling/practice-informing)
- "review" = systematic review, meta-analysis, guidelines

Impact guidance:
- 5 = Practice-changing, expect immediate adoption or significant guideline update
- 4 = Clinically important, likely to influence decision-making
- 3 = Informative, adds to evidence base without changing standard of care
- 2 = Hypothesis-generating or narrow applicability
- 1 = Minimally impactful for clinical practice

Article title: {title}

Journal: {journal}

Abstract:
{abstract}"""

def summarize_article(article: dict) -> object:
    """Call Claude to generate a structured summary of an article."""
    uid = article["uid"]

    # Check cache
    cached = load_cache(uid)
    if cached:
        log.debug("Cache hit: %s", uid)
        return cached

    prompt = SUMMARIZE_PROMPT.format(
        title=article["title"],
        journal=article["journal"],
        abstract=article["abstract"][:4000],  # Truncate very long abstracts
    )

    try:
        response = client.messages.create(
            model="claude-sonnet-4-20250514",
            max_tokens=1000,
            system=SUMMARIZE_SYSTEM,
            messages=[{"role": "user", "content": prompt}],
        )
        raw = response.content[0].text.strip()

        # Strip markdown code fences if present
        if raw.startswith("```"):
            raw = raw.split("```")[1]
            if raw.startswith("json"):
                raw = raw[4:]
        raw = raw.strip()

        summary = json.loads(raw)

        # Merge with article metadata
        result = {
            "id": uid,
            "pmid": article["pmid"],
            "title": article["title"],
            "journal": article["journal"],
            "pub_date": article["pub_date"],
            "source": article["source"],
            "link": article.get("link", f"https://pubmed.ncbi.nlm.nih.gov/{article['pmid']}"),
            **summary,
        }

        save_cache(uid, result)
        return result

    except json.JSONDecodeError as e:
        log.warning("JSON parse error for %s: %s", uid, e)
        return None
    except anthropic.APIError as e:
        log.error("Anthropic API error for %s: %s", uid, e)
        time.sleep(5)
        return None
    except Exception as e:
        log.error("Unexpected error for %s: %s", uid, e)
        return None

# ─── Main pipeline ────────────────────────────────────────────────────────────
def run_pipeline(args):
    log.info("═══ OncoPulse pipeline starting (lookback=%d days) ═══", args.days)
    start = datetime.now()

    # 1. Fetch from all sources
    all_articles = []

    if not args.skip_pubmed:
        pmids = fetch_pubmed_ids(days_back=args.days, max_ids=150)
        pubmed_articles = fetch_pubmed_abstracts(pmids)
        all_articles.extend(pubmed_articles)

    if not args.skip_fda:
        fda_articles = fetch_fda_approvals(days_back=args.days)
        all_articles.extend(fda_articles)

    if not args.skip_rss:
        rss_articles = fetch_journal_rss(days_back=args.days)
        all_articles.extend(rss_articles)

    log.info("Total raw articles: %d", len(all_articles))

    # 2. Filter and deduplicate
    all_articles = [a for a in all_articles if is_clinically_relevant(a)]
    log.info("After relevance filter: %d articles", len(all_articles))

    all_articles = deduplicate(all_articles)

    # 3. Prioritize and cap (FDA first, then by source)
    fda_arts   = [a for a in all_articles if a["source"] == "fda"]
    rss_arts   = [a for a in all_articles if a["source"] == "rss"]
    pm_arts    = [a for a in all_articles if a["source"] == "pubmed"]

    priority_ordered = fda_arts + rss_arts + pm_arts
    capped = priority_ordered[:args.max_articles]
    log.info("Processing %d articles (cap=%d)", len(capped), args.max_articles)

    # 4. Summarize each with Claude
    summarized = []
    for i, article in enumerate(capped, 1):
        log.info("[%d/%d] Summarizing: %s…", i, len(capped), article["title"][:70])
        result = summarize_article(article)
        if result:
            summarized.append(result)
        time.sleep(0.5)  # Gentle rate limiting

    log.info("Successfully summarized %d articles", len(summarized))

    # 5. Sort by impact desc, then date
    summarized.sort(key=lambda x: (-x.get("impact", 0), x.get("pub_date", "")))

    # 6. Build stats
    stats = {
        "total": len(summarized),
        "phase3": sum(1 for a in summarized if a.get("type") == "phase3"),
        "phase2": sum(1 for a in summarized if a.get("type") == "phase2"),
        "fda":    sum(1 for a in summarized if a.get("type") == "fda"),
        "review": sum(1 for a in summarized if a.get("type") == "review"),
        "by_disease": {},
    }
    for disease in DISEASE_KEYWORDS:
        count = sum(1 for a in summarized if a.get("disease") == disease)
        if count > 0:
            stats["by_disease"][disease] = count

    # 7. Build output payload
    week_label = (
        f"{(date.today() - timedelta(days=args.days)).strftime('%b %-d')}–"
        f"{date.today().strftime('%b %-d, %Y')}"
    )

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "week_label": week_label,
        "lookback_days": args.days,
        "stats": stats,
        "articles": summarized,
    }

    # 8. Write output
    out_path = OUTPUT_DIR / "data.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log.info("Wrote %s (%d KB)", out_path, out_path.stat().st_size // 1024)

    elapsed = (datetime.now() - start).total_seconds()
    log.info("═══ Pipeline complete in %.1fs ═══", elapsed)

    return output

# ─── CLI ─────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OncoPulse weekly literature pipeline")
    parser.add_argument("--days",         type=int, default=LOOKBACK_DAYS,  help="Days to look back")
    parser.add_argument("--max-articles", type=int, default=MAX_ARTICLES,   help="Max articles to summarize")
    parser.add_argument("--skip-pubmed",  action="store_true", help="Skip PubMed fetch")
    parser.add_argument("--skip-fda",     action="store_true", help="Skip FDA fetch")
    parser.add_argument("--skip-rss",     action="store_true", help="Skip journal RSS fetch")
    args = parser.parse_args()
    run_pipeline(args)
