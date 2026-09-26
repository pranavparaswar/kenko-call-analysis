# Kenko Call Analysis — Project Plan

## What this is
An automated system that takes every recorded Callyzer call, transcribes it
(Sarvam, with speaker diarization), analyzes it (summary, sentiment, QA score,
and a role-specific must-cover checklist for Sales vs Nutrition), and surfaces it
on a dashboard for weekly review and coaching.

## Where we are today (working, local)
`pipeline.py` already does the full chain, proven end-to-end on real calls:

- Pulls calls from Callyzer (`/call-log/history`), handles pagination + rate limits.
- Downloads each recording; **one Sarvam job per call** so a transcript can never
  be attached to the wrong call (this was a real bug we found and fixed).
- Transcribes with `saaras:v3` (batch, diarized, up to 2h/file), analyzes with
  `sarvam-105b`.
- Routes each call to Sales or Nutrition via `employees.csv` (per-rep role tags);
  Ops/other roles are skipped.
- Scores each call against the role's must-cover checklist.
- Outputs `results.json` + a self-contained `dashboard.html` (filters, per-rep
  compliance, sentiment, transcript viewer for verification, client name + number).

Commands: `--employees` (build role sheet), `--check --days N` (connection +
per-rep recording coverage), `--days N` (full run), `--reprocess` (re-run from
local audio, Sarvam only), `--rebuild` (regenerate dashboard from results.json).

---

## Phase 1 — Every Callyzer call → live dashboard, on GitHub, live

**Goal:** the pipeline runs itself on a schedule and publishes an always-current
dashboard at a URL. No laptop, no manual runs.

**Prerequisites (not code):**
1. **Callyzer token** — resolve the login/subscription issue, generate a valid API
   token. (Current blocker.)
2. **Enable call recording on every rep's phone** in Callyzer — reps at 0% coverage
   (see `--check`) produce no audio and can't be analyzed.
3. **Finalize the two must-cover checklists** (Sales + Nutrition) with real wording.

**Build steps:**
1. **GitHub repo** — commit `pipeline.py`, `dashboard_template.html`, `employees.csv`,
   `requirements.txt`, README. `.gitignore` excludes `audio/`, `transcripts_cache.json`,
   and any keys.
2. **Incremental processing** — track the last-processed timestamp so each run only
   handles new calls (efficient, avoids re-work). Keep transcript cache so nothing
   is ever re-transcribed/re-charged.
3. **Scheduled automation** — GitHub Actions workflow runs the pipeline on a cron
   (e.g. every hour or each morning). `CALLYZER_TOKEN` and `SARVAM_API_KEY` stored
   as **GitHub repository secrets** (never in code).
4. **Publish the dashboard** — the Action writes `results.json` + `dashboard.html`
   and deploys to **GitHub Pages** (free). Result: a live URL the team opens
   anytime, refreshed automatically.
5. **Data** — `results.json` is committed/published each run and accumulates history.

**Phase 1 outcome:** every recorded Callyzer call, across all reps with recording
on, appears on a hosted dashboard that updates on a schedule — no manual steps.

**Cost:** Sarvam transcription ≈ ₹45/hour of audio (diarized). ~113 recorded
calls/week ≈ ₹400–800/week; the ~75,000 credits cover well over a year.

---

## Phase 2 — Hand off to tech team (FlutterFlow + Firebase, real-time)

**Goal:** move from scheduled batch to real-time, app-based, productionized —
owned by your tech team.

**Architecture:**
```
Call ends → Callyzer webhook → Firebase Cloud Function
   → download recording → Sarvam (transcribe + analyze)
   → write result to Firestore
→ FlutterFlow app (and web dashboard) read Firestore live
```

**Steps for the tech team:**
1. **Callyzer webhook** → a **Cloud Function** endpoint (public HTTPS) receives each
   completed call. (Handle the short delay before a recording is available.)
2. **Port the pipeline logic** into Cloud Functions (Firebase supports Python) —
   the transcription, role routing, checklists, and analysis prompts all carry over
   directly from `pipeline.py`.
3. **Sarvam webhook** — submit the recording to Sarvam with a callback so the
   function isn't blocked waiting; a second function receives "done" and runs the
   analysis.
4. **Firestore** as the datastore (replaces `results.json`) — one document per call.
5. **FlutterFlow app** reads Firestore collections → live dashboard/app for the
   team, with the same views (per-rep compliance, sentiment, transcript, checklist).
6. **Auth & access control** for staff.

**Hand-off package (what the tech team gets):**
- `pipeline.py` as the proven reference implementation (all logic + prompts).
- Data model: the `results.json` schema maps 1:1 to Firestore documents.
- API notes: Callyzer endpoints, Sarvam batch + webhook, models (`saaras:v3`,
  `sarvam-105b`), the one-job-per-call rule, and the role-routing precedence.
- The finalized must-cover checklists and `employees.csv` role map.

**Phase 2 outcome:** near-real-time — a call ends and its analysis appears in the
app within minutes — with no scheduled batch, owned and maintained in your stack.

---

## Open items / decisions
- Resolve Callyzer login + confirm subscription/API access is active.
- Enable call recording on all reps' devices.
- Provide final must-cover wording for Sales and Nutrition.
- Phase 1 hosting: GitHub Pages (recommended, free) vs. another static host.
- Phase 2 timing: when to bring the tech team in.
