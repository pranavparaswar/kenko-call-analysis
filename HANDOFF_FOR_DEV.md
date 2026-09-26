# Kenko Call Analysis — Handoff to Dev (Firebase + Flutter)

Paste this whole file into your Claude project's knowledge/custom instructions
to get full context in one shot. It's the current, accurate state as of this
handoff — more current than some of the older docs in `docs/`, which describe
earlier planning stages.

## What this is
A pipeline for The Kenko Life (nutrition/health company) that pulls recorded
sales/nutrition calls, transcribes them, and uses AI to: detect the rep's role,
classify the call type (e.g. onboarding / consultation / escalation), score it
against that type's must-cover checklist, and produce a summary + sentiment +
QA score. Currently running as a proven batch script (`pipeline.py`) with a
working HTML dashboard, on **real production data** (108 calls analyzed as of
this handoff, spanning ~1.5 weeks).

## Your job: Phase 2 — real-time, Firebase + Flutter
```
Call ends → Callyzer webhook → Firebase Cloud Function
   → download recording → Sarvam (transcribe + analyze)
   → write result to Firestore
→ Flutter app (and/or web dashboard) reads Firestore live
```
This replaces the current on-demand batch model (a "pull last N days" script
run manually/on a schedule) with real-time, per-call processing.

## Where to start
`backend_firebase_optional/` is your starting point — a near-complete Cloud
Functions scaffold, **just brought up to date** with the current scoring logic
(it previously used an older, simpler checklist — now fixed to match
`pipeline.py` exactly):
- `main.py` — the two Cloud Functions: `callyzer_webhook` (call ends →
  transcribe), `sarvam_webhook` (transcription done → analyze → write to
  Firestore).
- `core.py` — pure logic: role routing, the call-type taxonomy + weighted
  scoring, the analysis prompt, JSON parsing. No Firebase deps — easy to
  unit-test standalone.
- `seed_roles.py` — one-off script to load `employees.csv` into Firestore.
- `README_BACKEND.md` — deploy steps, Firestore data model, and a **"VERIFY"**
  section listing the few spots that depend on your exact Callyzer webhook
  payload and `sarvamai` SDK version — confirm these against live data before
  going live.

`pipeline.py` (the batch script, one level up) is the **proven reference
implementation** — when in doubt about how a step should behave, that's the
source of truth; `core.py`'s logic is a direct port of it.

## The scoring model (important — read this before touching checklists)
Each `(role, call_type)` pair has its own checklist. Each checkpoint has a
**weight**: `3` = core to the call's purpose, `2` = important, `1` = hygiene.

```
qa_score = round(100 * sum(weight of checkpoints hit) / sum(weight of all checkpoints))
```

This number is **derived**, not the model's own opinion — the model just
returns `must_say: {checkpoint_id: true/false}` and `weighted_score()` computes
the actual score from that. The model's own raw guess is kept separately as
`qa_score_model`, for comparison only — never display that as *the* score.

Taxonomy: nutrition → `onboarding` / `consultation` / `escalation`; sales →
`enquiry` / `follow_up` / `closing` / `escalation`. Full checkpoint wording and
weights are in `CALL_TYPES` (`pipeline.py` and `core.py`, kept identical).
Editing checkpoints = editing that dict; no other code changes needed.

## Firestore data model
See `backend_firebase_optional/README_BACKEND.md` → "Firestore data model" for
the full field list. The one thing to get right: **`status == "error"`** means
the model's JSON reply failed to parse for that call — `qa_score` is `null` and
`call_type` is `""`. This is a system/formatting failure, **not the rep's
fault** — exclude these from every average (QA score, compliance %, sentiment
split) and show them in a distinct "needs re-analysis" state instead of a
misleading 0. (We hit this exact bug in the batch pipeline — the model
sometimes wraps JSON in a markdown fence or emits an invalid `\'` escape; both
are now tolerated in `core.py`'s `parse_json()`, but any deeper JSON failure
still needs this null-scoring treatment rather than defaulting to 0.)

## Reference artifacts included in this package
- `dashboard.html` — the current working dashboard (open directly in a
  browser). Shows exactly what the Flutter app's review screen should
  functionally replicate: role/call-type tabs, per-rep compliance, sentiment
  split, calls table with search (name/phone/summary), and a per-call detail
  view with the checklist, transcript, and audio reference.
- `results.json` — **real production data** (108 analyzed calls), matching the
  Firestore document shape you'll be writing. Use it to seed a dev/staging
  Firestore so the Flutter UI has real-shaped data to build against without
  waiting on the pipeline.
- `sample_results.json` — 64 fake demo calls, safe to use in any public repo
  or CI.
- `dashboard_template.html` — the dashboard's source (a plain HTML/JS template
  the batch pipeline injects data into). Not needed for Flutter, but useful
  as a spec for the UI behavior described above.
- `docs/INTEGRATION.md` — contract for an **alternate**, already-working
  on-demand REST API (`server.py`, also in `backend_firebase_optional/` despite
  the folder name — it's unrelated to the Firebase webhook path, just an
  ordinary FastAPI service). Not what you're building, but useful if you want
  a quick way to cross-check outputs against the batch pipeline during
  migration — `GET /calls` on that service returns the same shape as
  `results.json`.
- `docs/PLAN.md` — the original phase 1/phase 2 planning doc.

**Not included:** `audio/` (113 real call recordings — large, and Callyzer/
Sarvam remain the source of truth for audio; your webhook re-fetches per call
anyway). `.env` (real API keys — see below).

## Keys you'll need (get directly from the business owner, never in code)
- `CALLYZER_TOKEN` — Callyzer dashboard → generate API token.
- `SARVAM_API_KEY` — platform.sarvam.ai → API Keys.
- Store both as Firebase Functions secrets (`firebase functions:secrets:set`),
  never committed. `.env.example` shows the two variable names.

## Known gotchas (already hit and fixed once — don't reintroduce)
- **One Sarvam job per recording, never batch multiple files into one job** —
  batching caused transcripts to map to the wrong calls in testing.
- **Roles come only from the `employees` collection** (seeded from
  `employees.csv`), never AI-guessed — guessing labeled the same rep
  inconsistently across calls.
- **`call_type` vs `direction`**: Callyzer's own `call_type` field is actually
  Incoming/Outgoing (call direction) — don't confuse it with the AI-classified
  call type. `main.py` now stores these as separate fields (`direction` and
  `call_type`).
- Only ~30% of calls had recordings in testing — recording must be enabled per
  rep, per device, in the Callyzer app; reps at 0% coverage will simply never
  get analyzed.

## Suggested first steps
1. Read `backend_firebase_optional/README_BACKEND.md` fully (deploy steps +
   "VERIFY" spots).
2. Seed a dev Firestore project with `results.json` (real data) so you can
   build the Flutter review screens against real-shaped documents immediately.
3. Deploy the two Cloud Functions to a dev project, confirm the "VERIFY" spots
   against one real Callyzer webhook payload and one real Sarvam job.
4. Wire the Callyzer webhook, do one end-to-end test call, compare its
   Firestore doc against the equivalent call in `results.json`/`pipeline.py`'s
   output for the same recording.
