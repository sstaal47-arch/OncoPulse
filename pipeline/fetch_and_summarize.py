#!/usr/bin/env python3
"""
OncoPulse — Weekly Oncology Literature Pipeline
Fetches up to 300 articles from PubMed, FDA, and journal RSS feeds.
Classifies articles using full NCCN disease taxonomy.
Skips any article already seen in previous runs.
Outputs data.json with title, abstract, pubmed link, and source link.
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

# No seen-articles cache — pipeline always fetches the most recent 7 days fresh each run

# ─── NCCN Disease Taxonomy ────────────────────────────────────────────────────
# Each entry: (nccn_id, keywords...)
# Order matters — more specific entries should come before broader ones.
NCCN_DISEASE_KEYWORDS = [
    # ── Hematologic: B-Cell Lymphomas ──
    ("dlbcl",           ["diffuse large b-cell", "dlbcl", "large b-cell lymphoma"]),
    ("follicular_lymphoma", ["follicular lymphoma", "follicular grade"]),
    ("mantle_cell",     ["mantle cell lymphoma", "mcl"]),
    ("marginal_zone",   ["marginal zone lymphoma", "malt lymphoma", "mzl", "splenic marginal"]),
    ("burkitt",         ["burkitt lymphoma", "burkitt's lymphoma"]),
    ("primary_mediastinal", ["primary mediastinal", "pmbcl"]),
    ("transformed_fl",  ["transformed follicular", "richter transformation", "richter's syndrome"]),
    ("waldenstrom",     ["waldenstrom", "lymphoplasmacytic lymphoma", "waldenström"]),
    # ── Hematologic: T-Cell Lymphomas ──
    ("ptcl",            ["peripheral t-cell lymphoma", "ptcl", "angioimmunoblastic", "aitl",
                         "anaplastic large cell", "alcl"]),
    ("ctcl",            ["cutaneous t-cell lymphoma", "ctcl", "mycosis fungoides", "sezary",
                         "sézary"]),
    # ── Hematologic: Hodgkin ──
    ("hodgkin",         ["hodgkin lymphoma", "hodgkin's lymphoma", "classical hodgkin",
                         "nodular lymphocyte", "nlphl"]),
    # ── Hematologic: CLL ──
    ("cll",             ["chronic lymphocytic leukemia", "cll", "small lymphocytic lymphoma",
                         "sll", "cll/sll"]),
    # ── Hematologic: Acute Leukemias ──
    ("aml",             ["acute myeloid leukemia", "aml", "acute myelogenous"]),
    ("all",             ["acute lymphoblastic leukemia", "acute lymphocytic leukemia", "all ",
                         "b-all", "t-all", "philadelphia chromosome-positive all"]),
    ("apl",             ["acute promyelocytic leukemia", "apl", "pml-rara"]),
    # ── Hematologic: Chronic/Other Leukemias ──
    ("cml",             ["chronic myeloid leukemia", "cml", "chronic myelogenous leukemia",
                         "bcr-abl", "bcr::abl"]),
    ("cmml",            ["chronic myelomonocytic leukemia", "cmml"]),
    ("hcl",             ["hairy cell leukemia", "hcl"]),
    # ── Hematologic: MDS ──
    ("mds",             ["myelodysplastic syndrome", "mds", "myelodysplasia",
                         "refractory anemia", "excess blasts"]),
    # ── Hematologic: MPN ──
    ("mpn",             ["myeloproliferative neoplasm", "mpn", "polycythemia vera",
                         "essential thrombocythemia", "primary myelofibrosis", "myelofibrosis",
                         "jak2", "calr mutation"]),
    # ── Hematologic: Plasma Cell ──
    ("multiple_myeloma",["multiple myeloma", "myeloma"]),
    ("smoldering_myeloma", ["smoldering myeloma", "smoldering multiple myeloma", "smm"]),
    ("amyloidosis",     ["amyloidosis", "al amyloid", "systemic amyloid"]),
    ("mgus",            ["mgus", "monoclonal gammopathy"]),
    # ── Hematologic: Other ──
    ("mastocytosis",    ["mastocytosis", "systemic mastocytosis", "kit d816"]),
    ("mds_mpn",         ["mds/mpn", "mds-mpn", "overlap syndrome"]),
    # ── CNS ──
    ("glioblastoma",    ["glioblastoma", "gbm", "glioblastoma multiforme"]),
    ("glioma",          ["glioma", "astrocytoma", "oligodendroglioma", "low-grade glioma",
                         "high-grade glioma", "idh-mutant"]),
    ("brain_mets",      ["brain metastasis", "brain metastases", "cerebral metastasis"]),
    ("cns_lymphoma",    ["cns lymphoma", "primary cns lymphoma", "pcnsl", "vitreoretinal lymphoma"]),
    ("meningioma",      ["meningioma"]),
    ("medulloblastoma", ["medulloblastoma", "primitive neuroectodermal"]),
    # ── Breast ──
    ("breast_her2",     ["her2-positive breast", "her2+ breast", "her2-amplified breast",
                         "trastuzumab", "pertuzumab", "ado-trastuzumab"]),
    ("breast_tnbc",     ["triple negative breast", "tnbc", "triple-negative breast"]),
    ("breast_hr",       ["hormone receptor-positive breast", "er-positive breast", "luminal",
                         "aromatase inhibitor", "fulvestrant", "cdk4/6"]),
    ("breast_general",  ["breast cancer", "breast carcinoma", "breast tumor", "mammary"]),
    # ── GI Tumors ──
    ("colorectal",      ["colorectal cancer", "colon cancer", "rectal cancer", "colorectal carcinoma",
                         "crc", "mismatch repair", "microsatellite instability", "msi-h colorectal"]),
    ("gastric",         ["gastric cancer", "stomach cancer", "gastric adenocarcinoma",
                         "gastroesophageal junction", "gej"]),
    ("esophageal",      ["esophageal cancer", "esophageal carcinoma", "esophageal adenocarcinoma",
                         "esophageal squamous"]),
    ("pancreatic",      ["pancreatic cancer", "pancreatic ductal adenocarcinoma", "pdac",
                         "pancreatic adenocarcinoma"]),
    ("hcc",             ["hepatocellular carcinoma", "hcc", "liver cancer", "hepatoma"]),
    ("biliary",         ["cholangiocarcinoma", "biliary tract cancer", "gallbladder cancer",
                         "intrahepatic cholangiocarcinoma", "extrahepatic cholangiocarcinoma",
                         "ihcca", "fgfr2"]),
    ("appendix",        ["appendiceal cancer", "appendix cancer", "pseudomyxoma peritonei"]),
    ("anal",            ["anal cancer", "anal carcinoma", "anal squamous"]),
    ("neuroendocrine",  ["neuroendocrine tumor", "neuroendocrine carcinoma", "net", "nec",
                         "carcinoid", "pnet", "somatostatin"]),
    # ── GU Tumors ──
    ("prostate",        ["prostate cancer", "castration-resistant prostate", "crpc",
                         "metastatic prostate", "psa", "enzalutamide", "abiraterone",
                         "darolutamide", "androgen deprivation"]),
    ("rcc",             ["renal cell carcinoma", "rcc", "kidney cancer", "clear cell renal",
                         "papillary renal"]),
    ("bladder",         ["bladder cancer", "urothelial carcinoma", "transitional cell",
                         "muscle-invasive bladder"]),
    ("penile",          ["penile cancer", "penile carcinoma"]),
    ("testicular",      ["testicular cancer", "testicular germ cell", "seminoma",
                         "nonseminomatous"]),
    ("adrenal",         ["adrenocortical carcinoma", "adrenal cancer", "pheochromocytoma",
                         "paraganglioma"]),
    # ── Gynecologic ──
    ("ovarian",         ["ovarian cancer", "ovarian carcinoma", "fallopian tube cancer",
                         "peritoneal cancer", "brca ovarian", "parp inhibitor ovarian"]),
    ("endometrial",     ["endometrial cancer", "endometrial carcinoma", "uterine cancer",
                         "endometrioid"]),
    ("cervical",        ["cervical cancer", "cervical carcinoma", "squamous cervical"]),
    ("uterine_sarcoma", ["uterine sarcoma", "leiomyosarcoma uterine", "carcinosarcoma"]),
    ("vulvar",          ["vulvar cancer", "vulvar carcinoma"]),
    ("gestational",     ["gestational trophoblastic", "choriocarcinoma", "molar pregnancy"]),
    # ── Thoracic ──
    ("nsclc",           ["non-small cell lung", "nsclc", "lung adenocarcinoma",
                         "lung squamous cell", "egfr-mutant", "alk-positive", "ros1",
                         "kras g12c", "met exon 14", "ret fusion lung"]),
    ("sclc",            ["small cell lung", "sclc", "extensive-stage small cell"]),
    ("mesothelioma",    ["mesothelioma", "malignant pleural mesothelioma", "mpm"]),
    ("thymoma",         ["thymoma", "thymic carcinoma", "thymic epithelial"]),
    # ── Head & Neck ──
    ("hnscc",           ["head and neck squamous", "hnscc", "oropharyngeal cancer",
                         "oral cavity cancer", "laryngeal cancer", "hypopharyngeal"]),
    ("nasopharyngeal",  ["nasopharyngeal carcinoma", "npc", "nasopharyngeal cancer"]),
    ("salivary",        ["salivary gland cancer", "parotid cancer", "adenoid cystic"]),
    ("thyroid",         ["thyroid cancer", "papillary thyroid", "follicular thyroid",
                         "medullary thyroid", "anaplastic thyroid", "differentiated thyroid"]),
    # ── Skin ──
    ("melanoma",        ["melanoma", "cutaneous melanoma", "uveal melanoma", "acral melanoma",
                         "braf-mutant melanoma"]),
    ("cscc",            ["cutaneous squamous cell carcinoma", "cscc", "cutaneous scc",
                         "skin squamous", "cemiplimab"]),
    ("merkel",          ["merkel cell carcinoma", "merkel cell cancer", "mcc"]),
    ("bcc",             ["basal cell carcinoma", "bcc", "basal cell cancer", "hedgehog pathway"]),
    # ── Bone & Soft Tissue ──
    ("osteosarcoma",    ["osteosarcoma", "osteogenic sarcoma"]),
    ("ewing",           ["ewing sarcoma", "ewing's sarcoma", "ewsr1"]),
    ("gist",            ["gastrointestinal stromal tumor", "gist", "kit mutation gist",
                         "pdgfra mutation"]),
    ("rhabdomyosarcoma",["rhabdomyosarcoma", "rms"]),
    ("leiomyosarcoma",  ["leiomyosarcoma", "lms"]),
    ("liposarcoma",     ["liposarcoma", "well-differentiated liposarcoma", "dedifferentiated liposarcoma"]),
    ("sarcoma_general", ["sarcoma", "soft tissue sarcoma", "undifferentiated pleomorphic sarcoma",
                         "angiosarcoma", "synovial sarcoma", "solitary fibrous tumor"]),
    # ── Endocrine ──
    ("thyroid_general", ["thyroid"]),  # catch-all after specific thyroid subtypes above
    # ── Eye ──
    ("uveal_melanoma",  ["uveal melanoma", "choroidal melanoma", "ocular melanoma"]),
    ("retinoblastoma",  ["retinoblastoma"]),
    # ── Other / Unknown Primary ──
    ("cup",             ["cancer of unknown primary", "cup", "unknown primary"]),
    ("other",           []),
]

# Broad parent mapping for broad-mode display
BROAD_PARENT = {
    "dlbcl": "lymphoma", "follicular_lymphoma": "lymphoma", "mantle_cell": "lymphoma",
    "marginal_zone": "lymphoma", "burkitt": "lymphoma", "primary_mediastinal": "lymphoma",
    "transformed_fl": "lymphoma", "waldenstrom": "lymphoma", "ptcl": "lymphoma",
    "ctcl": "lymphoma", "hodgkin": "lymphoma",
    "cll": "leukemia", "aml": "leukemia", "all": "leukemia", "apl": "leukemia",
    "cml": "leukemia", "cmml": "leukemia", "hcl": "leukemia",
    "mds": "leukemia", "mpn": "leukemia", "mds_mpn": "leukemia",
    "multiple_myeloma": "myeloma", "smoldering_myeloma": "myeloma",
    "amyloidosis": "myeloma", "mgus": "myeloma", "mastocytosis": "myeloma",
    "glioblastoma": "cns", "glioma": "cns", "brain_mets": "cns",
    "cns_lymphoma": "cns", "meningioma": "cns", "medulloblastoma": "cns",
    "breast_her2": "breast", "breast_tnbc": "breast", "breast_hr": "breast",
    "breast_general": "breast",
    "colorectal": "gi", "gastric": "gi", "esophageal": "gi", "pancreatic": "gi",
    "hcc": "gi", "biliary": "gi", "appendix": "gi", "anal": "gi",
    "neuroendocrine": "gi",
    "prostate": "gu", "rcc": "gu", "bladder": "gu", "penile": "gu",
    "testicular": "gu", "adrenal": "gu",
    "ovarian": "gynecologic", "endometrial": "gynecologic", "cervical": "gynecologic",
    "uterine_sarcoma": "gynecologic", "vulvar": "gynecologic", "gestational": "gynecologic",
    "nsclc": "lung", "sclc": "lung", "mesothelioma": "lung", "thymoma": "lung",
    "hnscc": "head_neck", "nasopharyngeal": "head_neck", "salivary": "head_neck",
    "thyroid": "head_neck", "thyroid_general": "head_neck",
    "melanoma": "skin", "cscc": "skin", "merkel": "skin", "bcc": "skin",
    "osteosarcoma": "sarcoma", "ewing": "sarcoma", "gist": "sarcoma",
    "rhabdomyosarcoma": "sarcoma", "leiomyosarcoma": "sarcoma",
    "liposarcoma": "sarcoma", "sarcoma_general": "sarcoma",
    "uveal_melanoma": "skin", "retinoblastoma": "other",
    "cup": "other", "other": "other",
}

HEME_DISEASES = {
    "dlbcl", "follicular_lymphoma", "mantle_cell", "marginal_zone", "burkitt",
    "primary_mediastinal", "transformed_fl", "waldenstrom", "ptcl", "ctcl",
    "hodgkin", "cll", "aml", "all", "apl", "cml", "cmml", "hcl",
    "mds", "mpn", "mds_mpn", "multiple_myeloma", "smoldering_myeloma",
    "amyloidosis", "mgus", "mastocytosis",
}

def classify_disease(text: str) -> str:
    """Returns the most specific NCCN disease ID matching the text."""
    t = text.lower()
    for disease_id, keywords in NCCN_DISEASE_KEYWORDS:
        if disease_id == "other":
            continue
        if any(kw in t for kw in keywords):
            return disease_id
    return "other"

def classify_category(disease: str) -> str:
    return "heme" if disease in HEME_DISEASES else "solid"

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
import xml.etree.ElementTree as ET

PUBMED_BASE = "https://eutils.ncbi.nlm.nih.gov/entrez/eutils"

ONCOLOGY_QUERY = (
    "(cancer[tiab] OR oncol*[tiab] OR tumor[tiab] OR carcinoma[tiab]"
    " OR lymphoma[tiab] OR leukemia[tiab] OR myeloma[tiab] OR sarcoma[tiab]"
    " OR melanoma[tiab] OR glioblastoma[tiab])"
    " AND"
    " (randomized controlled trial[pt] OR clinical trial[pt] OR meta-analysis[pt]"
    " OR phase 2[tiab] OR phase 3[tiab] OR phase II[tiab] OR phase III[tiab])"
    " AND humans[mesh]"
)

def _xml_text(el, path: str, default: str = "") -> str:
    """Safely extract text from an XML element by tag path."""
    found = el.find(path)
    return (found.text or "").strip() if found is not None else default

def fetch_pubmed(days_back: int, max_ids: int = 500) -> list:
    mindate = (date.today() - timedelta(days=days_back)).strftime("%Y/%m/%d")
    maxdate = date.today().strftime("%Y/%m/%d")

    # Step 1: search for IDs
    search_params = {
        "db": "pubmed",
        "term": ONCOLOGY_QUERY,
        "retmax": max_ids,
        "mindate": mindate,
        "maxdate": maxdate,
        "datetype": "pdat",
        "retmode": "json",
        "sort": "relevance",
    }
    if PUBMED_API_KEY:
        search_params["api_key"] = PUBMED_API_KEY

    log.info("Searching PubMed (last %d days)…", days_back)
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

    # Step 2: fetch abstracts in batches using XML (reliable)
    articles = []
    for i in range(0, len(ids), 50):
        batch = ids[i:i + 50]
        fetch_params = {
            "db": "pubmed",
            "id": ",".join(batch),
            "retmode": "xml",
            "rettype": "abstract",
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
                pmid = _xml_text(art, ".//PMID")
                if not pmid:
                    continue

                title = _xml_text(art, ".//ArticleTitle")
                if not title:
                    continue

                # Abstract — may have multiple structured sections
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

                uid = f"pmid_{pmid}"
                full_text = title + " " + abstract
                disease   = classify_disease(full_text)

                articles.append({
                    "uid":         uid,
                    "pmid":        pmid,
                    "title":       title,
                    "abstract":    abstract,
                    "journal":     journal,
                    "pub_date":    pub_date,
                    "type":        classify_type(full_text, "pubmed"),
                    "disease":     disease,
                    "category":    classify_category(disease),
                    "source":      "pubmed",
                    "source_link": "",
                    "pubmed_link": f"https://pubmed.ncbi.nlm.nih.gov/{pmid}/",
                })
            except Exception as e:
                log.debug("Error parsing PubMed article: %s", e)

        time.sleep(0.4)  # NCBI rate limit

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
    # ── General Medical ──
    "NEJM":           "https://www.nejm.org/action/showFeed?jc=nejmoa&type=etoc&feed=rss",
    "Lancet":         "https://www.thelancet.com/rssfeed/lancet_current.xml",
    "JAMA":           "https://jamanetwork.com/rss/site_3/67.xml",
    "BMJ":            "https://www.bmj.com/rss/current.xml",
    "NEJM Evidence":  "https://evidence.nejm.org/action/showFeed?type=etoc&feed=rss",
    # ── Oncology Specialist ──
    "Lancet Oncol":   "https://www.thelancet.com/rssfeed/lanonc_current.xml",
    "JCO":            "https://ascopubs.org/action/showFeed?type=etoc&feed=rss&jc=jco",
    "Blood":          "https://ashpublications.org/rss/site_6/1.xml",
    "Cancer Cell":    "https://www.cell.com/cancer-cell/current.rss",
    "Nature Cancer":  "https://www.nature.com/natcancer.rss",
    "JAMA Oncol":     "https://jamanetwork.com/rss/site_375/67.xml",
    "Ann Oncol":      "https://www.annalsofoncology.org/rssfeed/ANNONC.xml",
    "Cancer Discov":  "https://aacrjournals.org/cancerdiscovery/pages/rss",
    "Clin Cancer Res":"https://aacrjournals.org/clincancerres/pages/rss",
    "J Hematol Oncol":"https://jhoonline.biomedcentral.com/articles/most-recent/rss.xml",
    "Leukemia":       "https://www.nature.com/leu.rss",
    "BJH":            "https://onlinelibrary.wiley.com/action/showFeed?jc=13652141&type=etoc&feed=rss",
    "Eur J Cancer":   "https://www.ejcancer.com/rssfeed/EJC.xml",
    "Blood Cancer J": "https://www.nature.com/bcj.rss",
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

# ─── Altmetric Scoring ────────────────────────────────────────────────────────
# Free API — no key required. Rate limit: ~1 req/sec.
ALTMETRIC_BASE = "https://api.altmetric.com/v1"

# Journal prestige tier (1=top, 2=high, 3=specialist)
JOURNAL_TIER = {
    "NEJM": 1, "Lancet": 1, "JAMA": 1, "BMJ": 1,
    "Lancet Oncol": 1, "JCO": 1, "Cancer Cell": 1, "Nature Cancer": 1,
    "Cancer Discov": 1, "Ann Oncol": 2, "JAMA Oncol": 2, "Blood": 2,
    "Clin Cancer Res": 2, "NEJM Evidence": 2,
    "J Hematol Oncol": 3, "Leukemia": 3, "BJH": 3,
    "Eur J Cancer": 3, "Blood Cancer J": 3, "FDA": 1,
}

STUDY_TYPE_SCORE = {
    "fda":    100,
    "phase3": 80,
    "phase2": 50,
    "review": 30,
}

def fetch_altmetric_score(pmid: str) -> dict:
    """Fetch Altmetric data for a given PMID. Returns dict with score and details."""
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
            "altmetric_score": round(data.get("score", 0), 1),
            "altmetric_id":    data.get("altmetric_id"),
            "altmetric_url":   data.get("details_url"),
            "cited_by_posts":  data.get("cited_by_posts_count", 0),
            "cited_by_news":   data.get("cited_by_news_count", 0),
            "cited_by_twitter":data.get("cited_by_tweeters_count", 0),
        }
    except Exception as e:
        log.debug("Altmetric fetch failed for PMID %s: %s", pmid, e)
        return {"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None}

def fetch_altmetric_batch(articles: list) -> list:
    """Fetch Altmetric scores for all articles with PMIDs. Rate-limited to 1 req/sec."""
    pmid_articles = [a for a in articles if a.get("pmid")]
    log.info("Fetching Altmetric scores for %d articles with PMIDs…", len(pmid_articles))

    for i, a in enumerate(pmid_articles):
        scores = fetch_altmetric_score(a["pmid"])
        a.update(scores)
        if i % 10 == 0 and i > 0:
            log.info("  Altmetric: %d/%d done", i, len(pmid_articles))
        time.sleep(1.0)  # Respect free tier rate limit

    # Articles without PMIDs get zero Altmetric score
    for a in articles:
        if "altmetric_score" not in a:
            a.update({"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None})

    log.info("  Altmetric scoring complete")
    return articles

def compute_composite_score(a: dict) -> float:
    """
    Composite importance score combining:
    - Altmetric score (0–∞, normalized, weight 50%)
    - Study type tier (weight 25%)
    - Journal prestige tier (weight 25%)
    FDA approvals always score maximum.
    """
    if a.get("type") == "fda":
        return 1000.0

    # Altmetric — log-scale to prevent single viral articles dominating
    alt = a.get("altmetric_score", 0) or 0
    alt_norm = min(math.log1p(alt) * 15, 150)  # caps at ~150 for score of ~20000

    # Study type
    type_score = STUDY_TYPE_SCORE.get(a.get("type", "review"), 30)

    # Journal tier (1=best)
    tier = JOURNAL_TIER.get(a.get("journal", ""), 3)
    journal_score = {1: 40, 2: 25, 3: 10}.get(tier, 10)

    return round(alt_norm + type_score + journal_score, 2)


def run_pipeline(args):
    log.info("═══ OncoPulse pipeline starting (lookback=%d days, max=%d) ═══",
             args.days, args.max_articles)



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
    # Filter out correspondence, letters, comments, editorials
    EXCLUDE_KEYWORDS = [
        "correspondence", "letter to the editor", "letter:", "in reply",
        "reply to", "comment on", "commentary on", "author's reply",
        "authors' reply", "to the editor", "erratum", "correction to",
        "retraction", "expression of concern",
    ]
    def is_excluded(a):
        t = (a.get("title","") + " " + a.get("abstract","")).lower()
        return any(kw in t for kw in EXCLUDE_KEYWORDS)

    new_articles = [a for a in all_articles if not is_excluded(a)]
    excluded = len(all_articles) - len(new_articles)
    if excluded:
        log.info("Excluded %d correspondence/letters/corrections", excluded)
    log.info("Articles to process: %d", len(new_articles))

    # ── Altmetric scoring ──
    if not args.skip_altmetric:
        new_articles = fetch_altmetric_batch(new_articles)
    else:
        log.info("Skipping Altmetric scoring (--skip-altmetric)")
        for a in new_articles:
            a.update({"altmetric_score": 0, "altmetric_id": None, "altmetric_url": None})

    # ── Composite score and rank ──
    for a in new_articles:
        a["importance_score"] = compute_composite_score(a)

    new_articles.sort(key=lambda a: a["importance_score"], reverse=True)

    for i, a in enumerate(new_articles):
        a["rank"] = i + 1

    log.info("Articles ranked by composite importance score")

    # Cap at max
    new_articles = new_articles[:args.max_articles]

    # Mark all as seen


    # Build stats
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
        f"{(date.today() - timedelta(days=args.days)).strftime('%b %-d')}–"
        f"{date.today().strftime('%b %-d, %Y')}"
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
    log.info("Wrote %s (%d articles, %d KB)",
             out_path, len(new_articles), out_path.stat().st_size // 1024)
    log.info("═══ Done ═══")

# ─── CLI ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="OncoPulse pipeline")
    parser.add_argument("--days",            type=int, default=LOOKBACK_DAYS)
    parser.add_argument("--max-articles",    type=int, default=MAX_ARTICLES)
    parser.add_argument("--skip-pubmed",     action="store_true")
    parser.add_argument("--skip-fda",        action="store_true")
    parser.add_argument("--skip-rss",        action="store_true")
    parser.add_argument("--skip-altmetric",  action="store_true",
                        help="Skip Altmetric API calls (faster, for testing)")
    args = parser.parse_args()
    run_pipeline(args)

