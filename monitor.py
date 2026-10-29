
import logging
import os
import re
import smtplib
import sys
from datetime import datetime, timezone
from email.message import EmailMessage
from urllib.parse import parse_qs, urljoin, urlparse

import requests
from bs4 import BeautifulSoup

# -------------------- Configuration --------------------

BASE_URL = "https://technopark.in"
SEARCH_URL = f"{BASE_URL}/job-search?search=flutter&type="
CRAWL_URL = f"{BASE_URL}/job-crawl"

SUPABASE_URL = os.environ["SUPABASE_URL"].rstrip("/")
SUPABASE_KEY = os.environ["SUPABASE_SERVICE_ROLE_KEY"]

EMAIL_USER = os.environ["EMAIL_USER"]
EMAIL_APP_PASSWORD = os.environ["EMAIL_APP_PASSWORD"]
ALERT_EMAIL = os.environ["ALERT_EMAIL"]

TABLE_URL = f"{SUPABASE_URL}/rest/v1/technopark_jobs"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)

session = requests.Session()
session.headers.update({
    "User-Agent": "Mozilla/5.0 (compatible; TechnoparkJobMonitor/1.0)",
    "Accept": "text/html,application/xhtml+xml",
})

db_headers = {
    "apikey": SUPABASE_KEY,
    "Content-Type": "application/json",
}


# -------------------- Website scraping --------------------

def fetch_html(url):
    """Download a page or raise an error if it cannot be fetched."""
    response = session.get(url, timeout=30)
    response.raise_for_status()

    if not response.text.strip():
        raise RuntimeError(f"Empty response from {url}")

    return response.text


def extract_job_id(url):
    """Extract the numeric ID from a Technopark job-detail URL."""
    path = urlparse(url).path
    match = re.search(r"/job-details/(\d+)", path)
    return match.group(1) if match else None


def extract_title(anchor):
    """Prefer the job title in the URL, then use visible link text."""
    query = parse_qs(urlparse(anchor.get("href", "")).query)
    title = query.get("job", [None])[0]

    if title and title.strip():
        return title.strip()

    return anchor.get_text(" ", strip=True)


def parse_listing_page(html, search_results=False):
    """
    Extract unique job links.

    The search page is assumed to have already filtered results for
    Flutter. On the crawl-page fallback, match Flutter in the title.
    """
    soup = BeautifulSoup(html, "html.parser")
    jobs = {}

    for anchor in soup.select("a[href]"):
        url = urljoin(BASE_URL, anchor["href"])
        parsed = urlparse(url)

        if parsed.hostname not in {
            "technopark.in",
            "www.technopark.in",
        }:
            continue

        job_id = extract_job_id(url)
        if not job_id:
            continue

        title = extract_title(anchor)

        # Crawl page is unfiltered, so apply a title keyword filter.
        # Search results are already filtered by the site's query.
        if not search_results and not re.search(
            r"\bflutter\b", title, flags=re.IGNORECASE
        ):
            continue

        if not title or title.lower() == "job posting":
            title = f"Technopark job #{job_id}"

        jobs[job_id] = {
            "job_id": job_id,
            "title": title,
            "company": None,
            "job_url": f"{BASE_URL}/job-details/{job_id}",
        }

    return list(jobs.values())


def discover_jobs():
    """
    Try the Flutter search first.
    Use the public crawl page if the search page fails or yields no jobs.
    """
    errors = []

    for url, is_search in [
        (SEARCH_URL, True),
        (CRAWL_URL, False),
    ]:
        try:
            html = fetch_html(url)
            jobs = parse_listing_page(html, search_results=is_search)

            logging.info(
                "Checked %s: found %d matching jobs",
                url,
                len(jobs),
            )

            if jobs:
                return jobs

            errors.append(f"{url}: no matching job links found")

        except (requests.RequestException, RuntimeError) as exc:
            errors.append(f"{url}: {exc}")

    raise RuntimeError(
        "Could not discover any jobs. " + " | ".join(errors)
    )


# -------------------- Supabase database --------------------



def db_request(method, params=None, payload=None, prefer=None):
    headers = dict(db_headers)

    # Use a server-identifying User-Agent for Supabase.
    headers["User-Agent"] = "TechnoparkJobMonitor/1.0"

    if prefer:
        headers["Prefer"] = prefer

    response = session.request(
        method=method,
        url=TABLE_URL,
        headers=headers,
        params=params,
        json=payload,
        timeout=30,
    )

    if response.status_code >= 400:
        logging.error(
            "Supabase request failed: method=%s status=%s",
            method,
            response.status_code,
        )
        logging.error(
            "Supabase response body: %s",
            response.text[:1000],
        )

    response.raise_for_status()

    if not response.content:
        return []

    return response.json()




def utc_now():
    return datetime.now(timezone.utc).isoformat()


def get_one_existing_job():
    """Return one saved row, or an empty list."""
    return db_request(
        "GET",
        params={
            "select": "job_id",
            "limit": "1",
        },
    )


def insert_job(job, notified_at=None):
    """
    Insert a job only if its ID is new.
    Returns True only when a new row was inserted.
    """
    payload = {
        "job_id": job["job_id"],
        "title": job["title"],
        "company": job.get("company"),
        "job_url": job["job_url"],
        "notified_at": notified_at,
    }

    rows = db_request(
        "POST",
        params={"on_conflict": "job_id"},
        payload=payload,
        prefer="resolution=ignore-duplicates,return=representation",
    )

    return bool(rows)


def get_pending_notifications():
    """Retrieve new jobs whose email notification is still pending."""
    return db_request(
        "GET",
        params={
            "select": "job_id,title,company,job_url",
            "notified_at": "is.null",
            "order": "first_seen_at.asc",
            "limit": "1000",
        },
    )


def mark_notified(job_ids):
    """Mark jobs as notified after Gmail accepts the email."""
    for job_id in job_ids:
        db_request(
            "PATCH",
            params={"job_id": f"eq.{job_id}"},
            payload={"notified_at": utc_now()},
            prefer="return=minimal",
        )


# -------------------- Email notifications --------------------

def send_email(jobs):
    if not jobs:
        return

    message = EmailMessage()
    message["Subject"] = (
        f"[Technopark Jobs] {len(jobs)} new Flutter posting(s)"
    )
    message["From"] = EMAIL_USER
    message["To"] = ALERT_EMAIL

    lines = [
        f"Found {len(jobs)} new Flutter-related Technopark job(s).",
        "",
    ]

    for job in jobs:
        lines.extend([
            f"Title: {job['title']}",
            f"Company: {job.get('company') or 'See job details'}",
            f"Link: {job['job_url']}",
            "",
        ])

    lines.extend([
        "This alert was generated by your Technopark job monitor.",
    ])

    message.set_content("\n".join(lines))

    with smtplib.SMTP_SSL(
        "smtp.gmail.com",
        465,
        timeout=30,
    ) as smtp:
        smtp.login(EMAIL_USER, EMAIL_APP_PASSWORD)
        smtp.send_message(message)

    logging.info("Email sent for %d jobs.", len(jobs))


# -------------------- Main monitoring flow --------------------

def main():
    jobs = discover_jobs()

    if not jobs:
        raise RuntimeError(
            "No jobs discovered. Database was not changed."
        )

    # On the first run, establish a baseline without sending alerts
    # for all jobs that were already listed.
    existing = get_one_existing_job()

    if not existing:
        for job in jobs:
            insert_job(job, notified_at=utc_now())

        logging.info(
            "Initial baseline saved. Existing jobs: %d",
            len(jobs),
        )
        return

    # Insert new listings. Existing IDs are ignored by PostgreSQL.
    for job in jobs:
        was_inserted = insert_job(job, notified_at=None)

        if was_inserted:
            logging.info(
                "New job discovered: %s (%s)",
                job["title"],
                job["job_id"],
            )

    # Retry any unsent alerts from previous runs as well.
    pending = get_pending_notifications()

    if not pending:
        logging.info("No new Flutter jobs. No email required.")
        return

    # If email delivery fails, notified_at remains NULL, so the
    # next scheduled run can retry the notification.
    send_email(pending)
    mark_notified([job["job_id"] for job in pending])

    logging.info(
        "Monitoring completed successfully. Notified: %d",
        len(pending),
    )


if __name__ == "__main__":
    try:
        main()
    except Exception:
        logging.exception("Technopark monitor failed.")
        sys.exit(1)
