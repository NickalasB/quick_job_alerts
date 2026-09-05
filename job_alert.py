#!/usr/bin/env python3
"""
Job alert bot: Director-level or Senior Recruiter/Talent Acquisition roles
in Portland, OR or Remote, at tech companies.

Data source: Adzuna Job Search API (free tier).
Delivery: ntfy.sh push notification (instant, works on iOS/Android with the
free ntfy app, no account or app-store publishing required).

Designed to run every 10-15 minutes via GitHub Actions (see
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

# How far back to look for postings (days). Keep small since this runs
# frequently; it's a safety net against missed runs, not the primary filter.
MAX_DAYS_OLD = 3

# Separate searches, one per role concept. Adzuna's documented "what" param
# ANDs together every word in the string, so a single combined query like
# "recruiter recruiting talent acquisition" would require ALL of those words
# in one listing and return nothing. Running one simple, well-documented
# "what" query per phrase and merging results client-side is more reliable
# than depending on undocumented OR-style parameters.
ROLE_QUERIES = ["recruiting", "recruiter", "talent acquisition"]

# Client-side filters applied on top of the API results, since Adzuna's
# search is fuzzy. This is where the real precision comes from.
#
# No level/seniority requirement here on purpose: Director, Principal,
# Staff, Manager, Senior, and plain "Recruiter" titles are all acceptable,
# so requiring a level word only narrows results without adding value.
# The $100k salary floor (via MIN_SALARY / Adzuna's salary_min param) does
# the seniority filtering instead, plus EXCLUDE_PATTERN below for clearly
# entry-level titles.
ROLE_PATTERN = re.compile(
    r"\b(recruit(er|ing|ment)?|talent\s*acquisition|talent\s*partner|"
    r"talent\s*advisor|sourcer|sourcing)\b",
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


# ---------------------------------------------------------------------------
# Adzuna search
# ---------------------------------------------------------------------------

def adzuna_search(what: str, page: int = 1) -> list[dict]:
    """Run one Adzuna search for a single role phrase, nationwide.

    We search nationwide (no "where") rather than splitting into a
    Portland-specific call and a remote-specific call: Portland postings
    still show up in a nationwide search, and this halves the number of
    API calls, which matters for staying under Adzuna's free-tier daily cap.
    Location precision (Portland vs. remote) is enforced client-side in
    passes_filters().
    """
    url = f"https://api.adzuna.com/v1/api/jobs/{COUNTRY}/search/{page}"
    params = {
        "app_id": ADZUNA_APP_ID,
        "app_key": ADZUNA_APP_KEY,
        "results_per_page": RESULTS_PER_PAGE,
        "what": what,
        "max_days_old": MAX_DAYS_OLD,
        "salary_min": MIN_SALARY,
        "sort_by": "date",
        "content-type": "application/json",
    }

    resp = requests.get(url, params=params, timeout=30)
    resp.raise_for_status()
    return resp.json().get("results", [])


def passes_filters(job: dict) -> bool:
    title = job.get("title", "")
    description = job.get("description", "")
    location = (job.get("location") or {}).get("display_name", "")

    if EXCLUDE_PATTERN.search(title):
        return False
    if not ROLE_PATTERN.search(title):
        return False

    is_portland = bool(PORTLAND_METRO_PATTERN.search(location))
    is_remote = bool(
        REMOTE_PATTERN.search(title)
        or REMOTE_PATTERN.search(location)
        or REMOTE_PATTERN.search(description[:500])
    )
    return is_portland or is_remote


def collect_matches() -> list[dict]:
    matches = {}
    for query in ROLE_QUERIES:
        for job in adzuna_search(what=query):
            if passes_filters(job):
                matches[job["id"]] = job
    return list(matches.values())


# ---------------------------------------------------------------------------
# State (dedupe across runs)
# ---------------------------------------------------------------------------

def load_seen_ids() -> set[str]:
    if not SEEN_JOBS_FILE.exists():
        return set()
    try:
        return set(json.loads(SEEN_JOBS_FILE.read_text()))
    except (json.JSONDecodeError, OSError):
        return set()


def save_seen_ids(ids: set[str]) -> None:
    # Cap file size: keep the most recent 2000 ids so it doesn't grow forever.
    trimmed = list(ids)[-2000:]
    SEEN_JOBS_FILE.write_text(json.dumps(trimmed, indent=2))


# ---------------------------------------------------------------------------
# Notification
# ---------------------------------------------------------------------------

def send_ntfy_alert(job: dict) -> None:
    title = job.get("title", "New role")
    company = (job.get("company") or {}).get("display_name", "Unknown company")
    location = (job.get("location") or {}).get("display_name", "")
    url = job.get("redirect_url", "")

    message = f"{company} — {location}"

    resp = requests.post(
        f"{NTFY_SERVER}/{NTFY_TOPIC}",
        data=message.encode("utf-8"),
        headers={
            "Title": title[:200].encode("utf-8"),
            "Priority": "high",
            "Tags": "briefcase,rotating_light",
            "Click": url,
            "Actions": f"view, Open posting, {url}",
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

    seen = load_seen_ids()
    matches = collect_matches()

    new_matches = [j for j in matches if str(j["id"]) not in seen]
    print(f"Found {len(matches)} matching jobs, {len(new_matches)} new.")

    for job in new_matches:
        try:
            send_ntfy_alert(job)
            print(f"Alerted: {job.get('title')} @ {(job.get('company') or {}).get('display_name')}")
        except requests.RequestException as e:
            print(f"Failed to send alert for job {job.get('id')}: {e}", file=sys.stderr)
        seen.add(str(job["id"]))
        time.sleep(1)  # be polite to ntfy

    save_seen_ids(seen)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
