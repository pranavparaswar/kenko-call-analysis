# Kenko Call Analysis — Real-time Backend (for the tech team)

A near-complete, event-driven backend. A Callyzer webhook fires when a call ends;
we transcribe it (Sarvam, diarized), analyze it (summary, sentiment, QA score,
role-specific must-cover checklist), and write the result to **Firestore**. Your
FlutterFlow app (and any web dashboard) read the `calls` collection live.

```
Call ends → Callyzer webhook → callyzer_webhook()
   → download recording → Sarvam single-file job (callback = sarvam_webhook)
   → Firestore calls/{id} = {status:"processing", ...metadata}
Sarvam done → sarvam_webhook() → download transcript → analyze
   → Firestore calls/{id} = {status:"done", transcript, summary, sentiment, qa, must_say...}
FlutterFlow reads `calls` (live)
```

## Files
| File | Purpose |
|------|---------|
| `main.py` | The two Cloud Functions: `callyzer_webhook`, `sarvam_webhook` |
| `core.py` | Pure logic: role routing, must-cover checklists, analysis (sarvam-105b) |
| `seed_roles.py` | One-off: load `employees.csv` → Firestore `employees` collection |
| `requirements.txt` | Python deps |

> The batch `pipeline.py` in the parent folder is the **proven reference
> implementation** — same transcription/analysis logic, run as a script. Use it to
> sanity-check outputs and to backfill historical calls.

## Prerequisites
- A Firebase project with **Firestore** and **Cloud Functions (Python)** enabled
  (Blaze plan — functions that call external APIs need it).
- `firebase-tools` CLI (`npm i -g firebase-tools`), `firebase login`.
- A valid **Callyzer API token** and **Sarvam API key**.

## Deploy
1. **Init** (in this `backend/` folder):
   ```bash
   firebase init functions      # choose Python, this folder
   ```
2. **Set secrets / env** (never commit these):
   ```bash
   firebase functions:secrets:set CALLYZER_TOKEN
   firebase functions:secrets:set SARVAM_API_KEY
   firebase functions:secrets:set SARVAM_WEBHOOK_TOKEN   # any strong random string
   ```
   (Bind them to both functions, or use env config — see Firebase docs.)
3. **First deploy** (gets you the function URLs):
   ```bash
   firebase deploy --only functions
   ```
4. **Set the Sarvam callback URL** to the deployed `sarvam_webhook` URL, then redeploy:
   ```bash
   firebase functions:config:set   # or set env SARVAM_CALLBACK_URL = https://.../sarvam_webhook
   firebase deploy --only functions
   ```
5. **Seed roles** into Firestore:
   ```bash
   export GOOGLE_APPLICATION_CREDENTIALS=/path/to/serviceAccount.json
   python seed_roles.py ../employees.csv
   ```

## Wire the webhooks
- **Callyzer** → point its webhook at the deployed `callyzer_webhook` URL
  (Callyzer dashboard → Connectors → API & Webhook).
- **Sarvam** → already automatic: each job is created with `callback` = your
  `sarvam_webhook` URL + the shared `SARVAM_WEBHOOK_TOKEN` (validated on receipt).

## Firestore data model (what FlutterFlow reads)
Collection **`calls`**, document id = Callyzer call id:
```
call_id, role ("sales"|"nutrition"|"unknown"),
rep, client, client_number, date, duration_sec,
direction ("Incoming"|"Outgoing", from Callyzer),
call_type (AI-classified: nutrition -> "onboarding"|"consultation"|"escalation",
           sales -> "enquiry"|"follow_up"|"closing"|"escalation"; "" if analysis failed),
status ("processing"|"done"|"error"|"awaiting_recording"|"skipped_role"|"..._failed"),
transcript,
summary, sentiment, sentiment_reason,
qa_score (0-100, weighted checkpoint coverage — null if status=="error"),
qa_score_model (0-100, the model's own unvalidated rating, kept for comparison),
qa_notes,
must_say  (map: checkpoint_id -> bool, keyed by THIS call's call_type — always
           look up checkpoint labels from CALL_TYPES[role][call_type]),
action_items (array), updated_at
```
Collections **`employees`** (role map) and **`jobs`** (job_id → call_id, internal).

In FlutterFlow: back the review screen with the `calls` collection, filter by
`role` and `status == "done"`, sort by `date`. `must_say` renders as the
checklist — the taxonomy (role -> call_type -> checkpoints, with weights) is
`CALL_TYPES` in `core.py`, a byte-for-byte copy of `pipeline.py`'s `CALL_TYPES`
(the proven reference). **`status == "error"`** calls have `qa_score: null` and
`call_type: ""` — the model's JSON reply failed to parse. This is a system
failure, not the rep's fault: exclude these from every average (QA score,
compliance %, sentiment split) the same way the reference dashboard does, and
surface them in a "needs re-analysis" view instead. Re-running analysis (e.g.
re-invoking `sarvam_webhook` for that job, or a small backfill script that
calls `core.analyze()` again on the stored transcript) usually recovers them.

## The few "VERIFY" spots (search the code)
These depend on live payloads / SDK version — confirm once against real data:
1. **Callyzer webhook payload keys** (`main.py` `callyzer_webhook`) — map to the
   actual fields Callyzer sends. The call-log field names are:
   `id, emp_number, emp_country_code, emp_name, client_name, client_number,
   client_country_code, duration, call_type, call_date, call_recording_url`.
2. **Sarvam job id + job fetch** (`main.py` `_submit_sarvam`, `_fetch_transcript`)
   — confirm how your `sarvamai` version returns the job id and how to fetch a
   finished job's output by id (SDK `get_job`/`download_outputs`, or the REST
   `/speech-to-text/job/v1/download-files`).
3. **Recording delay** — if Callyzer's webhook can fire before the recording is
   uploaded, `call_recording_url` may be empty (`status:"awaiting_recording"`).
   Add a scheduled function to re-check those, or handle Callyzer's later event.

## Notes
- Transcription: `saaras:v3` batch, diarized (agent vs customer). Analysis:
  `sarvam-105b`. One job per call = each transcript maps to exactly one call.
- Cost: ~₹45/hour of audio (diarized). Billed per second.
- Checklists live in `core.py` (`CHECKLISTS`); move to a Firestore `config` doc if
  you want to edit them without redeploying.
