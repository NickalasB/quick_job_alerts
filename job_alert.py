#!/usr/bin/env python3
"""
Job alert bot: Director-level or Senior Recruiter/Talent Acquisition roles
in Portland, OR or Remote, at tech companies.

Data sources:
  1. Adzuna Job Search API (free tier) -- broad net across the whole market.
  2. Direct polling of specific companies' Greenhouse/Lever/Ashby job
     boards -- for companies confirmed (via discover_ats.py) to run on one
     of those platforms. This catches postings the moment they go live,
     often before they reach any aggregator.

Delivery: ntfy.sh push notification.

Designed to run every 20 minutes via GitHub Actions (see
.github/workflows/check_jobs.yml). State (which jobs we've already alerted
on) is persisted in seen_jobs.json, which the workflow commits back to the
repo after each run.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

ADZUNA_APP_ID = os.environ.get("ADZUNA_APP_ID")
ADZUNA_APP_KEY = os.environ.get("ADZUNA_APP_KEY")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC")  # e.g. "jane-ta-jobs-x7f2q"
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")

COUNTRY = "us"
SEEN_JOBS_FILE = Path(__file__).parent / "seen_jobs.json"

# How far back Adzuna looks (days). Sorted by date, so this is a safety net
# against missed runs, not the primary filter.
MAX_DAYS_OLD = 7

# Separate searches, one per role concept. Adzuna's documented "what" param
# ANDs together every word in the string, so a single combined query would
# require ALL of those words in one listing and return nothing. Running one
# simple "what" query per phrase and merging client-side is more reliable
# than depending on undocumented OR-style parameters.
ROLE_QUERIES = ["recruiting", "recruiter", "talent acquisition"]

# Companies confirmed (via discover_ats.py) to run their careers page on
# Greenhouse, Lever, or Ashby -- polled directly in addition to Adzuna.
# Format: (display_name, platform, board_slug)
TARGET_COMPANIES = [
    ("Blue Bottle Coffee", "lever", "bluebottlecoffee"),
    ("QuintoAndar", "greenhouse", "quintoandar"),
    ("SHEIN", "greenhouse", "shein"),
    ("Nubank", "ashby", "nubank"),
    ("Betterment", "greenhouse", "betterment"),
    ("SoFi", "greenhouse", "sofi"),
    ("Block", "greenhouse", "block"),
    ("Tamara", "greenhouse", "tamara"),
    ("Tide", "greenhouse", "tide"),
    ("Lucid Motors", "greenhouse", "lucidmotors"),
    ("MOIA", "greenhouse", "moia"),
    ("Supercell", "ashby", "supercell"),
    ("LG Electronics", "greenhouse", "lgelectronics"),
    ("Knowunity", "ashby", "knowunity"),
    ("Tonal", "ashby", "tonal"),
    ("Wolt", "greenhouse", "wolt"),
    ("Canonical", "greenhouse", "canonical"),
    ("ClickUp", "ashby", "clickup"),
]

# Client-side filters applied on top of every source's results.
#
# No level/seniority requirement here on purpose: Director, Principal,
# Staff, Manager, Senior, and plain "Recruiter" titles are all acceptable,
# so requiring a level word only narrows results without adding value.
# The $100k salary floor (where salary data exists) does the seniority
# filtering instead, plus EXCLUDE_PATTERN below for clearly entry-level
# titles.
ROLE_PATTERN = re.compile(
    r"\b(recruit(er|ing|ment)?|talent\s*acquisition|talent\s*partner|"
    r"talent\s*advisor|sourcer|sourcing|head\s+of\s+talent|"
    r"vp\s+talent|vice\s+president\s*,?\s+talent|"
    r"chief\s+talent\s+officer|people\s+acquisition)\b",
    re.I,
)
EXCLUDE_PATTERN = re.compile(
    r"\b(coordinator|assistant|intern(ship)?|junior|jr\.?|associate|"
    r"admin(istrative)?|scheduler)\b",
    re.I,
)
MIN_SALARY = 100000
REMOTE_PATTERN = re.compile(r"\bremote\b", re.I)

# Portland-metro suburbs/cities that a job board might list instead of
# "Portland" itself, but that are effectively the same commute area.
PORTLAND_METRO_PATTERN = re.compile(
    r"\b(portland|beaverton|hillsboro|tigard|lake oswego|tualatin|"
    r"wilsonville|gresham|milwaukie|oregon city|clackamas|vancouver,?\s*wa)\b",
    re.I,
)

RESULTS_PER_PAGE = 50
REQUEST_TIMEOUT = 20
DELAY_BETWEEN_REQUESTS = 0.3  # be a reasonable citizen of these free APIs


# ---------------------------------------------------------------------------
# Freshness formatting
# ---------------------------------------------------------------------------

def format_posted_ago(posted_at) -> str:
    """Turn a source's raw timestamp (ISO 8601 string or epoch-ms int/str)
    into a short "posted X ago" string. Returns "" if it can't be parsed --
    freshness is a nice-to-have, never worth crashing the run over."""
    if not posted_at:
        return ""

    from datetime import datetime, timezone

    dt = None
    try:
        if isinstance(posted_at, (int, float)) or (
            isinstance(posted_at, str) and posted_at.isdigit()
        ):
            # Epoch milliseconds (Lever's createdAt).
            dt = datetime.fromtimestamp(int(posted_at) / 1000, tz=timezone.utc)
        else:
            # ISO 8601, e.g. "2026-09-04T18:07:39Z"
            dt = datetime.fromisoformat(str(posted_at).replace("Z", "+00:00"))
    except (ValueError, OverflowError, OSError):
        return ""

    if dt is None:
        return ""

    delta = datetime.now(timezone.utc) - dt
    hours = delta.total_seconds() / 3600
    if hours < 0:
        return ""
    if hours < 1:
        return "posted <1h ago"
    if hours < 24:
        return f"posted {int(hours)}h ago"
    days = int(hours / 24)
    return f"posted {days}d ago"


# ---------------------------------------------------------------------------
# Cross-source dedupe fingerprint
# ---------------------------------------------------------------------------

def fingerprint(job: dict) -> str:
    """Identify "the same real posting" across sources even when the raw
    IDs differ -- e.g. a company's own Greenhouse board and Adzuna's crawl
    of that same board would otherwise both alert on it separately. Keyed
    on company + title only (not location), since the same role can appear
    with slightly different location text between sources."""

    def normalize_company(name: str) -> str:
        name = name.lower()
        name = re.sub(r"\b(inc|llc|corp|ltd|co|company|group)\b\.?", "", name)
        return re.sub(r"[^a-z0-9]", "", name)

    company = normalize_company(job["company"])
    title = re.sub(r"[^a-z0-9]", "", job["title"].lower())
    return f"{company}:{title}"


# ---------------------------------------------------------------------------
# Normalized job format
# ---------------------------------------------------------------------------
# Every source below produces plain dicts with this shape, so filtering,
# deduping, and notifying are all source-agnostic:
#   uid           globally unique string (prefixed by source)
#   title         str
#   company       str
#   location      str
#   url           str
#   description   str (may be empty -- not all sources provide one cheaply)
#   salary_min    float | None
#   salary_max    float | None
#   is_remote_hint  bool | None  (explicit signal from the source, when it
#                                 has one -- preferred over regex guessing)


# ---------------------------------------------------------------------------
# Adzuna (broad market search)
# ---------------------------------------------------------------------------

def adzuna_search(what: str, page: int = 1) -> list[dict]:
    """Run one Adzuna search for a single role phrase, nationwide.

    We search nationwide (no "where") rather than splitting into a
    Portland-specific call and a remote-specific call: Portland postings
    still show up in a nationwide search, and this halves the number of
    API calls, which matters for staying under Adzuna's free-tier daily cap.
    Location precision (Portland vs. remote) is enforced client-side.
    """
    url = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search/{page}"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": RESULTS_PER_PAGE,
        "what": what,
        "max_days_old": MAX_DAYS_OLD,
        "sort_by": "date",
        "content-type": "application/json",
    }

    try:
        resp = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [adzuna:{what!r}] request failed: {e}", file=sys.stderr)
        return []

    if resp.status_code != 200:
        print(
            f"  [adzuna:{what!r}] HTTP {resp.status_code}: {resp.text[:500]}",
            file=sys.stderr,
        )
        return []

    results = resp.json().get("results", [])
    print(f"  [adzuna:{what!r}] raw results: {len(results)}")
    return results


def normalize_adzuna_job(job: dict) -> dict:
    location_obj = job.get("location") or {}
    return {
        "uid": f"adzuna:{job.get('id')}",
        "title": job.get("title", ""),
        "company": (job.get("company") or {}).get("display_name", "Unknown company"),
        "location": location_obj.get("display_name", ""),
        "url": job.get("redirect_url", ""),
        "description": job.get("description", "") or "",
        "salary_min": job.get("salary_min"),
        "salary_max": job.get("salary_max"),
        # Adzuna doesn't give an explicit remote flag; a location with no
        # specific city/state (just the country) is often how it represents
        # nationwide/remote roles.
        "is_remote_hint": True if len(location_obj.get("area", [])) <= 1 else None,
        "posted_at": job.get("created"),  # ISO 8601 string
    }


def collect_adzuna_jobs() -> list[dict]:
    all_jobs = []
    for query in ROLE_QUERIES:
        print(f"Searching Adzuna for {query!r}...")
        for raw in adzuna_search(what=query):
            all_jobs.append(normalize_adzuna_job(raw))
        time.sleep(DELAY_BETWEEN_REQUESTS)
    return all_jobs


# ---------------------------------------------------------------------------
# Direct company boards (Greenhouse / Lever / Ashby)
# ---------------------------------------------------------------------------

def fetch_greenhouse_jobs(company: str, slug: str) -> list[dict]:
    url = f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [greenhouse:{slug}] request failed: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        print(f"  [greenhouse:{slug}] HTTP {resp.status_code}", file=sys.stderr)
        return []

    jobs = resp.json().get("jobs", [])
    print(f"  [greenhouse:{slug}] raw jobs: {len(jobs)}")
    normalized = []
    for job in jobs:
        normalized.append({
            "uid": f"greenhouse:{slug}:{job.get('id')}",
            "title": job.get("title", ""),
            "company": company,
            "location": (job.get("location") or {}).get("name", ""),
            "url": job.get("absolute_url", ""),
            "description": "",  # not fetched, to keep payloads small
            "salary_min": None,
            "salary_max": None,
            "is_remote_hint": None,
            "posted_at": job.get("first_published") or job.get("updated_at"),
        })
    return normalized


def fetch_lever_jobs(company: str, slug: str) -> list[dict]:
    url = f"https://api.lever.co/v0/postings/{slug}"
    try:
        resp = requests.get(
            url, params={"mode": "json"}, timeout=REQUEST_TIMEOUT
        )
    except requests.RequestException as e:
        print(f"  [lever:{slug}] request failed: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        print(f"  [lever:{slug}] HTTP {resp.status_code}", file=sys.stderr)
        return []

    jobs = resp.json()
    if not isinstance(jobs, list):
        return []
    print(f"  [lever:{slug}] raw jobs: {len(jobs)}")
    normalized = []
    for job in jobs:
        categories = job.get("categories") or {}
        workplace_type = (job.get("workplaceType") or "").lower()
        normalized.append({
            "uid": f"lever:{slug}:{job.get('id')}",
            "title": job.get("text", ""),
            "company": company,
            "location": categories.get("location", ""),
            "url": job.get("hostedUrl", ""),
            "description": "",
            "salary_min": None,
            "salary_max": None,
            "is_remote_hint": True if workplace_type == "remote" else None,
            "posted_at": job.get("createdAt"),  # epoch milliseconds
        })
    return normalized


def fetch_ashby_jobs(company: str, slug: str) -> list[dict]:
    url = f"https://api.ashbyhq.com/posting-api/job-board/{slug}"
    try:
        resp = requests.get(url, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [ashby:{slug}] request failed: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        print(f"  [ashby:{slug}] HTTP {resp.status_code}", file=sys.stderr)
        return []

    jobs = resp.json().get("jobs", [])
    print(f"  [ashby:{slug}] raw jobs: {len(jobs)}")
    normalized = []
    for job in jobs:
        normalized.append({
            "uid": f"ashby:{slug}:{job.get('jobId')}",
            "title": job.get("title", ""),
            "company": company,
            "location": job.get("location", ""),
            "url": job.get("jobUrl", ""),
            "description": "",
            "salary_min": None,
            "salary_max": None,
            # Ashby gives an explicit remote flag -- trust it directly.
            "is_remote_hint": job.get("isRemote"),
            "posted_at": job.get("publishedAt"),  # ISO 8601 string
        })
    return normalized


FETCHERS = {
    "greenhouse": fetch_greenhouse_jobs,
    "lever": fetch_lever_jobs,
    "ashby": fetch_ashby_jobs,
}


def collect_target_company_jobs() -> list[dict]:
    all_jobs = []
    for company, platform, slug in TARGET_COMPANIES:
        fetcher = FETCHERS.get(platform)
        if not fetcher:
            print(f"  Unknown platform {platform!r} for {company}", file=sys.stderr)
            continue
        all_jobs.extend(fetcher(company, slug))
        time.sleep(DELAY_BETWEEN_REQUESTS)
    return all_jobs


# ---------------------------------------------------------------------------
# Filtering (source-agnostic)
# ---------------------------------------------------------------------------

def passes_filters(job: dict) -> bool:
    title = job["title"]
    location = job["location"]
    description = job["description"]

    if EXCLUDE_PATTERN.search(title):
        return False
    if not ROLE_PATTERN.search(title):
        return False

    # Soft salary filter: only reject a job if it HAS salary data and that
    # data is clearly below the floor. Most postings (especially on
    # Greenhouse/Lever/Ashby) don't list a salary at all -- treat unknown
    # as passing rather than excluding them.
    best_salary_signal = job["salary_max"] or job["salary_min"]
    if best_salary_signal is not None and best_salary_signal < MIN_SALARY:
        return False

    is_portland = bool(PORTLAND_METRO_PATTERN.search(location))

    if job["is_remote_hint"] is True:
        is_remote = True
    elif job["is_remote_hint"] is False:
        is_remote = False
    else:
        is_remote = bool(
            REMOTE_PATTERN.search(title)
            or REMOTE_PATTERN.search(location)
            or REMOTE_PATTERN.search(description[:500])
        )

    return is_portland or is_remote


def collect_matches() -> list[dict]:
    matches = {}

    print("=== Adzuna (broad search) ===")
    adzuna_jobs = collect_adzuna_jobs()
    for job in adzuna_jobs:
        if passes_filters(job):
            matches[job["uid"]] = job

    print("\n=== Target companies (direct boards) ===")
    company_jobs = collect_target_company_jobs()
    for job in company_jobs:
        if passes_filters(job):
            matches[job["uid"]] = job

    all_jobs = adzuna_jobs + company_jobs
    if not matches and all_jobs:
        print(
            f"\nNo jobs passed filters, but {len(all_jobs)} raw results came "
            f"back combined. Sample (unfiltered):"
        )
        for job in all_jobs[:10]:
            print(
                f"  - {job['title']!r} @ {job['company']!r} | "
                f"{job['location']!r} | salary_min={job['salary_min']} "
                f"salary_max={job['salary_max']} | uid={job['uid']}"
            )
    elif not all_jobs:
        print(
            "\nEvery source returned 0 raw results. This usually means an "
            "API problem (bad credentials, wrong parameter, or a rate "
            "limit) rather than a filtering issue -- check for HTTP error "
            "lines above."
        )

    return list(matches.values())


# ---------------------------------------------------------------------------
# State (dedupe across runs)
# ---------------------------------------------------------------------------

def load_seen() -> tuple[set[str], set[str]]:
    """Returns (seen_uids, seen_fingerprints). Backward-compatible with the
    old format (a flat JSON list of uids, no fingerprints)."""
    if not SEEN_JOBS_FILE.exists():
        return set(), set()
    try:
        data = json.loads(SEEN_JOBS_FILE.read_text())
    except (json.JSONDecodeError, OSError):
        return set(), set()

    if isinstance(data, list):
        # Old format: just uids.
        return set(data), set()
    if isinstance(data, dict):
        return set(data.get("uids", [])), set(data.get("fingerprints", []))
    return set(), set()


def save_seen(uids: set[str], fingerprints: set[str]) -> None:
    # Cap sizes so the file doesn't grow forever.
    payload = {
        "uids": list(uids)[-3000:],
        "fingerprints": list(fingerprints)[-3000:],
    }
    SEEN_JOBS_FILE.write_text(json.dumps(payload, indent=2))


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_ntfy_alert(job: dict) -> None:
    posted_str = format_posted_ago(job.get("posted_at"))
    message_parts = [f"{job['company']} — {job['location']}"]
    if posted_str:
        message_parts.append(posted_str)
    message = "\n".join(message_parts)

    # Surface exact-level matches (her current title) above adjacent ones
    # at a glance, since the target-company additions mean more volume to
    # triage than before.
    title = job["title"]
    is_director_level = bool(re.search(r"\bdirector\b", title, re.I))
    display_title = f"⭐ {title}" if is_director_level else title

    resp = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": display_title[:200].encode("utf-8"),
            "Priority": "high" if is_director_level else "default",
            "Tags": "briefcase,rotating_light",
            "Click": job["url"],
            "Actions": f"view, Open posting, {job['url']}",
        },
        timeout=15,
    )
    resp.raise_for_status()


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> int:
    if not ADZUNA_APP_ID or not ADZUNA_APP_KEY:
        print("ERROR: ADZUNA_APP_ID / ADZUNA_APP_KEY not set.", file=sys.stderr)
        return 1
    if not NTFY_TOPIC:
        print("ERROR: NTFY_TOPIC not set.", file=sys.stderr)
        return 1

    seen_uids, seen_fingerprints = load_seen()
    matches = collect_matches()

    new_matches = []
    for job in matches:
        fp = fingerprint(job)
        if job["uid"] in seen_uids or fp in seen_fingerprints:
            continue
        new_matches.append(job)

    print(f"\nFound {len(matches)} matching jobs, {len(new_matches)} new.")

    for job in new_matches:
        try:
            send_ntfy_alert(job)
            print(f"Alerted: {job['title']} @ {job['company']}")
        except requests.RequestException as e:
            print(f"Failed to send alert for job {job['uid']}: {e}", file=sys.stderr)
        seen_uids.add(job["uid"])
        seen_fingerprints.add(fingerprint(job))
        time.sleep(1)  # be polite to ntfy

    save_seen(seen_uids, seen_fingerprints)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
