"""
OncoPulse weekly digest emailer.

Reads dashboard/data.json (already fetched, classified and ranked by
fetch_and_summarize.py), pulls active subscribers from Supabase, and emails
each one their top-N articles for their chosen disease sites.

Env vars (all required except RESEND_FROM):
  SUPABASE_URL                Project URL, e.g. https://xxxx.supabase.co
  SUPABASE_SERVICE_ROLE_KEY   Service-role key (server-side only — never
                               put this in dashboard/preferences.html)
  RESEND_API_KEY              Resend API key
  RESEND_FROM                 Sender address, e.g. "OncoPulse <digest@yourdomain.com>"
  SITE_URL                    Base URL where dashboard/preferences.html is
                               hosted, e.g. https://sstaal47-arch.github.io/OncoPulse
  DATA_JSON                   Path to data.json (default: ./dashboard/data.json)
  DISEASE_GROUPS_JSON         Path to disease_groups.json
                               (default: ./pipeline/disease_groups.json)
"""
import json
import logging
import os
import sys
import time
from pathlib import Path
from xml.etree import ElementTree

import requests

log = logging.getLogger("send_digests")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]
RESEND_API_KEY = os.environ["RESEND_API_KEY"]
RESEND_FROM = os.environ.get("RESEND_FROM", "OncoPulse <digest@oncopulse.app>")
SITE_URL = os.environ["SITE_URL"].rstrip("/")
DATA_JSON = Path(os.environ.get("DATA_JSON", "./dashboard/data.json"))
DISEASE_GROUPS_JSON = Path(os.environ.get("DISEASE_GROUPS_JSON", "./pipeline/disease_groups.json"))

PMC_IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"


def load_disease_groups() -> dict:
    """group_id -> set of leaf NCCN disease ids."""
    data = json.loads(DISEASE_GROUPS_JSON.read_text())
    return {g["id"]: set(g["ids"]) for g in data["groups"]}


def load_articles() -> list[dict]:
    data = json.loads(DATA_JSON.read_text())
    return data["articles"], data.get("week_label", "")


def fetch_subscribers() -> list[dict]:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/subscribers",
        params={"is_active": "eq.true", "select": "id,email,disease_sites,top_n,unsubscribe_token"},
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def pmids_to_pmc_oa_links(pmids: list[str]) -> dict[str, str]:
    """pmid -> free full-text PMC article URL, for whichever pmids have one.

    Articles without a PMCID (most paywalled journal content) are simply
    absent from the returned dict — callers must fall back to the PubMed
    abstract page or publisher link for those.
    """
    links: dict[str, str] = {}
    pmids = [p for p in pmids if p]
    for i in range(0, len(pmids), 200):  # idconv batch limit
        batch = pmids[i : i + 200]
        try:
            resp = requests.get(
                PMC_IDCONV_URL,
                params={"ids": ",".join(batch), "idtype": "pmid"},
                timeout=30,
            )
            resp.raise_for_status()
            root = ElementTree.fromstring(resp.content)
            for record in root.findall("record"):
                pmcid = record.get("pmcid")
                pmid = record.get("pmid") or record.get("requested-id")
                if pmcid and pmid:
                    links[pmid] = f"https://www.ncbi.nlm.nih.gov/pmc/articles/{pmcid}/"
        except requests.RequestException as exc:
            log.warning("PMC id-conv lookup failed for batch starting %s: %s", batch[0], exc)
        time.sleep(0.34)  # NCBI: max ~3 req/sec unsynchronous
    return links


def resolve_download_link(article: dict, pmc_links: dict[str, str]) -> tuple[str, str]:
    """Returns (url, label) for the article's action link.

    Prefers a free PMC full-text page (downloadable PDF from there) over the
    PubMed abstract page or the original publisher/FDA link, which may sit
    behind a subscription paywall.
    """
    pmid = article.get("pmid")
    if pmid and pmid in pmc_links:
        return pmc_links[pmid], "Download full text (PMC, free)"
    if article.get("pubmed_link"):
        return article["pubmed_link"], "View on PubMed"
    if article.get("source_link"):
        return article["source_link"], "View source"
    return "", ""


def top_articles_for(subscriber: dict, articles: list[dict], group_leaf_ids: dict) -> list[dict]:
    selected = subscriber.get("disease_sites") or []
    leaf_ids: set[str] = set()
    for group_id in selected:
        leaf_ids |= group_leaf_ids.get(group_id, set())
    if not leaf_ids:
        return []
    top_n = subscriber.get("top_n") or 5
    # articles is already sorted by descending importance_score
    matches = [a for a in articles if a.get("disease") in leaf_ids]
    return matches[:top_n]


def render_email_html(subscriber: dict, week_label: str, picks: list[dict], pmc_links: dict) -> str:
    manage_url = f"{SITE_URL}/preferences.html?token={subscriber['unsubscribe_token']}"
    unsub_url = f"{manage_url}&action=unsubscribe"

    cards = []
    for a in picks:
        url, label = resolve_download_link(a, pmc_links)
        link_html = f'<a href="{url}" style="color:#1d6fd6;text-decoration:none;font-weight:600;">{label} &rarr;</a>' if url else ""
        cards.append(f"""
        <tr><td style="padding:18px 0;border-bottom:1px solid #e3e8f0;">
          <div style="font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:#6b7a99;margin-bottom:6px;">
            {a.get('journal','')} &middot; {a.get('pub_date','')} &middot; {a.get('type','').upper()}
          </div>
          <div style="font-size:17px;font-weight:700;color:#0f1829;line-height:1.35;margin-bottom:8px;">
            {a.get('title','')}
          </div>
          <div style="font-size:14px;color:#374766;line-height:1.5;margin-bottom:10px;">
            {(a.get('abstract') or '')[:600]}
          </div>
          {link_html}
        </td></tr>""")

    return f"""\
<!doctype html>
<html>
<body style="margin:0;padding:0;background:#f4f6fb;font-family:-apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif;">
  <table width="100%" cellpadding="0" cellspacing="0"><tr><td align="center">
    <table width="600" cellpadding="0" cellspacing="0" style="background:#ffffff;margin:24px 0;border-radius:10px;overflow:hidden;">
      <tr><td style="background:#0f1829;padding:24px 28px;">
        <div style="font-size:20px;font-weight:800;color:#4d9fff;">OncoPulse</div>
        <div style="font-size:13px;color:#8ea8cc;margin-top:4px;">Your digest for {week_label}</div>
      </td></tr>
      <tr><td style="padding:8px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0">{''.join(cards)}</table>
      </td></tr>
      <tr><td style="padding:20px 28px;font-size:12px;color:#8a93a6;">
        Sent because {subscriber['email']} is subscribed to OncoPulse digests for:
        {', '.join(subscriber.get('disease_sites') or [])}.
        <a href="{manage_url}" style="color:#1d6fd6;">Manage preferences</a>
        &middot;
        <a href="{unsub_url}" style="color:#1d6fd6;">Unsubscribe</a>
      </td></tr>
    </table>
  </td></tr></table>
</body>
</html>"""


def send_email(to_email: str, subject: str, html: str) -> None:
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": RESEND_FROM, "to": [to_email], "subject": subject, "html": html},
        timeout=30,
    )
    if resp.status_code >= 300:
        log.error("Resend failed for %s: %s %s", to_email, resp.status_code, resp.text)
        resp.raise_for_status()


def main() -> None:
    articles, week_label = load_articles()
    group_leaf_ids = load_disease_groups()
    subscribers = fetch_subscribers()
    log.info("Loaded %d articles, %d active subscribers", len(articles), len(subscribers))

    all_pmids = [a.get("pmid") for a in articles if a.get("pmid")]
    pmc_links = pmids_to_pmc_oa_links(all_pmids)
    log.info("Resolved %d free PMC full-text links out of %d PubMed articles", len(pmc_links), len(all_pmids))

    sent, skipped = 0, 0
    for sub in subscribers:
        picks = top_articles_for(sub, articles, group_leaf_ids)
        if not picks:
            skipped += 1
            continue
        html = render_email_html(sub, week_label, picks, pmc_links)
        try:
            send_email(sub["email"], f"OncoPulse Digest — {week_label}", html)
            sent += 1
        except requests.RequestException:
            log.error("Failed sending to %s", sub["email"])
        time.sleep(0.5)  # stay well under Resend's rate limit

    log.info("Digest run complete: %d sent, %d skipped (no matching articles)", sent, skipped)


if __name__ == "__main__":
    try:
        main()
    except KeyError as exc:
        log.error("Missing required env var: %s", exc)
        sys.exit(1)
