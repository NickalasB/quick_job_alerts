#!/usr/bin/env python3
"""
Product Manager / Technical Product Manager job alert (software roles).

A second, independent profile. It reuses job_alert.py's helpers and company
list (TARGET_COMPANIES) but has its own title filters, Adzuna searches, state
file (seen_jobs_pm.json) and GitHub workflow (check_pm_jobs.yml), so it can be
switched on and off separately from the recruiting alerts.

Needs job_alert.py in the same folder. Importing it runs nothing.
"""

import json
import os
import re
import sys
import time
from pathlib import Path

import requests

import job_alert as base

SEEN_FILE = Path(__file__).parent / "seen_jobs_pm.json"

# Alerts go to PM_NTFY_TOPIC if that secret is set, otherwise to the same
# topic as the recruiting alerts.
NTFY_TOPIC = os.environ.get("PM_NTFY_TOPIC") or base.NTFY_TOPIC

MIN_SALARY = 100000  # soft floor: only applied when a salary is listed

# --- Title filters ---------------------------------------------------------
ROLE_PATTERN = re.compile(
    r"\b(product\s+manage(r|ment)|"
    r"(head|vp|vice\s+president)\s*,?\s+(of\s+)?product"
    r"(?!\s+(marketing|design|operations)))\b",
    re.I,
)
# Entry-level titles plus clearly non-software product roles.
EXCLUDE_PATTERN = re.compile(
    r"\b(associate|apm|intern(ship)?|junior|jr\.?|assistant|coordinator|"
    r"hardware|mechanical|electrical|chemical|pharma(ceutical)?|clinical|"
    r"food|beverage|apparel|footwear|merchandis\w*|packaging|"
    r"manufacturing|construction)\b",
    re.I,
)
# Gets the star + high priority in the notification.
PRIORITY_PATTERN = re.compile(r"\btechnical\s+product\b", re.I)

# Many target companies hire worldwide, so "Remote - UK" would otherwise pass
# the remote check. Reject locations naming a non-US place unless they also
# mention the US. Portland-area locations always pass.
NON_US_PATTERN = re.compile(
    r"\b(uk|united kingdom|england|london|ireland|dublin|germany|berlin|"
    r"munich|france|paris|spain|madrid|barcelona|portugal|lisbon|"
    r"netherlands|amsterdam|poland|warsaw|sweden|stockholm|finland|"
    r"helsinki|denmark|copenhagen|norway|oslo|switzerland|zurich|italy|"
    r"milan|israel|tel aviv|india|bangalore|bengaluru|hyderabad|pune|"
    r"mumbai|singapore|japan|tokyo|korea|seoul|china|shanghai|beijing|"
    r"australia|sydney|melbourne|brazil|s[aã]o paulo|(?<!new )mexico|"
    r"argentina|colombia|canada|toronto|emea|apac|latam|europe|asia)\b",
    re.I,
)
US_PATTERN = re.compile(
    r"(united states|\busa\b|\bus\b|north america|americas)", re.I
)

# Point the shared filter at the PM rules. base.passes_filters() reads these
# module-level names when it runs, so overriding them here (in this process
# only) reuses its Portland / remote / salary logic unchanged.
base.ROLE_PATTERN = ROLE_PATTERN
base.EXCLUDE_PATTERN = EXCLUDE_PATTERN
base.MIN_SALARY = MIN_SALARY

# --- Adzuna searches ---------------------------------------------------------
# PM is a high-volume role, so a single nationwide search would fill its 50
# newest results with jobs that fail the location filter. Instead: one search
# centred on Portland, plus remote-specific searches. Adzuna's "what" ANDs its
# words, so "product manager remote" requires all three.
# Each entry: (what, where). where is a list of spellings to try in order
# (Adzuna silently returns 0 results for a place name it doesn't recognise),
# or None for nationwide.
PORTLAND_WHERE = ["Portland, Oregon", "Portland, OR", "97204"]
PORTLAND_RADIUS_KM = 40  # Adzuna's distance is kilometres (~25 miles)
ADZUNA_QUERIES = [
    ("product manager", PORTLAND_WHERE),
    ("product manager remote", None),
    ("technical product manager remote", None),
]


def adzuna_search(what: str, where: str | None) -> list[dict]:
    url = f"https://api.adzuna.com/v1/api/jobs/{base.COUNTRY}/search/1"
    params = {
        "app_id": base.ADZUNA_APP_ID,
        "app_key": base.ADZUNA_APP_KEY,
        "results_per_page": base.RESULTS_PER_PAGE,
        "what": what,
        "max_days_old": base.MAX_DAYS_OLD,
        "sort_by": "date",
        "content-type": "application/json",
    }
    if where:
        params["where"] = where
        params["distance"] = PORTLAND_RADIUS_KM
    label = f"{what!r}" + (f" near {where!r}" if where else "")

    try:
        resp = requests.get(url, params=params, timeout=base.REQUEST_TIMEOUT)
    except requests.RequestException as e:
        print(f"  [adzuna:{label}] request failed: {e}", file=sys.stderr)
        return []
    if resp.status_code != 200:
        print(
            f"  [adzuna:{label}] HTTP {resp.status_code}: {resp.text[:300]}",
            file=sys.stderr,
        )
        return []

    results = resp.json().get("results", [])
    print(f"  [adzuna:{label}] raw results: {len(results)}")
    return results


def collect_adzuna_jobs() -> list[dict]:
    jobs = []
    for what, where in ADZUNA_QUERIES:
        raw = []
        for place in where or [None]:
            raw = adzuna_search(what, place)
            time.sleep(base.DELAY_BETWEEN_REQUESTS)
            if raw:
                break  # only fall through to the next spelling on 0 results
        jobs.extend(base.normalize_adzuna_job(r) for r in raw)
    return jobs


# --- Filtering -----------------------------------------------------------------
def passes_filters(job: dict) -> bool:
    loc = job["location"]
    if (
        NON_US_PATTERN.search(loc)
        and not US_PATTERN.search(loc)
        and not base.PORTLAND_METRO_PATTERN.search(loc)
    ):
        return False
    return base.passes_filters(job)


# --- State -------------------------------------------------------------------------
def load_state() -> tuple[set[str], set[str]]:
    """Returns (seen job uids, baselined company prefixes)."""
    try:
        data = json.loads(SEEN_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        data = {}
    return set(data.get("uids", [])), set(data.get("baselined", []))


def save_state(seen: set[str], baselined: set[str]) -> None:
    SEEN_FILE.write_text(
        json.dumps(
            {"uids": sorted(seen), "baselined": sorted(baselined)}, indent=2
        )
    )


# --- Notification ---------------------------------------------------------------
def send_alert(job: dict) -> None:
    body = f"{job['company']} — {job['location']}"
    posted = base.format_posted_ago(job.get("posted_at"))
    if posted:
        body += f"\n{posted}"

    starred = bool(PRIORITY_PATTERN.search(job["title"]))
    title = f"⭐ {job['title']}" if starred else job["title"]

    resp = requests.post(
        f"{base.NTFY_SERVER}/{NTFY_TOPIC}",
        data=body.encode("utf-8"),
        headers={
            "Title": title[:200].encode("utf-8"),
            "Priority": "high" if starred else "default",
            "Tags": "rocket",
            "Click": job["url"],
            "Actions": f"view, Open posting, {job['url']}",
        },
        timeout=15,
    )
    resp.raise_for_status()


# --- Main ------------------------------------------------------------------------------
def main() -> int:
    if not base.ADZUNA_APP_ID or not base.ADZUNA_APP_KEY:
        print("ERROR: ADZUNA_APP_ID / ADZUNA_APP_KEY not set.", file=sys.stderr)
        return 1
    if not NTFY_TOPIC:
        print("ERROR: set NTFY_TOPIC or PM_NTFY_TOPIC.", file=sys.stderr)
        return 1

    # Manual-run option: alert on everything open right now, ignoring history.
    notify_all = os.environ.get("NOTIFY_ALL", "").lower() == "true"
    seen, baselined = load_state()

    print("=== Adzuna (PM searches) ===")
    adzuna_jobs = collect_adzuna_jobs()
    print("\n=== Target companies (direct boards) ===")
    company_jobs = base.collect_target_company_jobs()

    matches = {}
    for job in adzuna_jobs + company_jobs:
        if passes_filters(job):
            matches[job["uid"]] = job

    # A company's own board is the authority for its jobs, so drop Adzuna
    # copies of a role the board already matched (same company + title).
    board_fps = {
        base.fingerprint(j)
        for j in matches.values()
        if not j["uid"].startswith("adzuna:")
    }
    # Companies whose boards actually returned jobs this run.
    fetched = {p for j in company_jobs if (p := base.company_prefix(j["uid"]))}

    to_alert, to_seed = [], []
    for job in matches.values():
        uid = job["uid"]
        if uid.startswith("adzuna:") and base.fingerprint(job) in board_fps:
            continue
        if uid in seen and not notify_all:
            continue
        prefix = base.company_prefix(uid)
        if prefix and prefix not in baselined and not notify_all:
            # First time we've ever seen this company's board: everything open
            # is backlog, not news. Record it silently.
            to_seed.append(job)
        else:
            to_alert.append(job)
    baselined |= fetched  # even companies with zero matches are now baselined

    print(
        f"\nFound {len(matches)} matching jobs: {len(to_alert)} to alert, "
        f"{len(to_seed)} baseline-seeded."
    )
    if to_seed:
        names = sorted({j["company"] for j in to_seed})
        print(f"Baseline-seeded (no alert) from: {', '.join(names)}")
    if not matches and (adzuna_jobs or company_jobs):
        print("No jobs passed filters. Sample of raw Adzuna results:")
        for j in adzuna_jobs[:10]:
            print(f"  - {j['title']!r} @ {j['company']!r} | {j['location']!r}")
    if not adzuna_jobs and not company_jobs:
        print("Every source returned 0 results -- check for HTTP errors above.")

    for job in to_seed:
        seen.add(job["uid"])
    for job in to_alert:
        try:
            send_alert(job)
            seen.add(job["uid"])
            print(f"Alerted: {job['title']} @ {job['company']}")
        except requests.RequestException as e:
            # Not marked seen, so it retries on the next run.
            print(f"Alert failed for {job['uid']}: {e}", file=sys.stderr)
        time.sleep(1)

    save_state(seen, baselined)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
