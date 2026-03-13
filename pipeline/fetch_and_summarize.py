#!/usr/bin/env python3
"""
OncoPulse — Weekly Oncology Literature Pipeline
Fetches up to 300 articles from PubMed, FDA, and journal RSS feeds.
Classifies articles using full NCCN disease taxonomy.
Scores articles using Altmetric attention score + study design + journal tier.
Skips any article already seen in previous 14 days.
Outputs data.json with title, abstract, pubmed link, source link, and scores.
No Claude API required.
"""

import os
import json
import math
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
MAX_ARTICLES   = int(os.environ.get("MAX_ARTICLES", "300"))

CACHE_DIR.mkdir(exist_ok=True)
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SEEN_FILE = CACHE_DIR / "seen_articles.json"

# ─── Seen-articles tracker ────────────────────────────────────────────────────
def load_seen() -> set:
    if SEEN_FILE.exists():
        try:
            data = json.loads(SEEN_FILE.read_text())
            if isinstance(data, list):
                return set(data)
            elif isinstance(data, dict):
                return set(data.keys())
        except Exception:
            pass
    return set()

def save_seen(seen: set):
    existing_raw = {}
    if SEEN_FILE.exists():
        try:
            raw = json.loads(SEEN_FILE.read_text())
            if isinstance(raw, dict):
                existing_raw = raw
        except Exception:
            pass
    now_ts = datetime.utcnow().isoformat()
    for uid in seen:
        if uid not in existing_raw:
            existing_raw[uid] = now_ts
    cutoff = (datetime.utcnow() - timedelta(days=14)).isoformat()
    pruned = {uid: ts for uid, ts in existing_raw.items() if ts >= cutoff}
    SEEN_FILE.write_text(json.dumps(pruned))
    log.info("Seen cache: %d entries (pruned from %d)", len(pruned), len(existing_raw))

# ─── NCCN Disease Taxonomy ────────────────────────────────────────────────────
NCCN_DISEASE_KEYWORDS = [
    ("dlbcl",              ["diffuse large b-cell", "dlbcl", "large b-cell lymphoma"]),
    ("follicular_lymphoma",["follicular lymphoma", "follicular grade"]),
    ("mantle_cell",        ["mantle cell lymphoma", "mcl"]),
    ("marginal_zone",      ["marginal zone lymphoma", "malt lymphoma", "mzl", "splenic marginal"]),
    ("burkitt",            ["burkitt lymphoma", "burkitt's lymphoma"]),
    ("primary_mediastinal",["primary mediastinal", "pmbcl"]),
    ("transformed_fl",     ["transformed follicular", "richter transformation", "richter's syndrome"]),
    ("waldenstrom",        ["waldenstrom", "lymphoplasmacytic lymphoma"]),
    ("ptcl",               ["peripheral t-cell lymphoma", "ptcl", "angioimmunoblastic", "aitl", "anaplastic large cell", "alcl"]),
    ("ctcl",               ["cutaneous t-cell lymphoma", "ctcl", "mycosis fungoides", "sezary", "sezary syndrome"]),
    ("hodgkin",            ["hodgkin lymphoma", "hodgkin's lymphoma", "classical hodgkin", "nodular lymphocyte"]),
    ("cll",                ["chronic lymphocytic leukemia", "cll", "small lymphocytic lymphoma", "sll"]),
    ("aml",                ["acute myeloid leukemia", "aml", "acute myelogenous"]),
    ("all",                ["acute lymphoblastic leukemia", "all", "acute lymphocytic leukemia", "b-all", "t-all"]),
    ("apl",                ["acute promyelocytic leukemia", "apl", "promyelocytic"]),
    ("cml",                ["chronic myeloid leukemia", "cml", "chronic myelogenous", "bcr-abl"]),
    ("cmml",               ["chronic myelomonocytic leukemia", "cmml"]),
    ("hcl",                ["hairy cell leukemia", "hcl"]),
    ("mds",                ["myelodysplastic syndrome", "mds", "myelodysplasia"]),
    ("mpn",                ["myeloproliferative neoplasm", "mpn", "polycythemia vera", "essential thrombocythemia", "myelofibrosis", "jak2"]),
    ("mds_mpn",            ["mds/mpn", "mds-mpn", "overlap syndrome"]),
    ("multiple_myeloma",   ["multiple myeloma", "myeloma", "plasma cell myeloma"]),
    ("smm",                ["smoldering myeloma", "smoldering multiple myeloma", "smm"]),
    ("amyloidosis",        ["amyloidosis", "al amyloid", "systemic amyloid"]),
    ("mgus",               ["mgus", "monoclonal gammopathy"]),
    ("mastocytosis",       ["mastocytosis", "systemic mastocytosis", "mast cell"]),
    ("gbm",                ["glioblastoma", "gbm", "glioblastoma multiforme"]),
    ("glioma",             ["glioma", "astrocytoma", "oligodendroglioma", "idh-mutant"]),
    ("brain_mets",         ["brain metastasis", "brain metastases", "cerebral metastasis"]),
    ("cns_lymphoma",       ["cns lymphoma", "primary cns lymphoma", "pcnsl", "vitreoretinal lymphoma"]),
    ("meningioma",         ["meningioma"]),
    ("medulloblastoma",    ["medulloblastoma"]),
    ("her2_breast",        ["her2-positive breast", "her2+ breast", "her2-amplified breast", "trastuzumab", "pertuzumab", "ado-trastuzumab"]),
    ("tnbc",               ["triple-negative breast", "tnbc", "triple negative breast"]),
    ("hr_breast",          ["hormone receptor", "er-positive breast", "luminal breast", "endocrine therapy breast"]),
    ("breast_general",     ["breast cancer", "breast carcinoma", "breast tumor"]),
    ("crc",                ["colorectal cancer", "colon cancer", "rectal cancer", "colorectal carcinoma", "crc"]),
    ("gastric",            ["gastric cancer", "gastric adenocarcinoma", "stomach cancer", "gastroesophageal junction"]),
    ("esophageal",         ["esophageal cancer", "esophageal carcinoma", "esophageal squamous"]),
    ("pancreatic",         ["pancreatic cancer", "pancreatic adenocarcinoma", "pdac", "pancreatic ductal"]),
    ("hcc",                ["hepatocellular carcinoma", "hcc", "liver cancer", "hepatocellular cancer"]),
    ("biliary",            ["cholangiocarcinoma", "biliary tract", "bile duct cancer", "gallbladder cancer", "intrahepatic cholangiocarcinoma"]),
    ("appendix",           ["appendix cancer", "appendiceal", "pseudomyxoma"]),
    ("anal",               ["anal cancer", "anal carcinoma", "anal squamous"]),
    ("net",                ["neuroendocrine tumor", "neuroendocrine neoplasm", "carcinoid", "net", "pnet"]),
    ("prostate",           ["prostate cancer", "prostate carcinoma", "castration-resistant", "crpc", "enzalutamide", "abiraterone", "darolutamide"]),
    ("rcc",                ["renal cell carcinoma", "rcc", "kidney cancer", "clear cell renal", "papillary renal"]),
    ("bladder",            ["bladder cancer", "urothelial carcinoma", "transitional cell carcinoma", "urothelial"]),
    ("testicular",         ["testicular cancer", "testicular germ cell", "seminoma", "nonseminoma"]),
    ("penile",             ["penile cancer", "penile carcinoma"]),
    ("adrenal",            ["adrenocortical carcinoma", "adrenal cancer", "pheochromocytoma", "paraganglioma"]),
    ("ovarian",            ["ovarian cancer", "ovarian carcinoma", "fallopian tube cancer", "peritoneal cancer"]),
    ("endometrial",        ["endometrial cancer", "endometrial carcinoma", "uterine cancer", "uterine carcinoma"]),
    ("cervical",           ["cervical cancer", "cervical carcinoma"]),
    ("uterine_sarcoma",    ["uterine sarcoma", "leiomyosarcoma uterine", "endometrial stromal sarcoma"]),
    ("vulvar",             ["vulvar cancer", "vulvar carcinoma"]),
    ("gtn",                ["gestational trophoblastic", "choriocarcinoma", "hydatidiform mole"]),
    ("nsclc",              ["non-small cell lung", "nsclc", "lung adenocarcinoma", "lung squamous", "egfr mutation", "alk fusion", "ros1", "kras g12c", "osimertinib", "lorlatinib"]),
    ("sclc",               ["small cell lung", "sclc", "small-cell lung"]),
    ("mesothelioma",       ["mesothelioma", "malignant pleural mesothelioma"]),
    ("thymoma",            ["thymoma", "thymic carcinoma", "thymic epithelial"]),
    ("hnscc",              ["head and neck squamous", "hnscc", "oropharyngeal cancer", "oral cavity cancer", "laryngeal cancer", "hypopharyngeal"]),
    ("nasopharyngeal",     ["nasopharyngeal carcinoma", "nasopharyngeal cancer", "npc"]),
    ("salivary",           ["salivary gland", "parotid cancer", "adenoid cystic"]),
    ("thyroid",            ["thyroid cancer", "papillary thyroid", "follicular thyroid", "medullary thyroid", "anaplastic thyroid"]),
    ("melanoma",           ["melanoma", "cutaneous melanoma", "uveal melanoma", "braf mutation melanoma"]),
    ("cscc",               ["cutaneous squamous", "squamous cell carcinoma skin", "cscc", "cutaneous scc"]),
    ("merkel",             ["merkel cell carcinoma", "merkel cell"]),
    ("bcc",                ["basal cell carcinoma", "basal cell cancer", "bcc", "hedgehog pathway"]),
    ("osteosarcoma",       ["osteosarcoma", "osteogenic sarcoma"]),
    ("ewing",              ["ewing sarcoma", "ewing's sarcoma"]),
    ("gist",               ["gastrointestinal stromal tumor", "gist", "kit mutation", "pdgfra"]),
    ("rms",                ["rhabdomyosarcoma", "rms"]),
    ("lms",                ["leiomyosarcoma"]),
    ("liposarcoma",        ["liposarcoma", "dedifferentiated liposarcoma", "myxoid liposarcoma"]),
    ("sarcoma_general",    ["sarcoma", "soft tissue sarcoma"]),
    ("uveal_melanoma",     ["uveal melanoma", "ocular melanoma", "choroidal melanoma"]),
    ("retinoblastoma",     ["retinoblastoma"]),
    ("cup",                ["cancer of unknown primary", "cup", "unknown primary"]),
]

CATEGORY_MAP = {
    "dlbcl": "heme", "follicular_lymphoma": "heme", "mantle_cell": "heme",
    "marginal_zone": "heme", "burkitt": "heme", "primary_mediastinal": "heme",
    "transformed_fl": "heme", "waldenstrom": "heme", "ptcl": "heme",
    "ctcl": "heme", "hodgkin": "heme", "cll": "heme", "aml": "heme",
    "all": "heme", "apl": "heme", "cml": "heme", "cmml": "heme",
    "hcl": "heme", "mds": "heme", "mpn": "heme", "mds_mpn": "heme",
    "multiple_myeloma": "heme", "smm": "heme", "amyloidosis": "heme",
    "mgus": "heme", "mastocytosis": "heme",
}

def classify_disease(text: str) -> str:
    t = text.lower()
    for disease_id, keywords in NCCN_DISEASE_KEYWORDS:
        if any(kw in t for kw in keywords):
            return disease_id
    return "other"

def classify_category(disease_id: str) -> str:
    return CATEGORY_MAP.get(disease_id, "solid")

TRIAL_KEYWORDS = {
    "phase3": ["phase 3", "phase iii", "phase-3", "phase-iii", "randomized controlled", "randomised controlled"],
    "phase2": ["phase 2", "phase ii", "phase-2", "phase-ii"],
    "review": ["systematic review", "meta-analysis", "meta analysis", "pooled analysis", "network meta"],
}

def classify_type(text: str, source: str) -> str:
    if source == "fda":
        return "fda"
    t = text.lower()
    for type_id, keywords in TRIAL_KEYWORDS.items():
        if any(kw in t for kw in keywords):
            return type_id
    return "review"

# ─── Altmetric Scoring ────────────────────────────────────────────────────────
ALTMETRIC_BASE = "https://api.altmetric.com/v1"

JOURNAL_TIER = {
    "NEJM": 1, "Lancet": 1, "JAMA": 1, "BMJ": 1,
    "Lancet Oncol": 1, "JCO": 1, "Cancer Cell": 1, "Nature Cancer": 1,
    "Cancer Discov": 1, "Ann Oncol": 2, "JAMA Oncol": 2, "Blood": 2,
    "Clin Cancer Res": 2, "NEJM Evidence": 2,
    "J Hematol Oncol": 3, "Leukemia": 3, "BJH": 3,
    "Eur J Cancer": 3, "Blood Cancer J": 3, "FDA": 1,
}

STUDY_TYPE_SCORE = {
    "fda": 100, "phase3": 80, "phase2": 50, "review": 30,
}

def fetch_altmetric_score(pmid: str) -> dict:
    if not pmid:
        return {"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None}
    try:
        r = requests.get(
            f"{ALTMETRIC_BASE}/pmid/{pmid}",
            timeout=10,
            headers={"User-Agent": "OncoPulse/1.0 (oncology literature dashboard)"}
        )
        if r.status_code == 404:
            return {"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None}
        r.raise_for_status()
        data = r.json()
        return {
            "altmetric_score":  round(data.get("score", 0), 1),
            "altmetric_id":     data.get("altmetric_id"),
            "altmetric_url":    data.get("details_url"),
            "cited_by_posts":   data.get("cited_by_posts_count", 0),
            "cited_by_news":    data.get("cited_by_news_count", 0),
            "cited_by_twitter": data.get("cited_by_tweeters_count", 0),
        }
    except Exception as e:
        log.debug("Altmetric fetch failed for PMID %s: %s", pmid, e)
        return {"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None}

def fetch_altmetric_batch(articles: list) -> list:
    pmid_articles = [a for a in articles if a.get("pmid")]
    log.info("Fetching Altmetric scores for %d articles with PMIDs...", len(pmid_articles))
    for i, a in enumerate(pmid_articles):
        scores = fetch_altmetric_score(a["pmid"])
        a.update(scores)
        if i % 10 == 0 and i > 0:
            log.info("  Altmetric: %d/%d done", i, len(pmid_articles))
        time.sleep(1.0)
    for a in articles:
        if "altmetric_score" not in a:
            a.update({"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None})
    log.info("  Altmetric scoring complete")
    return articles

def compute_composite_score(a: dict) -> float:
    if a.get("type") == "fda":
        return 1000.0
    alt = a.get("altmetric_score", 0) or 0
    alt_norm = min(math.log1p(alt) * 15, 150)
    type_score = STUDY_TYPE_SCORE.get(a.get("type", "review"), 30)
    tier = JOURNAL_TIER.get(a.get("journal", ""), 3)
    journal_score = {1: 40, 2: 25, 3: 10}.get(tier, 10)
    return round(alt_norm + type_score + journal_score, 2)

# ─── Helpers ──────────────────────────────────────────────────────────────────
PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

def _xml_text(el, path, default=""):
    import xml.etree.ElementTree as ET
    found = el.find(path)
    return (found.text or "").strip() if found is not None else default

ONCOLOGY_QUERY = (
    "(cancer[tiab] OR tumor[tiab] OR tumour[tiab] OR carcinoma[tiab] OR "
    "lymphoma[tiab] OR leukemia[tiab] OR leukaemia[tiab] OR myeloma[tiab] OR "
    "melanoma[tiab] OR sarcoma[tiab] OR oncology[tiab] OR chemotherapy[tiab] OR "
    "immunotherapy[tiab] OR targeted therapy[tiab] OR car-t[tiab] OR "
    "bispecific[tiab] OR checkpoint inhibitor[tiab]) AND "
    "(clinical trial[pt] OR randomized controlled trial[pt] OR "
    "systematic review[pt] OR meta-analysis[pt] OR "
    "\"phase 2\"[tiab] OR \"phase 3\"[tiab] OR \"phase ii\"[tiab] OR \"phase iii\"[tiab])"
)

def fetch_pubmed(days_back: int, max_ids: int = 500) -> list:
    import xml.etree.ElementTree as ET
    mindate = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    maxdate = date.today().strftime("%Y/%m/%d")
    search_params = {
        "db": "pubmed", "term": ONCOLOGY_QUERY, "retmax": max_ids,
        "mindate": mindate, "maxdate": maxdate, "datetype": "pdat",
        "retmode": "json", "sort": "relevance",
    }
    if PUBMED_API_KEY:
        search_params["api_key"] = PUBMED_API_KEY
    log.info("Searching PubMed (last %d days)...", days_back)
    try:
        r = requests.get(f"{PUBMED_BASE}/esearch.fcgi", params=search_params, timeout=30)
        r.raise_for_status()
        ids = r.json()["esearchresult"]["idlist"]
        log.info("  Found %d PubMed IDs", len(ids))
    except Exception as e:
        log.error("PubMed search failed: %s", e)
        return []
    if not ids:
        return []
    articles = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i+50]
        fetch_params = {
            "db": "pubmed", "id": ",".join(batch),
            "retmode": "xml", "rettype": "abstract",
        }
        if PUBMED_API_KEY:
            fetch_params["api_key"] = PUBMED_API_KEY
        try:
            r = requests.get(f"{PUBMED_BASE}/efetch.fcgi", params=fetch_params, timeout=45)
            r.raise_for_status()
            root = ET.fromstring(r.content)
        except Exception as e:
            log.error("PubMed fetch batch failed: %s", e)
            time.sleep(1)
            continue
        for art in root.findall(".//PubmedArticle"):
            try:
                pmid    = _xml_text(art, ".//PMID")
                if not pmid:
                    continue
                title   = _xml_text(art, ".//ArticleTitle")
                if not title:
                    continue
                abstract_parts = []
                for ab in art.findall(".//AbstractText"):
                    label = ab.get("Label", "")
                    text  = (ab.text or "").strip()
                    if text:
                        abstract_parts.append(f"{label}: {text}" if label else text)
                abstract = " ".join(abstract_parts)
                journal  = _xml_text(art, ".//Journal/Title")
                year     = _xml_text(art, ".//PubDate/Year") or str(date.today().year)
                month    = _xml_text(art, ".//PubDate/Month")
                pub_date = f"{year} {month}".strip()
                uid      = f"pmid_{pmid}"
                full_text = title + " " + abstract
                disease   = classify_disease(full_text)
                articles.append({
                    "uid": uid, "pmid": pmid, "title": title,
                    "abstract": abstract, "journal": journal,
                    "pub_date": pub_date,
                    "type": classify_type(full_text, "pubmed"),
                    "disease": disease,
                    "category": classify_category(disease),
                    "source": "pubmed", "source_link": "",
                    "pubmed_link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
            except Exception as e:
                log.debug("Error parsing PubMed article: %s", e)
        time.sleep(0.4)
    log.info("  Parsed %d PubMed articles", len(articles))
    return articles

# ─── FDA RSS ──────────────────────────────────────────────────────────────────
def fetch_fda(days_back: int) -> list:
    log.info("Fetching FDA approvals...")
    cutoff  = datetime.now() - timedelta(days=days_back)
    articles = []
    fda_rss = "https://www.fda.gov/about-fda/contact-fda/stay-informed/rss-feeds/press-releases/rss.xml"
    try:
        feed = feedparser.parse(fda_rss)
        for entry in feed.entries:
            published = datetime(*entry.published_parsed[:6]) if hasattr(entry, "published_parsed") and entry.published_parsed else datetime.now()
            if published < cutoff:
                continue
            title   = entry.get("title", "").strip()
            summary = entry.get("summary", "").strip()
            link    = entry.get("link", "")
            text    = (title + " " + summary).lower()
            if not any(kw in text for kw in ["cancer","tumor","oncol","lymphoma","leukemia","myeloma","carcinoma","melanoma"]):
                continue
            uid     = f"fda_{hashlib.md5(link.encode()).hexdigest()[:10]}"
            disease = classify_disease(title + " " + summary)
            articles.append({
                "uid": uid, "pmid": "", "title": title,
                "abstract": summary, "journal": "FDA",
                "pub_date": published.strftime("%Y %b"),
                "type": "fda", "disease": disease,
                "category": classify_category(disease),
                "source": "fda", "source_link": link, "pubmed_link": "",
            })
    except Exception as e:
        log.warning("FDA RSS failed: %s", e)
    log.info("  Found %d FDA items", len(articles))
    return articles

# ─── Journal RSS ──────────────────────────────────────────────────────────────
JOURNAL_FEEDS = {
    "NEJM":          "https://www.nejm.org/action/showFeed?jc=nejmoa&type=etoc&feed=rss",
    "Lancet":        "https://www.thelancet.com/rssfeed/lancet_current.xml",
    "JAMA":          "https://jamanetwork.com/rss/site_3/67.xml",
    "BMJ":           "https://www.bmj.com/rss/current.xml",
    "NEJM Evidence": "https://evidence.nejm.org/action/showFeed?type=etoc&feed=rss",
    "Lancet Oncol":  "https://www.thelancet.com/rssfeed/lanonc_current.xml",
    "JCO":           "https://ascopubs.org/action/showFeed?type=etoc&feed=rss&jc=jco",
    "Blood":         "https://ashpublications.org/rss/site_6/1.xml",
    "Cancer Cell":   "https://www.cell.com/cancer-cell/current.rss",
    "Nature Cancer": "https://www.nature.com/natcancer.rss",
    "JAMA Oncol":    "https://jamanetwork.com/rss/site_375/67.xml",
    "Ann Oncol":     "https://www.annalsofoncology.org/rssfeed/ANNONC.xml",
    "Cancer Discov": "https://aacrjournals.org/cancerdiscovery/pages/rss",
    "Clin Cancer Res":"https://aacrjournals.org/clincancerres/pages/rss",
    "J Hematol Oncol":"https://jhoonline.biomedcentral.com/articles/most-recent/rss.xml",
    "Leukemia":      "https://www.nature.com/leu.rss",
    "BJH":           "https://onlinelibrary.wiley.com/action/showFeed?jc=13652141&type=etoc&feed=rss",
    "Eur J Cancer":  "https://www.ejcancer.com/rssfeed/EJC.xml",
    "Blood Cancer J":"https://www.nature.com/bcj.rss",
}

ONCOLOGY_TERMS = [
    "cancer","tumor","carcinoma","oncol","lymphoma","leukemia",
    "myeloma","melanoma","sarcoma","chemotherapy","immunotherapy",
    "checkpoint","car-t","bispecific","antibody-drug","clinical trial",
    "phase","survival","randomized",
]

def fetch_rss(days_back: int) -> list:
    cutoff   = datetime.now() - timedelta(days=days_back)
    articles = []
    for journal_name, feed_url in JOURNAL_FEEDS.items():
        log.info("  Fetching %s...", journal_name)
        try:
            feed = feedparser.parse(feed_url)
            for entry in feed.entries:
                published = datetime(*entry.published_parsed[:6]) if hasattr(entry, "published_parsed") and entry.published_parsed else datetime.now()
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
                uid     = f"rss_{hashlib.md5(link.encode()).hexdigest()[:10]}"
                disease = classify_disease(title + " " + summary)
                articles.append({
                    "uid": uid, "pmid": "", "title": title,
                    "abstract": summary, "journal": journal_name,
                    "pub_date": published.strftime("%Y %b"),
                    "type": classify_type(title + " " + summary, "rss"),
                    "disease": disease,
                    "category": classify_category(disease),
                    "source": "rss", "source_link": link, "pubmed_link": "",
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
    log.info("=== OncoPulse pipeline starting (lookback=%d days, max=%d) ===", args.days, args.max_articles)
    seen_uids = load_seen()
    log.info("Loaded %d previously seen article UIDs", len(seen_uids))

    all_articles = []
    if not args.skip_fda:
        all_articles.extend(fetch_fda(args.days))
    if not args.skip_rss:
        all_articles.extend(fetch_rss(args.days))
    if not args.skip_pubmed:
        all_articles.extend(fetch_pubmed(args.days))

    log.info("Total fetched: %d", len(all_articles))
    all_articles = deduplicate(all_articles)
    log.info("After dedup: %d", len(all_articles))

    new_articles = [a for a in all_articles if a["uid"] not in seen_uids]
    log.info("New (not previously seen): %d", len(new_articles))

    if not args.skip_altmetric:
        new_articles = fetch_altmetric_batch(new_articles)
    else:
        log.info("Skipping Altmetric scoring (--skip-altmetric)")
        for a in new_articles:
            a.update({"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None})

    for a in new_articles:
        a["importance_score"] = compute_composite_score(a)

    new_articles.sort(key=lambda a: a["importance_score"], reverse=True)

    for i, a in enumerate(new_articles):
        a["rank"] = i + 1

    new_articles = new_articles[:args.max_articles]

    for a in new_articles:
        seen_uids.add(a["uid"])
    save_seen(seen_uids)

    stats = {
        "total":          len(new_articles),
        "phase3":         sum(1 for a in new_articles if a["type"] == "phase3"),
        "phase2":         sum(1 for a in new_articles if a["type"] == "phase2"),
        "fda":            sum(1 for a in new_articles if a["type"] == "fda"),
        "review":         sum(1 for a in new_articles if a["type"] == "review"),
        "with_altmetric": sum(1 for a in new_articles if (a.get("altmetric_score") or 0) > 0),
        "top_altmetric":  round(max((a.get("altmetric_score") or 0) for a in new_articles), 1) if new_articles else 0,
    }

    week_label = (
        f"{(date.today() - timedelta(days=args.days)).strftime('%b %-d')}"
        f"-{date.today().strftime('%b %-d, %Y')}"
    )

    output = {
        "generated_at":  datetime.utcnow().isoformat() + "Z",
        "week_label":    week_label,
        "lookback_days": args.days,
        "stats":         stats,
        "articles":      new_articles,
    }

    out_path = OUTPUT_DIR / "data.json"
    out_path.write_text(json.dumps(output, indent=2, ensure_ascii=False))
    log.info("Wrote %s (%d articles, %d KB)", out_path, len(new_articles), out_path.stat().st_size // 1024)
    log.info("=== Done ===")

# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OncoPulse pipeline")
    parser.add_argument("--days",           type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--max-articles",   type=int, default=MAX_ARTICLES)
    parser.add_argument("--skip-pubmed",    action="store_true")
    parser.add_argument("--skip-fda",       action="store_true")
    parser.add_argument("--skip-rss",       action="store_true")
    parser.add_argument("--skip-altmetric", action="store_true",
                        help="Skip Altmetric API calls (faster, for testing)")
    args = parser.parse_args()
    run_pipeline(args)
