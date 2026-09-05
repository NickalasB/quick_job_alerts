# Director / TA Job Alert

Watches for **Director-level** or **Senior Recruiter / Talent Acquisition**
roles in **Portland, OR or Remote**, and pushes an instant notification to
your wife's phone the moment a new one shows up.

- **Source:** Adzuna job search API (free tier)
- **Delivery:** [ntfy.sh](https://ntfy.sh) push notifications
- **Runs on:** GitHub Actions, every 15 minutes, for free

No app to build, sign, or sideload — just an app install and two free API
sign-ups.

## Setup (about 10 minutes)

### 1. Get a free Adzuna API key
1. Go to https://developer.adzuna.com/ and sign up.
2. Create an app — you'll get an **App ID** and **App Key**. Keep this page open.

### 2. Set up ntfy on your wife's phone
1. Install the **ntfy** app: [iOS](https://apps.apple.com/us/app/ntfy/id1625396347) / [Android](https://play.google.com/store/apps/details?id=io.heckel.ntfy).
2. In the app, tap **+** to subscribe to a topic. Pick a random, hard-to-guess
   topic name (anyone who knows the exact name can send to it) — e.g.
   `jane-ta-jobs-7fq2x`. Write it down.
3. That's it — no account needed.

### 3. Create a GitHub repo for this script
1. Create a new **private** GitHub repo (e.g. `job-alert`).
2. Upload all the files from this project into it, keeping the folder
   structure (`.github/workflows/check_jobs.yml` must stay at that exact path).

### 4. Add your secrets
In the repo: **Settings → Secrets and variables → Actions → New repository secret**.
Add three secrets:
| Name | Value |
|---|---|
| `ADZUNA_APP_ID` | from step 1 |
| `ADZUNA_APP_KEY` | from step 1 |
| `NTFY_TOPIC` | the topic name from step 2 |

### 5. Turn it on
Go to the **Actions** tab in your repo, enable workflows if prompted, then
run **"Check for new jobs"** once manually (Actions tab → select the
workflow → "Run workflow") to confirm it works. After that it runs itself
every 15 minutes.

## Tuning it

Open `job_alert.py`:
- `ROLE_TERMS` / `LEVEL_PATTERN` / `ROLE_PATTERN` / `EXCLUDE_PATTERN` — adjust
  which titles count as a match.
- `MAX_DAYS_OLD` — how far back Adzuna looks each run (kept small since this
  runs frequently).
- `PORTLAND_DISTANCE_MILES` — radius around Portland to include.

## Known limitation

Adzuna doesn't tag postings by "is this a tech company," only by job
category — and recruiting roles get filed under HR regardless of the
company's industry. So you'll get some non-tech companies in the mix; the
title/location filtering handles the rest of the precision. If this gets
noisy, tell me and I'll add a company-name allow/block-list layer.
