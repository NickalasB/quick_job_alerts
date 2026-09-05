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
