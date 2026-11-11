"""
OncoPulse weekly digest emailer.

Reads dashboard/data.json (already fetched, classified and ranked by
fetch_and_summarize.py), pulls active subscribers from Supabase, and emails
each one a separate email per chosen disease site, holding that site's
top-N articles (N is set per site on the preferences page).

Exits non-zero if any email fails to send, so a broken Resend/Supabase setup
shows up as a red step in GitHub Actions instead of silently "succeeding".

Usage:
  python send_digests.py                       # normal weekly send
  python send_digests.py --dry-run             # build emails, write HTML to
                                               # ./digest_preview/, send nothing
                                               # (no secrets needed)
  python send_digests.py --test-to me@x.org    # send every site's email to one
                                               # address, ignoring subscribers

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
import argparse
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

SUPABASE_URL = os.environ.get("SUPABASE_URL", "").rstrip("/")
SUPABASE_SERVICE_ROLE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY", "")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
RESEND_FROM = os.environ.get("RESEND_FROM", "") or "OncoPulse <digest@oncopulse.app>"
SITE_URL = os.environ.get("SITE_URL", "").rstrip("/") or "https://example.invalid"
DEFAULT_TOP_N = 5
DATA_JSON = Path(os.environ.get("DATA_JSON", "./dashboard/data.json"))
DISEASE_GROUPS_JSON = Path(os.environ.get("DISEASE_GROUPS_JSON", "./pipeline/disease_groups.json"))

PMC_IDCONV_URL = "https://www.ncbi.nlm.nih.gov/pmc/utils/idconv/v1.0/"


def require_env(*names: str) -> None:
    missing = [n for n in names if not os.environ.get(n)]
    if missing:
        log.error("Missing required env var(s): %s. Add them under Settings -> Secrets and variables -> Actions.", ", ".join(missing))
        sys.exit(1)


def load_disease_groups() -> tuple[dict, dict]:
    """(group_id -> set of leaf disease ids, group_id -> display label)."""
    data = json.loads(DISEASE_GROUPS_JSON.read_text())
    leaves = {g["id"]: set(g["ids"]) for g in data["groups"]}
    labels = {g["id"]: g["label"] for g in data["groups"]}
    return leaves, labels


def load_articles() -> tuple[list[dict], str]:
    data = json.loads(DATA_JSON.read_text())
    return data["articles"], data.get("week_label", "")


def fetch_subscribers() -> list[dict]:
    resp = requests.get(
        f"{SUPABASE_URL}/rest/v1/subscribers",
        params={"is_active": "eq.true", "select": "*"},
        headers={
            "apikey": SUPABASE_SERVICE_ROLE_KEY,
            "Authorization": f"Bearer {SUPABASE_SERVICE_ROLE_KEY}",
        },
        timeout=30,
    )
    if resp.status_code >= 300:
        log.error("Supabase subscriber fetch failed: %s %s", resp.status_code, resp.text[:500])
        resp.raise_for_status()
    return resp.json()


def site_counts_for(subscriber: dict) -> dict[str, int]:
    """site id -> number of abstracts for that site's email.

    Prefers the per-site site_top_n column; falls back to the older single
    top_n applied to every site in disease_sites (rows saved before the
    supabase/002_per_site_top_n.sql migration, or if it hasn't been run).
    """
    counts = subscriber.get("site_top_n") or {}
    if not counts:
        n = subscriber.get("top_n") or DEFAULT_TOP_N
        counts = {s: n for s in (subscriber.get("disease_sites") or [])}
    out = {}
    for site, n in counts.items():
        try:
            out[site] = max(1, min(20, int(n)))
        except (TypeError, ValueError):
            out[site] = DEFAULT_TOP_N
    return out


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


def top_articles_for_site(site: str, top_n: int, articles: list[dict], group_leaf_ids: dict) -> list[dict]:
    leaf_ids = group_leaf_ids.get(site, set())
    if not leaf_ids:
        return []
    matches = [a for a in articles if a.get("disease") in leaf_ids]
    matches.sort(key=lambda a: a.get("importance_score") or 0, reverse=True)
    return matches[:top_n]


def render_email_html(subscriber: dict, site_label: str, week_label: str, picks: list[dict], pmc_links: dict) -> str:
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
        <div style="font-size:13px;color:#8ea8cc;margin-top:4px;">{site_label} &middot; top {len(picks)} for {week_label}</div>
      </td></tr>
      <tr><td style="padding:8px 28px;">
        <table width="100%" cellpadding="0" cellspacing="0">{''.join(cards)}</table>
      </td></tr>
      <tr><td style="padding:20px 28px;font-size:12px;color:#8a93a6;">
        Sent because {subscriber['email']} is subscribed to the OncoPulse {site_label} digest.
        <a href="{manage_url}" style="color:#1d6fd6;">Manage preferences</a>
        &middot;
        <a href="{unsub_url}" style="color:#1d6fd6;">Unsubscribe</a>
      </td></tr>
    </table>
  </td></tr></table>
</body>
</html>"""


class SendError(Exception):
    pass


def send_email(to_email: str, subject: str, html: str) -> None:
    resp = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {RESEND_API_KEY}"},
        json={"from": RESEND_FROM, "to": [to_email], "subject": subject, "html": html},
        timeout=30,
    )
    if resp.status_code >= 300:
        # Resend's body says why, e.g. an unverified "from" domain (403).
        raise SendError(f"{resp.status_code} {resp.text[:300]}")


def write_summary(lines: list[str]) -> None:
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if path:
        with open(path, "a") as fh:
            fh.write("\n".join(lines) + "\n")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry-run", action="store_true", help="write HTML previews, send nothing")
    ap.add_argument("--test-to", help="send every site's email to this one address only")
    args = ap.parse_args()

    articles, week_label = load_articles()
    group_leaf_ids, group_labels = load_disease_groups()

    if args.test_to:
        subscribers = [{
            "email": args.test_to,
            "unsubscribe_token": "test",
            "site_top_n": {g: DEFAULT_TOP_N for g in group_leaf_ids},
        }]
    elif args.dry_run and not (SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY):
        subscribers = [{"email": "preview@example.org", "unsubscribe_token": "preview",
                        "site_top_n": {g: DEFAULT_TOP_N for g in group_leaf_ids}}]
    else:
        require_env("SUPABASE_URL", "SUPABASE_SERVICE_ROLE_KEY", "SITE_URL")
        subscribers = fetch_subscribers()
    if not args.dry_run:
        require_env("RESEND_API_KEY")
        if not os.environ.get("RESEND_FROM"):
            log.warning("RESEND_FROM not set; using %s, which only works if that domain is verified in Resend", RESEND_FROM)
    log.info("Loaded %d articles, %d active subscribers", len(articles), len(subscribers))

    all_pmids = [a.get("pmid") for a in articles if a.get("pmid")]
    pmc_links = pmids_to_pmc_oa_links(all_pmids)
    log.info("Resolved %d free PMC full-text links out of %d PubMed articles", len(pmc_links), len(all_pmids))

    preview_dir = Path("digest_preview")
    sent, skipped, failures = 0, 0, []
    for sub in subscribers:
        for site, top_n in site_counts_for(sub).items():
            label = group_labels.get(site, site)
            picks = top_articles_for_site(site, top_n, articles, group_leaf_ids)
            if not picks:
                skipped += 1
                continue
            html = render_email_html(sub, label, week_label, picks, pmc_links)
            subject = f"OncoPulse {label}: top {len(picks)} for {week_label}"
            if args.dry_run:
                preview_dir.mkdir(exist_ok=True)
                safe = sub["email"].replace("@", "_at_")
                (preview_dir / f"{safe}__{site}.html").write_text(html)
                log.info("[dry run] %s -> %s (%d articles)", subject, sub["email"], len(picks))
                sent += 1
                continue
            try:
                send_email(sub["email"], subject, html)
                sent += 1
                log.info("Sent %s -> %s", site, sub["email"])
            except (SendError, requests.RequestException) as exc:
                failures.append((sub["email"], site, str(exc)))
                log.error("Failed sending %s to %s: %s", site, sub["email"], exc)
            time.sleep(0.6)  # Resend default limit is 2 req/sec

    verb = "previewed" if args.dry_run else "sent"
    log.info("Digest run complete: %d %s, %d skipped (no articles for that site), %d failed",
             sent, verb, skipped, len(failures))
    summary = [
        "### Digest emails",
        f"- Subscribers: {len(subscribers)}",
        f"- Emails {verb}: {sent}",
        f"- Skipped (site had no articles this week): {skipped}",
        f"- Failed: {len(failures)}",
    ]
    for email, site, err in failures[:20]:
        summary.append(f"  - `{site}` to {email.split('@')[0][:2]}***@{email.split('@')[-1]}: {err}")
    write_summary(summary)
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
