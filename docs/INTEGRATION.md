# Kenko Call Analysis — API Integration Guide

**This service is the single integration point.** Your app talks to it over HTTP
and gets JSON. You don't need the pipeline internals, Python knowledge, or any
loose files — run one container, call these endpoints.

## What it does
Pulls calls from Callyzer, transcribes them (Sarvam, diarized), and for each call
the AI (1) detects the **role** (nutrition vs sales, from the employee tag),
(2) classifies the **call type** (e.g. onboarding / consultation / escalation),
and (3) scores that type's **checkpoints**, plus summary, sentiment and a QA score.

## Run it (one container)
```bash
docker build -t kenko-api .
docker run -p 8080:8080 \
  -e CALLYZER_TOKEN="<callyzer token>" \
  -e SARVAM_API_KEY="<sarvam key>" \
  kenko-api
```
API is now at `http://<host>:8080`. (Deploy the container anywhere: Cloud Run,
Render, a VM — it's a standard FastAPI/uvicorn app.)

## Endpoints

### `GET /config`
The taxonomy the app renders checklists from. Call once on load.
```json
{
  "roles": ["nutrition", "sales"],
  "taxonomy": {
    "nutrition": {
      "onboarding":   {"desc": "...", "checkpoints": [{"id":"greet","label":"Greet + identify","criteria":"..."}, ...]},
      "consultation": {"desc": "...", "checkpoints": [...]},
      "escalation":   {"desc": "...", "checkpoints": [...]}
    },
    "sales": { "enquiry": {...}, "follow_up": {...}, "closing": {...}, "escalation": {...} }
  }
}
```

### `POST /pull`  — the "pull last N days" button
```json
// request
{ "days": 7 }
// response
{ "run_id": "a1b2c3d4e5f6", "status": "queued" }
```
Runs in the background (transcription takes minutes). Poll the run:

### `GET /runs/{run_id}`
```json
{ "status": "running", "message": "processing last 7 days" }
// status: queued | running | done | error
```

### `GET /calls?role=&call_type=&status=&limit=`
Returns the analyzed calls (newest first). All query params optional.
```json
[
  {
    "call_id": "C1042",
    "role": "nutrition",
    "call_type": "consultation",          // AI-classified type
    "date": "2026-07-30",
    "rep": "Disha",
    "client": "Himani T",                  // from Callyzer contact
    "client_number": "91-9632081711",
    "duration_sec": 353,
    "direction": "Outgoing",               // Callyzer incoming/outgoing
    "sentiment": "Neutral",
    "sentiment_reason": "...",
    "qa_score": 72,
    "qa_notes": "...",
    "must_say": { "greet": true, "progress": false, "weight": false, ... },  // keyed by this call_type's checkpoint ids
    "action_items": ["Share updated plan"],
    "transcript": "Speaker 0: ...\nSpeaker 1: ...",
    "status": "done"
  }
]
```

### `GET /health`
`{ "ok": true, "keys_configured": true }`

## How the app uses it
1. On load, `GET /config` → cache the taxonomy (labels for checkpoints per role+type).
2. Screen with a **"Pull last 1 / 7 / 30 days"** button → `POST /pull {days}` →
   poll `GET /runs/{id}` until `done`.
3. List/review screen → `GET /calls` (filter by `role`, `call_type`, etc.).
   For each call, render the checklist by looking up
   `taxonomy[call.role][call.call_type].checkpoints` and marking each id against
   `call.must_say`.

## Data model notes
- `must_say` keys always match the checkpoints of that call's `call_type` (from
  `/config`). Different call types have different checkpoints — always look up by
  `call_type`.
- `role` comes from the employee's tag; `call_type` is AI-classified from the
  transcript.
- Roles that aren't analyzed (e.g. Ops) don't appear in `/calls`.

## Editing checklists / call types
The taxonomy lives in `pipeline.py` (`CALL_TYPES`). To add a call type or change
checkpoints, edit that dict and redeploy — `/config` and scoring update
automatically. (Can be moved to a DB/config service later if you want no-redeploy edits.)

## Notes
- Transcription: Sarvam `saaras:v3` batch, diarized, one job per call (exact
  call↔transcript mapping). Analysis: `sarvam-105b`.
- Only calls that have a recording in Callyzer are analyzed.
- `results.json` in the working dir is the store; swap for a DB if you prefer.
