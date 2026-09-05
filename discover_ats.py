#!/usr/bin/env python3
"""
One-time discovery tool: for a list of company names, test the actual
Greenhouse / Lever / Ashby public job-board APIs directly to see which
companies really run on one of those platforms.

Why this exists instead of just searching the web: a company's public
careers page (e.g. clickup.com/careers) often hides which ATS is running
underneath -- some companies proxy a Greenhouse or Lever board under their
own domain, and some fully custom-branded pages are Greenhouse-powered
while looking nothing like boards.greenhouse.io. Web search snippets can't
reliably tell the difference. Hitting the real API endpoints can.

This is meant to be run once (or occasionally, if you add companies), not
on a schedule -- see .github/workflows/discover_ats.yml (manual trigger
only).
"""

import re
import time
import requests

# Round 2: a broad, hand-picked set of prominent tech companies known for
# remote-friendly or remote-first policies, spanning many disciplines
# (fintech, dev tools, security, productivity SaaS, infra, consumer) --
# not tied to any particular technology, since her recruiting background
# spans tech generally. Round 1 (the Flutter-adoption list) already ran;
# its confirmed matches are wired into job_alert.py's TARGET_COMPANIES.
COMPANIES = [
    # Directly referenced elsewhere as running one of these platforms --
    # still verified here rather than assumed.
    "Stripe", "Airbnb", "GitLab", "Coinbase", "Robinhood", "Instacart",
    "Pinterest", "Lyft", "Discord", "Spotify", "Palantir", "Notion",
    "Ramp", "Linear", "OpenAI", "Figma", "Vercel", "Posthog",
    # Fintech
    "Plaid", "Brex", "Affirm", "Chime", "Marqeta", "Toast", "Klaviyo",
    "Chainalysis",
    # Dev tools / infrastructure
    "HashiCorp", "Elastic", "Datadog", "PagerDuty", "CircleCI", "Sentry",
    "LaunchDarkly", "Postman", "Retool", "Netlify", "MongoDB",
    "Confluent", "Snowflake", "Databricks", "DigitalOcean", "Cloudflare",
    "Fastly",
    # Security
    "CrowdStrike", "SentinelOne", "Snyk", "1Password", "Okta",
    # Productivity / SaaS
    "Zapier", "Doist", "Buffer", "Automattic", "Airtable", "Miro",
    "Loom", "Calendly", "monday.com", "Asana", "Webflow", "Squarespace",
    "Grammarly",
    # Marketing / customer engagement
    "Braze", "Amplitude", "Mixpanel", "Segment", "Intercom", "Zendesk",
    "HubSpot",
    # People / HR tech (recruiter-to-recruiter irony intended)
    "Deel", "Rippling", "Gusto",
    # Consumer / social
    "Reddit", "Dropbox", "Atlassian",
    # Education
    "Duolingo", "Coursera", "Udemy",
]

GREENHOUSE_URL = "https://boards-api.greenhouse.io/v1/boards/{slug}/jobs"
LEVER_URL = "https://api.lever.co/v0/postings/{slug}?mode=json"
ASHBY_URL = "https://api.ashbyhq.com/posting-api/job-board/{slug}"

REQUEST_TIMEOUT = 10
DELAY_BETWEEN_REQUESTS = 0.3  # be a reasonable citizen of these free APIs


def slug_candidates(name: str) -> list[str]:
    """Generate plausible board-token slugs for a company name."""
    base = name.lower().strip()
    # Strip common corporate suffixes that are usually dropped from slugs.
    base = re.sub(r"\b(inc|llc|corp|co|company|group|global)\b\.?", "", base)
    no_space = re.sub(r"[^a-z0-9]", "", base)
    hyphenated = re.sub(r"[^a-z0-9]+", "-", base).strip("-")
    candidates = {no_space, hyphenated}
    return [c for c in candidates if c]


def check_greenhouse(slug: str) -> int | None:
    try:
        resp = requests.get(
            GREENHOUSE_URL.format(slug=slug), timeout=REQUEST_TIMEOUT
        )
        if resp.status_code == 200:
            jobs = resp.json().get("jobs", [])
            if jobs:
                return len(jobs)
    except requests.RequestException:
        pass
    return None


def check_lever(slug: str) -> int | None:
    try:
        resp = requests.get(LEVER_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            jobs = resp.json()
            if isinstance(jobs, list) and jobs:
                return len(jobs)
    except requests.RequestException:
        pass
    return None


def check_ashby(slug: str) -> int | None:
    try:
        resp = requests.get(ASHBY_URL.format(slug=slug), timeout=REQUEST_TIMEOUT)
        if resp.status_code == 200:
            jobs = resp.json().get("jobs", [])
            if jobs:
                return len(jobs)
    except requests.RequestException:
        pass
    return None


def main() -> None:
    confirmed = []
    unmatched = []

    seen_slugs_per_company = {}
    for company in COMPANIES:
        candidates = slug_candidates(company)
        seen_slugs_per_company[company] = candidates

    for company, candidates in seen_slugs_per_company.items():
        found = False
        for slug in candidates:
            for platform, checker in (
                ("greenhouse", check_greenhouse),
                ("lever", check_lever),
                ("ashby", check_ashby),
            ):
                count = checker(slug)
                time.sleep(DELAY_BETWEEN_REQUESTS)
                if count is not None:
                    confirmed.append((company, platform, slug, count))
                    print(f"MATCH  {company!r} -> {platform} (slug={slug!r}), {count} open jobs")
                    found = True
                    break
            if found:
                break
        if not found:
            unmatched.append(company)

    print("\n" + "=" * 60)
    print(f"CONFIRMED MATCHES ({len(confirmed)}):")
    for company, platform, slug, count in confirmed:
        print(f"  {company:<25} {platform:<12} slug={slug!r:<20} {count} jobs")

    print(f"\nNO MATCH on Greenhouse/Lever/Ashby ({len(unmatched)}):")
    for company in unmatched:
        print(f"  {company}")


if __name__ == "__main__":
    main()
