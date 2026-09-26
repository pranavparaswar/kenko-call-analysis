"""
Kenko Call Analysis — real-time webhook backend (Firebase Cloud Functions, Python).

Flow:
  Call ends -> Callyzer webhook -> callyzer_webhook()
     -> download recording -> submit to Sarvam (1 job, diarized) with a callback
     -> write calls/{call_id} = {status: "processing", ...metadata}
  Sarvam finishes -> POSTs to sarvam_webhook()
     -> download transcript -> analyze -> update calls/{call_id} = {status: "done", ...analysis}
  FlutterFlow / web dashboard read the `calls` collection from Firestore (live).

Deploy: see README_BACKEND.md. Search this file for "VERIFY" — those few spots
depend on the exact Callyzer webhook payload and Sarvam SDK, which the team should
confirm against live responses.
"""
import os
import json
import tempfile
import datetime as dt

import requests
from firebase_functions import https_fn, options
from firebase_admin import initialize_app, firestore

import core

initialize_app()

CALLYZER_TOKEN = os.environ.get("CALLYZER_TOKEN", "")
SARVAM_WEBHOOK_TOKEN = os.environ.get("SARVAM_WEBHOOK_TOKEN", "")   # shared secret
# Public URL of THIS deployment's sarvam_webhook function (set after first deploy):
SARVAM_CALLBACK_URL = os.environ.get("SARVAM_CALLBACK_URL", "")

# cache roles per warm instance; refresh_roles() re-reads Firestore
_ROLES = None


def db():
    return firestore.client()


def load_roles():
    """Build {norm_number: role, name_lower: role} from the `employees` collection."""
    global _ROLES
    if _ROLES is not None:
        return _ROLES
    m = {}
    for doc in db().collection("employees").stream():
        d = doc.to_dict() or {}
        role = core.normalize_role(d.get("role"))
        if not role:
            continue
        if d.get("emp_number"):
            m[core.norm_num(d["emp_number"])] = role
        if d.get("emp_name"):
            m[d["emp_name"].strip().lower()] = role
    _ROLES = m
    return m


# ---------------------------------------------------------------------------
# 1) Callyzer webhook — a call has completed
# ---------------------------------------------------------------------------
@https_fn.on_request(memory=options.MemoryOption.MB_512, timeout_sec=300)
def callyzer_webhook(req: https_fn.Request) -> https_fn.Response:
    try:
        payload = req.get_json(silent=True) or {}
    except Exception:
        payload = {}

    # VERIFY: adapt these keys to Callyzer's actual webhook payload. Callyzer's
    # call-log objects use: id, emp_number, emp_country_code, emp_name,
    # client_name, client_number, client_country_code, duration, call_type,
    # call_date, call_time, call_recording_url.
    call = payload.get("call") or payload.get("data") or payload
    call_id = str(call.get("id") or call.get("call_id") or "")
    if not call_id:
        return https_fn.Response("no call id", status=400)

    emp_number = call.get("emp_number", "")
    emp_name = call.get("emp_name", "")
    role = core.role_for(emp_number, emp_name, load_roles())

    base_doc = {
        "call_id": call_id,
        "role": role or "unknown",
        "date": (call.get("call_date") or ""),
        "rep": emp_name or emp_number or "Unknown",
        "client": call.get("client_name") or call.get("client_number") or "Unknown",
        "client_number": _full_number(call.get("client_country_code"), call.get("client_number")),
        "duration_sec": call.get("duration", 0),
        # Callyzer's own "call_type" field is actually Incoming/Outgoing (direction),
        # not the AI-classified call type (onboarding/consultation/...). Keep them
        # separate — "call_type" below is set once analysis finishes.
        "direction": call.get("call_type", ""),
        "call_type": "",
        "updated_at": dt.datetime.utcnow().isoformat(),
    }

    # Skip non-analyzed roles (Ops etc.) and untagged reps — store for visibility.
    if role not in core.ANALYZED_ROLES:
        base_doc["status"] = "skipped_role"
        db().collection("calls").document(call_id).set(base_doc, merge=True)
        return https_fn.Response("skipped (role not analyzed)", status=200)

    rec_url = call.get("call_recording_url")
    if not rec_url:
        # Recording may not be ready yet. Mark it; a scheduled re-check function
        # (TODO) or a later webhook can pick it up.
        base_doc["status"] = "awaiting_recording"
        db().collection("calls").document(call_id).set(base_doc, merge=True)
        return https_fn.Response("no recording yet", status=200)

    # download recording
    audio_path = _download(rec_url, call_id)
    if not audio_path:
        base_doc["status"] = "download_failed"
        db().collection("calls").document(call_id).set(base_doc, merge=True)
        return https_fn.Response("download failed", status=200)

    # submit to Sarvam (single-file job) with a callback to sarvam_webhook
    try:
        job_id = _submit_sarvam(audio_path)
    except Exception as e:
        base_doc["status"] = "stt_submit_failed"
        base_doc["error"] = str(e)[:300]
        db().collection("calls").document(call_id).set(base_doc, merge=True)
        return https_fn.Response(f"stt submit failed: {e}", status=200)

    base_doc["status"] = "processing"
    base_doc["job_id"] = job_id
    db().collection("calls").document(call_id).set(base_doc, merge=True)
    # job_id -> call_id map so the Sarvam callback can find the call
    db().collection("jobs").document(job_id).set({"call_id": call_id})
    return https_fn.Response("accepted", status=200)


# ---------------------------------------------------------------------------
# 2) Sarvam webhook — transcription finished
# ---------------------------------------------------------------------------
@https_fn.on_request(memory=options.MemoryOption.MB_512, timeout_sec=300)
def sarvam_webhook(req: https_fn.Request) -> https_fn.Response:
    # validate shared secret
    token = req.headers.get("X-SARVAM-JOB-CALLBACK-TOKEN", "")
    if SARVAM_WEBHOOK_TOKEN and token != SARVAM_WEBHOOK_TOKEN:
        return https_fn.Response("forbidden", status=403)

    payload = req.get_json(silent=True) or {}
    job_id = payload.get("job_id")
    state = str(payload.get("job_state", "")).lower()
    if not job_id:
        return https_fn.Response("no job id", status=400)

    jm = db().collection("jobs").document(job_id).get()
    call_id = (jm.to_dict() or {}).get("call_id") if jm.exists else None
    if not call_id:
        return https_fn.Response("unknown job", status=200)

    call_ref = db().collection("calls").document(call_id)

    if state == "failed":
        call_ref.set({"status": "stt_failed", "updated_at": dt.datetime.utcnow().isoformat()},
                     merge=True)
        return https_fn.Response("noted failure", status=200)

    if state not in ("completed", "success", "succeeded"):
        return https_fn.Response("ignored (not final)", status=200)

    # download the transcript for this job and analyze
    try:
        transcript = _fetch_transcript(job_id)
    except Exception as e:
        call_ref.set({"status": "download_failed", "error": str(e)[:300],
                      "updated_at": dt.datetime.utcnow().isoformat()}, merge=True)
        return https_fn.Response(f"download failed: {e}", status=200)

    call = call_ref.get().to_dict() or {}
    role = call.get("role", "sales")
    try:
        analysis = core.analyze(transcript, role, rep_name=call.get("rep", ""))
    except Exception as e:
        call_ref.set({"status": "analysis_failed", "transcript": transcript,
                      "error": str(e)[:300], "updated_at": dt.datetime.utcnow().isoformat()},
                     merge=True)
        return https_fn.Response(f"analysis failed: {e}", status=200)

    # A call whose analysis failed to parse gets NO call_type guess and NO
    # score (qa_score stays None) instead of a fake 0/all-missed — that 0 would
    # look like the rep's fault when it's actually a model/formatting failure.
    # FlutterFlow should filter these into a "needs re-analysis" view, not
    # average them into compliance/QA numbers.
    failed = analysis.get("_analysis_failed", False)
    call_type = "" if failed else core.valid_type(role, analysis.get("call_type", ""))
    must_say = analysis.get("must_say", {})
    call_ref.set({
        "status": "error" if failed else "done",
        "transcript": transcript,
        "call_type": call_type,
        "summary": analysis.get("summary", ""),
        "sentiment": analysis.get("sentiment", "Neutral"),
        "sentiment_reason": analysis.get("sentiment_reason", ""),
        # qa_score is DERIVED from weighted checkpoint coverage, so it always
        # matches the checklist. The model's own rating is kept as qa_score_model.
        "qa_score": None if failed else core.weighted_score(role, call_type, must_say),
        "qa_score_model": analysis.get("qa_score", 0),
        "qa_notes": analysis.get("qa_notes", ""),
        "must_say": must_say,
        "action_items": analysis.get("action_items", []),
        "updated_at": dt.datetime.utcnow().isoformat(),
    }, merge=True)
    return https_fn.Response("done", status=200)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _full_number(cc, num):
    return f"{cc}-{num}" if cc and num else (num or "")


def _download(url, call_id):
    for hdrs in ({"Authorization": f"Bearer {CALLYZER_TOKEN}"}, {}):
        try:
            r = requests.get(url, headers=hdrs, timeout=120)
            if r.status_code == 200 and r.content:
                p = os.path.join(tempfile.gettempdir(), f"{call_id}.mp3")
                with open(p, "wb") as f:
                    f.write(r.content)
                return p
        except Exception:
            continue
    return None


def _submit_sarvam(audio_path):
    """Submit a single-file diarized batch job with a callback. Returns job_id.
    VERIFY the SDK surface against your sarvamai version."""
    from sarvamai import SarvamAI, BulkJobCallbackParams
    client = SarvamAI(api_subscription_key=os.environ.get("SARVAM_API_KEY", ""))
    job = client.speech_to_text_job.create_job(
        model=core.SARVAM_STT_MODEL,
        mode="transcribe",
        language_code=core.SARVAM_LANGUAGE,
        with_diarization=True,
        num_speakers=2,
        callback=BulkJobCallbackParams(url=SARVAM_CALLBACK_URL,
                                       auth_token=SARVAM_WEBHOOK_TOKEN),
    )
    job.upload_files(file_paths=[audio_path])
    job.start()
    # VERIFY: attribute that holds the job id (job.job_id / job.id).
    return getattr(job, "job_id", None) or getattr(job, "id", None)


def _fetch_transcript(job_id):
    """Download the finished job's output and return diarized transcript text.
    VERIFY against your sarvamai version — reconstruct/lookup the job by id,
    download outputs, read the single output JSON."""
    from sarvamai import SarvamAI
    client = SarvamAI(api_subscription_key=os.environ.get("SARVAM_API_KEY", ""))
    outdir = tempfile.mkdtemp()
    # Option A (SDK): job = client.speech_to_text_job.get(job_id); job.download_outputs(outdir)
    # Option B (REST): POST /speech-to-text/job/v1/download-files {job_id, files:["0.json"]}
    job = client.speech_to_text_job.get_job(job_id) if hasattr(
        client.speech_to_text_job, "get_job") else None
    if job is not None:
        job.download_outputs(output_dir=outdir)
    else:
        raise RuntimeError("Adapt _fetch_transcript to your sarvamai SDK (get job by id).")
    import glob
    js = glob.glob(os.path.join(outdir, "*.json"))
    if not js:
        return ""
    data = json.loads(open(js[0]).read())
    return core.format_transcript(data)
