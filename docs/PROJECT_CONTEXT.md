# Kenko Call Analysis — Project Context (knowledge file)

Upload this to the Claude Project's knowledge, along with the code files listed
at the bottom. It captures the full state so any new chat has context.

## What it is
A pipeline for The Kenko Life that turns recorded Callyzer calls into coached QA
data. Flow:

```
Callyzer (poll API)  →  download recordings  →  Sarvam transcribe (diarized, 1 job/call)
   →  detect role (from employee tag)  →  AI classifies call TYPE
   →  score that type's checkpoints + summary + sentiment + QA score
   →  results.json  →  dashboard.html  AND  REST API (/calls) for the app
```

## Architecture decisions (and why)
- **On-demand pulls, not real-time.** A "pull last N days" button (via the API)
  runs the pipeline. We deliberately did NOT build webhooks / cloud functions /
  Firestore — weekly QA review doesn't need per-call real-time, and on-demand is
  far simpler to run and hand off.
- **One Sarvam job per call.** Batching multiple recordings into one Sarvam job
  caused transcripts to be mapped to the WRONG calls (found by comparing a
  transcript to its actual audio). Fixed by submitting one file per job — each
  transcript maps 1:1 to its call. Never revert to multi-file batching.
- **Roles from tags only.** Rep role (nutrition/sales) comes solely from
  employees.csv. AI role-guessing was removed because it labeled the same person
  differently on different calls.
- **Two-level classification.** Level 1 = role (who, from tag). Level 2 = call
  type (what, AI-classified from transcript). Each (role, call_type) has its own
  checkpoints.

## Models / APIs
- Callyzer: `https://api1.callyzer.co/api/v2.2`, Bearer token. Endpoints used:
  `/call-log/history` (calls), `/call-log/employee-summary` (employees). Response
  `result` is a list; employee numbers come as local number + separate
  `emp_country_code`. Rate-limited (429s) — pipeline backs off. Recording enabled
  PER DEVICE — only reps with recording on have audio to analyze.
- Sarvam: transcription = `saaras:v3` **Batch API** (diarized, up to 2h/file).
  Analysis = `sarvam-105b` chat completions (note: `sarvam-m` and `sarvam-30b`
  are deprecated). Billed ~₹45/hour of audio (diarized). ~75k credits ≈ 1+ year.

## Data model (results.json / API `/calls` object)
```
call_id, role ("nutrition"|"sales"), call_type (e.g. "onboarding"|"consultation"|
"escalation"|"enquiry"|"follow_up"|"closing"), date, rep, client (Callyzer contact
name — can be a nickname or "Unknown"), client_number, duration_sec,
direction (Callyzer Incoming/Outgoing), sentiment, sentiment_reason, qa_score,
qa_notes, must_say (map: checkpoint_id -> bool, keyed by THIS call_type's
checkpoints), action_items[], transcript (diarized "Speaker N: ..."), status
```
Taxonomy (role -> call_type -> {desc, checkpoints}) is in `results.json.taxonomy`
and from the API's `GET /config`. `must_say` keys always match the chosen
call_type's checkpoints — always look up labels by call_type.

## Roles & call types (taxonomy, editable in pipeline.py CALL_TYPES)
- nutrition: onboarding (first interaction), consultation (follow-ups),
  escalation (client has an issue).
- sales: enquiry, follow_up, closing, escalation.
Checkpoints per type are currently PLACEHOLDERS — to be replaced with Kenko's real
must-cover pointers.

## File inventory
- `pipeline.py` — the engine (CLI). Commands:
  `--employees` (build role sheet), `--check --days N` (connection + per-rep
  recording coverage), `--days N` (full run), `--reprocess` (re-run from local
  audio, Sarvam only, no Callyzer), `--rebuild` (regen dashboard from
  results.json), `--demo` (dashboard from sample data).
- `employees.csv` — rep -> role tags (sales/nutrition/ops). Ops & untagged are skipped.
- `dashboard_template.html` / `dashboard.html` — the UI (template + generated).
- `api/server.py` — the REST service the app integrates with (FastAPI).
- `api/INTEGRATION.md` — the API contract for the tech team.
- `api/Dockerfile`, `api/requirements.txt` — one-container deploy.
- `PLAN.md` — Phase 1 (live dashboard) / Phase 2 (FlutterFlow+Firebase) plan.
- `backend/` — an OPTIONAL real-time webhook (Firebase) scaffold; NOT the chosen
  path (we went on-demand). Ignore unless real-time is revived.
- `sample_results.json` — fake data for `--demo` (NOT real calls).
- `results.json` / `results.csv` / `transcripts_cache.json` / `audio/` — generated.

## Known gotchas (already handled)
- Transcript↔call mapping: fixed via one-job-per-call. If names in a summary don't
  match the Callyzer client, it's usually Callyzer's saved contact being a nickname
  — the phone number is shown for verification. Verify by playing audio/<call_id>.mp3.
- employees.csv parsing: reader handles BOM, delimiter (comma/semicolon), header
  case. `Loaded N tagged employees` prints at run start — if 0, the file/roles are off.
- Client "Unknown" = Callyzer had no saved name/number for that call.
- Only ~32% of calls had recordings in testing — reps need recording enabled per phone.

## How to run (on a machine with the keys)
```
pip install requests sarvamai
export CALLYZER_TOKEN=...   SARVAM_API_KEY=...
python3 pipeline.py --check --days 7      # coverage report
python3 pipeline.py --days 7              # full run -> dashboard.html
```
API: `cd api && pip install -r requirements.txt && uvicorn server:app --port 8080`
(or the Dockerfile).

## Open items / roadmap
1. Finalize real checkpoints per call type (both teams).
2. Confirm Callyzer token/subscription is active; enable recording on all phones.
3. Deploy the API (one container) for the tech team.
4. FlutterFlow app reads the API (`/config`, `/pull`, `/calls`).

## Files to also upload to the project knowledge
pipeline.py, employees.csv, api/server.py, api/INTEGRATION.md, PLAN.md
(dashboard_template.html optional).
