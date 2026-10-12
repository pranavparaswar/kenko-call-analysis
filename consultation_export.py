#!/usr/bin/env python3
"""
Consultation export
====================
Reads a Firebase-style consultations export zip (audio call recordings +
document.json metadata, some records text-only with no recording) and
produces a flat CSV in the "consultation_analysis" review format: one row
per consultation, transcribed (Sarvam STT) where a call recording exists,
with an LLM extraction pass (Sarvam chat) filling in the analysis columns.

Run:
    python3 consultation_export.py <zip_path> [--out FILE] [--limit N]
                                    [--only doc_id1,doc_id2,...]

Transcripts and per-record extractions are cached to
consultation_transcripts_cache.json / consultation_extract_cache.json so a
re-run after an interruption never re-transcribes or re-analyzes a record
that already succeeded.
"""

import os
import re
import csv
import json
import zipfile
import tempfile
import argparse
import datetime as dt
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
import pipeline  # reuse SARVAM config, retrying chat call, JSON parsing, transcript parsing

HERE = Path(__file__).parent
TRANSCRIPT_CACHE_PATH = HERE / "consultation_transcripts_cache.json"
EXTRACT_CACHE_PATH = HERE / "consultation_extract_cache.json"

FIELDNAMES = [
    "document_id", "filename", "client_name", "call_date", "email", "consultation_type",
    "weight_logged", "weight_details", "measurements_logged", "health_goals_history",
    "current_diet_recall", "kenko_meal_feedback", "progress_notes", "digestive_health",
    "energy_activity", "action_points", "issues", "plan_details", "next_checkin",
    "sentiment", "summary", "transcript",
]

EXTRACT_KEYS = [
    "weight_logged", "weight_details", "measurements_logged", "health_goals_history",
    "current_diet_recall", "kenko_meal_feedback", "progress_notes", "digestive_health",
    "energy_activity", "action_points", "issues", "plan_details", "next_checkin",
    "sentiment", "summary",
]

# Fields the nutritionist app captures directly as structured data on
# text-based check-ins (no call) — feed these to the model alongside the
# free-text notes so it isn't extracting from prose alone.
STRUCT_KEYS = [
    "current_weight", "weight_logged", "measurements_logged", "weight_change",
    "measurements_change", "outcome", "portion_adequacy", "diet_follow_pct",
    "weekend_adherence", "missed_meals", "meal_boredom", "taste_satisfaction",
    "flavour_issue", "texture_issue", "freshness_issue", "roti_quality",
    "contamination_issue", "oil_level", "issue_category", "selected_dish_name",
    "missing_components", "digestive_issues", "fiber_intake", "satiety",
    "cravings_control", "emotional_eating", "frequent_hunger", "fatigue_levels",
    "low_energy", "energy_shift", "evening_crash", "sleep_quality",
    "sleep_duration_change", "stress_level", "strength_perf", "poor_recovery",
    "protein_adequacy", "plateau", "visual_changes", "visible_changes",
    "motivation_level", "engagement_level", "client_friction", "risk_flag_renewal",
    "plan_adherence", "coach_interaction", "meal_satisfaction", "deviation_freq",
    "delivery_issues", "delivery_experience", "delivery_team", "health_check_day",
    "next_checkup_ts",
]

NAME_RE = re.compile(r"^([A-Za-z]+)_\d{6,}_")


def _load_json_cache(path):
    if path.exists():
        try:
            return json.loads(path.read_text())
        except Exception:
            return {}
    return {}


def _save_json_cache(path, data):
    path.write_text(json.dumps(data, indent=1, ensure_ascii=False))


def parse_ts(ts):
    if not ts:
        return None
    try:
        return dt.datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_call_date(ts):
    d = parse_ts(ts)
    return d.strftime("%Y-%m-%d %H:%M") if d else ""


def client_name_from_filename(filename):
    m = NAME_RE.match(filename)
    if m:
        return m.group(1)
    return Path(filename).stem.split("_")[0]


def client_name_from_email(email):
    if not email:
        return ""
    local = re.sub(r"[\d_.]+", " ", email.split("@")[0]).strip()
    return local.title() if local else email.split("@")[0]


def serialize_struct_fields(doc):
    parts = []
    for k in STRUCT_KEYS:
        v = doc.get(k)
        if v in (None, "", [], {}):
            continue
        parts.append(f"{k}: {v}")
    return "\n".join(parts)


def build_notes_source(doc):
    notes = (doc.get("nutritionist_notes") or "").strip()
    struct = serialize_struct_fields(doc)
    blob = notes
    if struct:
        blob = (blob + "\n\nStructured checkup fields:\n" + struct).strip()
    return blob or "(no notes recorded)"


def load_consultations(zip_path):
    z = zipfile.ZipFile(zip_path)
    dirs = {}
    for n in z.namelist():
        m = re.match(r"consultations/([^/]+)/(.+)", n)
        if not m:
            continue
        d, f = m.groups()
        info = dirs.setdefault(d, {"audio": None, "images": [], "doc": {}})
        if f.endswith((".mp3", ".m4a", ".wav")):
            info["audio"] = f
        elif f.startswith("interactionImages"):
            info["images"].append(f)
        elif f == "document.json":
            info["doc"] = json.loads(z.read(n))
    return z, dirs


def transcribe_one(doc_id, z, dirpath, audio_name, cache):
    if cache.get(doc_id):
        return cache[doc_id]
    from sarvamai import SarvamAI
    import glob
    client = SarvamAI(api_subscription_key=pipeline.CONFIG["SARVAM_API_KEY"])
    ext = Path(audio_name).suffix
    with tempfile.TemporaryDirectory() as tmp:
        path = os.path.join(tmp, f"{doc_id}{ext}")
        with open(path, "wb") as f:
            f.write(z.read(f"{dirpath}/{audio_name}"))
        job = client.speech_to_text_job.create_job(
            model=pipeline.CONFIG["SARVAM_STT_MODEL"],
            mode="transcribe",
            language_code=pipeline.CONFIG["SARVAM_LANGUAGE"],
            with_diarization=True,
            num_speakers=2,
        )
        job.upload_files(file_paths=[path])
        job.start()
        job.wait_until_complete()
        outdir = tempfile.mkdtemp(prefix="sarvam_out_")
        job.download_outputs(output_dir=outdir)
        js = glob.glob(os.path.join(outdir, "*.json"))
        text = pipeline._read_transcript(js[0]) if js else ""
    if text:
        cache[doc_id] = text
        _save_json_cache(TRANSCRIPT_CACHE_PATH, cache)
    return text


def extract_fields(source_text, consultation_type, is_call):
    kind = ("a transcribed nutritionist phone call" if is_call else
            "a nutritionist's written check-in notes (no call took place)")
    system = ("You are a QA analyst for The Kenko Life, a nutrition/health "
              "company. Read " + kind + " and pull out structured notes. "
              "Return STRICT JSON only.")
    user = f"""This is a "{consultation_type}" consultation, captured as {kind}.

Return JSON with EXACTLY these keys. Use "" for anything not discussed or
not applicable — never write "N/A".
- "weight_logged": "Yes" or "No" — was the client's current weight
  discussed or recorded in this interaction.
- "weight_details": actual weight figures / targets mentioned, else "".
- "measurements_logged": body measurements mentioned (height, waist, etc), else "".
- "health_goals_history": goals, medical history, medications/supplements mentioned.
- "current_diet_recall": what the client reported eating day-to-day.
- "kenko_meal_feedback": feedback specifically about Kenko's meals, delivery,
  taste, portions, packaging, or freshness.
- "progress_notes": progress since a prior check-in — weight change, adherence, wins.
- "digestive_health": digestion-related details (acidity, bloating, bowel habits).
- "energy_activity": exercise, sleep, energy levels, cravings mentioned.
- "action_points": semicolon-separated list of next steps / follow-ups agreed, else "".
- "issues": semicolon-separated list of problems or complaints raised, else "".
- "plan_details": the diet/meal plan being set or adjusted for the client.
- "next_checkin": when the next follow-up is scheduled, else "".
- "sentiment": one of "Positive", "Neutral", "Negative" — the client's overall sentiment.
- "summary": 2-4 sentence summary of the interaction.

Source:
\"\"\"{source_text[:12000]}\"\"\"

Return ONLY the JSON object."""
    r = pipeline._post_sarvam_chat({
        "model": pipeline.CONFIG["SARVAM_CHAT_MODEL"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 6000,
    }, timeout=120)
    msg = r.json()["choices"][0].get("message", {})
    content = msg.get("content") or msg.get("reasoning_content") or ""
    default = {k: "" for k in EXTRACT_KEYS}
    default["sentiment"] = "Neutral"
    return pipeline._parse_json(content, default=default)


def build_row(doc_id, info, tcache):
    doc = info["doc"]
    sub_type = doc.get("sub_type")
    is_call = sub_type == "Call" and bool(info["audio"])

    if is_call:
        try:
            text = transcribe_one(doc_id, info["_z"], f"consultations/{doc_id}",
                                   info["audio"], tcache)
        except Exception as e:
            print(f"  [{doc_id}] transcription error: {e}")
            text = ""
        if text:
            transcript_col = text
            source_text = text
        else:
            transcript_col = "[Transcription failed or empty]"
            source_text = build_notes_source(doc)
            is_call = False  # nothing to extract from audio; fall back to notes
    elif sub_type == "Call":  # marked as a call but no recording was uploaded
        notes = (doc.get("nutritionist_notes") or "").strip()
        transcript_col = "[Call — no recording uploaded]" + (f"\n\n{notes}" if notes else "")
        source_text = build_notes_source(doc)
    else:  # Text sub_type: no call ever happened
        notes = (doc.get("nutritionist_notes") or "").strip()
        marker = "[Text consultation — no call recording; based on nutritionist's written notes]"
        transcript_col = marker + (f"\n\n{notes}" if notes else "")
        source_text = build_notes_source(doc)

    try:
        extracted = extract_fields(source_text, doc.get("consultation_type", ""), is_call)
    except Exception as e:
        print(f"  [{doc_id}] extraction error: {e}")
        extracted = {k: "" for k in EXTRACT_KEYS}
        extracted["sentiment"] = "Neutral"

    filename = info["audio"] or (info["images"][0] if info["images"] else f"{doc_id}_note")
    client_name = (client_name_from_filename(info["audio"]) if info["audio"]
                   else client_name_from_email(doc.get("email", "")))

    row = {
        "document_id": doc_id,
        "filename": filename,
        "client_name": client_name,
        "call_date": fmt_call_date(doc.get("checkup_ts") or doc.get("createdTs")),
        "email": doc.get("email", ""),
        "consultation_type": doc.get("consultation_type", ""),
        "transcript": transcript_col,
    }
    for k in EXTRACT_KEYS:
        row[k] = extracted.get(k, "")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--out", default=str(HERE / "consultation_analysis.csv"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None, help="comma-separated doc_ids to process")
    ap.add_argument("--overwrite", action="store_true",
                     help="reprocess doc_ids even if already in the extract cache")
    args = ap.parse_args()

    z, dirs = load_consultations(args.zip_path)
    for info in dirs.values():
        info["_z"] = z

    doc_ids = sorted(dirs.keys())
    if args.only:
        wanted = set(args.only.split(","))
        doc_ids = [d for d in doc_ids if d in wanted]
    if args.limit:
        doc_ids = doc_ids[:args.limit]

    tcache = _load_json_cache(TRANSCRIPT_CACHE_PATH)
    ecache = _load_json_cache(EXTRACT_CACHE_PATH)

    total = len(doc_ids)
    for i, doc_id in enumerate(doc_ids, 1):
        if not args.overwrite and doc_id in ecache:
            print(f"[{i}/{total}] {doc_id} (cached)")
            continue
        info = dirs[doc_id]
        kind = "call" if (info["doc"].get("sub_type") == "Call" and info["audio"]) else "text"
        print(f"[{i}/{total}] {doc_id} ({kind}) processing...")
        row = build_row(doc_id, info, tcache)
        ecache[doc_id] = row
        _save_json_cache(EXTRACT_CACHE_PATH, ecache)

    out_doc_ids = sorted(dirs.keys()) if not args.only else doc_ids
    if args.limit and not args.only:
        out_doc_ids = out_doc_ids[:args.limit]

    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=FIELDNAMES)
        w.writeheader()
        for doc_id in out_doc_ids:
            if doc_id in ecache:
                row = ecache[doc_id]
                row["document_id"] = doc_id
                w.writerow(row)

    print(f"\nWrote {len(out_doc_ids)} rows to {args.out}")


if __name__ == "__main__":
    main()
