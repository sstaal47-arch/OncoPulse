#!/usr/bin/env python3
"""
OncoPulse — Weekly Oncology Literature Pipeline (Simplified)
Fetches up to 100 articles from PubMed, FDA, and journal RSS feeds.
Skips any article already seen in previous runs.
Outputs data.json with title, abstract, pubmed link, and source link.
No Claude API required.
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

# ─── Logging ──────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
)
log = logging.getLogger("oncopulse")

# ─── Config ───────────────────────────────────────────────────────────────────
PUBMED_API_KEY = os.environ.get("PUBMED_API_KEY", "")
OUTPUT_DIR     = Path(os.environ.get("OUTPUT_DIR", "../dashboard"))
CACHE_DIR      = Path(os.environ.get("CACHE_DIR", ".cache"))
LOOKBACK_DAYS  = int(os.environ.get("LOOKBACK_DAYS", "7"))
MAX_ARTICLES   = int(os.environ.get("MAX_ARTICLES", "100"))

CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

# File that tracks all article UIDs ever seen across runs
SEEN_FILE = CACHE_DIR / "seen_articles.json"

# ─── Seen-articles tracker ────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        return set(json.loads(SEEN_FILE.read_text()))
    return set()

def save_seen(seen: set):
    SEEN_FILE.write_text(json.dumps(list(seen)))

# ─── Disease classifier ───────────────────────────────────────────────────────
DISEASE_KEYWORDS = {
    "lymphoma":    ["lymphoma", "dlbcl", "follicular lymphoma", "hodgkin", "mantle cell",
                    "marginal zone", "burkitt", "t-cell lymphoma", "ptcl", "ctcl"],
    "leukemia":    ["leukemia", "cll", "aml", "all", "cml", "mds", "myelodysplastic",
                    "myeloproliferative", "mpn", "chronic myeloid"],
    "myeloma":     ["myeloma", "amyloidosis", "plasma cell", "smoldering myeloma"],
    "lung":        ["lung cancer", "nsclc", "sclc", "small cell", "non-small cell", "mesothelioma"],
    "breast":      ["breast cancer", "breast carcinoma", "her2", "triple negative breast", "tnbc"],
    "gi":          ["colorectal", "colon cancer", "rectal cancer", "gastric", "esophageal",
                    "pancreatic", "hepatocellular", "cholangiocarcinoma"],
    "gu":          ["prostate cancer", "renal cell", "bladder cancer", "urothelial", "kidney cancer"],
    "cns":         ["glioblastoma", "gbm", "brain tumor", "cns lymphoma", "glioma"],
    "gynecologic": ["ovarian", "endometrial", "cervical cancer", "uterine"],
    "skin":        ["melanoma", "merkel cell", "cutaneous squamous"],
    "head_neck":   ["head and neck", "nasopharyngeal", "thyroid cancer"],
    "sarcoma":     ["sarcoma", "gist", "osteosarcoma", "ewing"],
    "other":       []
}

def classify_disease(text: str) -> str:
    t = text.lower()
    for disease, keywords in DISEASE_KEYWORDS.items():
        if disease == "other":
            continue
        if any(kw in t for kw in keywords):
            return disease
    return "other"

def classify_category(disease: str) -> str:
    return "heme" if disease in {"lymphoma", "leukemia", "myeloma"} else "solid"

def classify_type(text: str, source: str) -> str:
    if source == "fda":
        return "fda"
    t = text.lower()
    if any(x in t for x in ["phase 3", "phase iii", "phase3"]):
        return "phase3"
    if any(x in t for x in ["phase 2", "phase ii", "phase2"]):
        return "phase2"
    if any(x in t for x in ["meta-analysis", "systematic review", "review"]):
        return "review"
    return "phase3"  # default for clinical trials

# ─── PubMed ───────────────────────────────────────────────────────────────────
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

ONCOLOGY_QUERY = """
(cancer[tiab] OR oncol*[tiab] OR tumor[tiab] OR carcinoma[tiab]
 OR lymphoma[tiab] OR leukemia[tiab] OR myeloma[tiab] OR sarcoma[tiab]
 OR melanoma[tiab] OR glioblastoma[tiab])
AND
(randomized controlled trial[pt] OR clinical trial[pt] OR meta-analysis[pt]
 OR phase 2[tiab] OR phase 3[tiab] OR phase II[tiab] OR phase III[tiab])
AND humans[mesh]
"""

def fetch_pubmed(days_back: int, max_ids: int = 200) -> list:
    mindate = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    maxdate = date.today().strftime("%Y/%m/%d")

    params = {
        "db": "pubmed", "term": ONCOLOGY_QUERY,
        "retmax": max_ids, "mindate": mindate, "maxdate": maxdate,
        "datetype": "pdat", "retmode": "json", "sort": "relevance",
    }
    if PUBMED_API_KEY:
        params["api_key"] = PUBMED_API_KEY

    log.info("Searching PubMed (last %d days)…", days_back)
    try:
        r = requests.get(f"{PUBMED_BASE}/esearch.fcgi", params=params, timeout=30)
        r.raise_for_status()
        ids = r.json()["esearchresult"]["idlist"]
        log.info("  Found %d PubMed IDs", len(ids))
    except Exception as e:
        log.error("PubMed search failed: %s", e)
        return []

    articles = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i+50]
        fetch_params = {
            "db": "pubmed", "id": ",".join(batch),
            "retmode": "json", "rettype": "abstract",
        }
        if PUBMED_API_KEY:
            fetch_params["api_key"] = PUBMED_API_KEY

        try:
            r = requests.get(f"{PUBMED_BASE}/efetch.fcgi", params=fetch_params, timeout=30)
            r.raise_for_status()
            data = r.json()
        except Exception as e:
            log.error("PubMed fetch batch failed: %s", e)
            continue

        pubmed_set = data.get("PubmedArticleSet", {})
        arts = pubmed_set.get("PubmedArticle", []) if isinstance(pubmed_set, dict) else []
        if not isinstance(arts, list):
            arts = [arts] if arts else []

        for art in arts:
            try:
                medline  = art.get("MedlineCitation", {})
                pmid     = str(medline.get("PMID", {}).get("#text", medline.get("PMID", "")))
                article  = medline.get("Article", {})

                title = article.get("ArticleTitle", "")
                if isinstance(title, dict):
                    title = title.get("#text", str(title))
                if not title:
                    continue

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

                journal = article.get("Journal", {}).get("Title", "")
                pub_date_obj = article.get("Journal", {}).get("JournalIssue", {}).get("PubDate", {})
                year  = pub_date_obj.get("Year", str(date.today().year))
                month = pub_date_obj.get("Month", "")
                pub_date = f"{year} {month}".strip()

                uid = f"pmid_{pmid}"
                text_for_classify = title + " " + abstract
                disease = classify_disease(text_for_classify)

                articles.append({
                    "uid":      uid,
                    "pmid":     pmid,
                    "title":    title.strip(),
                    "abstract": abstract.strip(),
                    "journal":  journal,
                    "pub_date": pub_date,
                    "type":     classify_type(text_for_classify, "pubmed"),
                    "disease":  disease,
                    "category": classify_category(disease),
                    "source":   "pubmed",
                    "source_link": "",
                    "pubmed_link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
            except Exception as e:
                log.debug("Error parsing PubMed article: %s", e)

        time.sleep(0.34)

    log.info("  Parsed %d PubMed articles", len(articles))
    return articles

# ─── FDA RSS ──────────────────────────────────────────────────────────────────
def fetch_fda(days_back: int) -> list:
    log.info("Fetching FDA approvals…")
    cutoff = datetime.now() - timedelta(days=days_back)
    articles = []

    fda_rss = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
    try:
        feed = feedparser.parse(fda_rss)
        for entry in feed.entries:
            published = datetime(*entry.published_parsed[:6]) if hasattr(entry, 'published_parsed') and entry.published_parsed else datetime.now()
            if published < cutoff:
                continue
            title   = entry.get("title", "").strip()
            summary = entry.get("summary", "").strip()
            link    = entry.get("link", "")
            text    = (title + " " + summary).lower()
            if not any(kw in text for kw in ["cancer", "tumor", "oncol", "lymphoma",
                                              "leukemia", "myeloma", "carcinoma", "melanoma"]):
                continue
            uid = f"fda_{hashlib.md5(link.encode()).hexdigest()[:10]}"
            disease = classify_disease(title + " " + summary)
            articles.append({
                "uid":         uid,
                "pmid":        "",
                "title":       title,
                "abstract":    summary,
                "journal":     "FDA",
                "pub_date":    published.strftime("%Y %b"),
                "type":        "fda",
                "disease":     disease,
                "category":    classify_category(disease),
                "source":      "fda",
                "source_link": link,
                "pubmed_link": "",
            })
    except Exception as e:
        log.warning("FDA RSS failed: %s", e)

    log.info("  Found %d FDA items", len(articles))
    return articles

# ─── Journal RSS ──────────────────────────────────────────────────────────────
JOURNAL_FEEDS = {
    "NEJM":          "https://www.nejm.org/action/showFeed?jc=nejmoa&type=etoc&feed=rss",
    "Lancet Oncol":  "https://www.thelancet.com/rssfeed/lanonc_current.xml",
    "JCO":           "https://ascopubs.org/action/showFeed?type=etoc&feed=rss&jc=jco",
    "Blood":         "https://ashpublications.org/rss/site_6/1.xml",
    "Cancer Cell":   "https://www.cell.com/cancer-cell/current.rss",
    "Nature Cancer": "https://www.nature.com/natcancer.rss",
    "JAMA Oncol":    "https://jamanetwork.com/rss/site_375/67.xml",
    "Ann Oncol":     "https://www.annalsofoncology.org/rssfeed/ANNONC.xml",
    "Cancer Discov": "https://aacrjournals.org/cancerdiscovery/pages/rss",
}

ONCOLOGY_TERMS = [
    "cancer", "tumor", "carcinoma", "oncol", "lymphoma", "leukemia",
    "myeloma", "melanoma", "sarcoma", "chemotherapy", "immunotherapy",
    "checkpoint", "car-t", "bispecific", "antibody-drug", "clinical trial",
    "phase", "survival", "randomized",
]

def fetch_rss(days_back: int) -> list:
    cutoff = datetime.now() - timedelta(days=days_back)
    articles = []

    for journal_name, feed_url in JOURNAL_FEEDS.items():
        log.info("  Fetching %s…", journal_name)
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                published = datetime(*entry.published_parsed[:6]) if hasattr(entry, 'published_parsed') and entry.published_parsed else datetime.now()
                if published < cutoff:
                    continue
                title   = entry.get("title", "").strip()
                summary = entry.get("summary", entry.get("description", "")).strip()
                link    = entry.get("link", "")
                if not title:
                    continue
                text = (title + " " + summary).lower()
                if not any(t in text for t in ONCOLOGY_TERMS):
                    continue

                uid = f"rss_{hashlib.md5(link.encode()).hexdigest()[:10]}"
                disease = classify_disease(title + " " + summary)
                articles.append({
                    "uid":         uid,
                    "pmid":        "",
                    "title":       title,
                    "abstract":    summary,
                    "journal":     journal_name,
                    "pub_date":    published.strftime("%Y %b"),
                    "type":        classify_type(title + " " + summary, "rss"),
                    "disease":     disease,
                    "category":    classify_category(disease),
                    "source":      "rss",
                    "source_link": link,
                    "pubmed_link": "",
                })
        except Exception as e:
            log.warning("  Failed %s: %s", journal_name, e)

    log.info("  Found %d RSS articles", len(articles))
    return articles

# ─── Deduplication ────────────────────────────────────────────────────────────
def deduplicate(articles: list) -> list:
    seen = {}
    out  = []
    for a in articles:
        key = "".join(c.lower() for c in a["title"] if c.isalnum())[:80]
        if key not in seen:
            seen[key] = True
            out.append(a)
    return out

# ─── Main pipeline ────────────────────────────────────────────────────────────
def run_pipeline(args):
    log.info("═══ OncoPulse pipeline starting (lookback=%d days, max=%d) ═══",
             args.days, args.max_articles)

    seen_uids = load_seen()
    log.info("Loaded %d previously seen article UIDs", len(seen_uids))

    # Fetch
    all_articles = []
    if not args.skip_fda:
        all_articles.extend(fetch_fda(args.days))
    if not args.skip_rss:
        all_articles.extend(fetch_rss(args.days))
    if not args.skip_pubmed:
        all_articles.extend(fetch_pubmed(args.days))

    log.info("Total fetched: %d", len(all_articles))

    # Dedup within this batch
    all_articles = deduplicate(all_articles)
    log.info("After dedup: %d", len(all_articles))

    # Remove already-seen articles
    new_articles = [a for a in all_articles if a["uid"] not in seen_uids]
    log.info("New (not previously seen): %d", len(new_articles))

    # Cap at max
    new_articles = new_articles[:args.max_articles]

    # Mark all as seen
    for a in new_articles:
        seen_uids.add(a["uid"])
    save_seen(seen_uids)

    # Build stats
    stats = {
        "total":  len(new_articles),
        "phase3": sum(1 for a in new_articles if a["type"] == "phase3"),
        "phase2": sum(1 for a in new_articles if a["type"] == "phase2"),
        "fda":    sum(1 for a in new_articles if a["type"] == "fda"),
        "review": sum(1 for a in new_articles if a["type"] == "review"),
    }

    week_label = (
        f"{(date.today() - timedelta(days=args.days)).strftime('%b %-d')}–"
        f"{date.today().strftime('%b %-d, %Y')}"
    )

    output = {
        "generated_at": datetime.utcnow().isoformat() + "Z",
        "week_label":   week_label,
        "lookback_days": args.days,
        "stats":        stats,
        "articles":     new_articles,
    }

    out_path = OUTPUT_DIR / "data.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log.info("Wrote %s (%d articles, %d KB)",
             out_path, len(new_articles), out_path.stat().st_size // 1024)
    log.info("═══ Done ═══")

# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OncoPulse pipeline")
    parser.add_argument("--days",         type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--max-articles", type=int, default=MAX_ARTICLES)
    parser.add_argument("--skip-pubmed",  action="store_true")
    parser.add_argument("--skip-fda",     action="store_true")
    parser.add_argument("--skip-rss",     action="store_true")
    args = parser.parse_args()
    run_pipeline(args)
