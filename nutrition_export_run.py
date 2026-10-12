#!/usr/bin/env python3
"""
Nutrition files export -> consultation_analysis CSV
=====================================================
Variant of consultation_export.py for a different zip shape: a flat
Firebase Storage pull (<doc_id>/recording.{wav,mp3,m4a} or
<doc_id>/image_N.jpg) described by a root-level manifest.csv
(document_id, email, kind, slot, original_filename, original_url,
exported_as) instead of per-folder document.json metadata.

There's no nutritionist_notes/consultation_type/checkup_ts here, so:
- client_name and call_date are parsed from original_filename
  (e.g. "Saiteja_24092026_193320.wav" -> name + DDMMYYYY_HHMMSS).
- consultation_type is classified by the extraction LLM call itself
  (onboarding / consultation / escalation, per CLAUDE.md's nutrition
  taxonomy) since no metadata field provides it.

Only "recording" kind manifest rows are processed (image-only rows are
standalone meal-photo check-ins with no text/audio to analyze).

Run:
    python3 nutrition_export_run.py <zip_path> [--out FILE] [--limit N]
                                     [--only doc_id1,doc_id2,...] [--overwrite]
"""

import re
import csv
import zipfile
import argparse
import datetime as dt
from pathlib import Path

import sys
sys.path.insert(0, str(Path(__file__).parent))
import pipeline
import consultation_export as ce

HERE = Path(__file__).parent
EXTRACT_CACHE_PATH = HERE / "nutrition_export_extract_cache.json"

DATE_RE = re.compile(r"_(\d{2})(\d{2})(\d{4})_(\d{2})(\d{2})(\d{2})")


def parse_date_from_name(name):
    m = DATE_RE.search(name or "")
    if not m:
        return ""
    day, month, year, hh, mm, ss = (int(x) for x in m.groups())
    try:
        d = dt.datetime(year, month, day, hh, mm, ss)
    except ValueError:
        return ""
    return d.strftime("%Y-%m-%d %H:%M")


def load_manifest_recordings(zip_path):
    z = zipfile.ZipFile(zip_path)
    with z.open("manifest.csv") as f:
        reader = csv.DictReader(line.decode("utf-8") for line in f)
        rows = list(reader)
    recs = {}
    for row in rows:
        if row.get("kind") != "recording":
            continue
        recs[row["document_id"]] = {
            "email": row.get("email", ""),
            "original_filename": row.get("original_filename", ""),
            "exported_as": row.get("exported_as", ""),
        }
    return z, recs


def extract_fields_with_type(source_text, is_call):
    kind = ("a transcribed nutritionist phone call" if is_call else
            "a nutritionist interaction with no usable transcript")
    system = ("You are a QA analyst for The Kenko Life, a nutrition/health "
              "company. Read " + kind + " and pull out structured notes. "
              "Return STRICT JSON only.")
    user = f"""This is {kind}, with no prior metadata about its type.

Return JSON with EXACTLY these keys. Use "" for anything not discussed or
not applicable — never write "N/A".
- "consultation_type": one of "onboarding", "consultation", or "escalation".
  onboarding = a new client's first plan-setup call (covers plan start date,
  food storage/handling instructions). consultation = a routine check-in
  referencing the client's app tabs (Meals/Progress/Feedback/Coach Notes).
  escalation = driven by a complaint/problem needing resolution. Best guess
  from the content; if truly unclear default to "consultation".
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
        # sarvam-105b is a reasoning model — its chain-of-thought eats into the
        # completion budget before it reaches the final JSON (see pipeline.analyze()).
        # 6000 wasn't enough margin once consultation_type reasoning was added.
        "max_tokens": 8000,
    }, timeout=120)
    msg = r.json()["choices"][0].get("message", {})
    content = msg.get("content") or msg.get("reasoning_content") or ""
    default = {k: "" for k in ce.EXTRACT_KEYS}
    default["consultation_type"] = "consultation"
    default["sentiment"] = "Neutral"
    return pipeline._parse_json(content, default=default)


def build_row(doc_id, info, z, tcache):
    audio_name = info["exported_as"]
    try:
        text = ce.transcribe_one(doc_id, z, doc_id, audio_name, tcache)
    except Exception as e:
        print(f"  [{doc_id}] transcription error: {e}")
        text = ""

    is_call = bool(text)
    transcript_col = text if text else "[Transcription failed or empty]"
    source_text = text if text else "(no transcript available)"

    try:
        extracted = extract_fields_with_type(source_text, is_call)
    except Exception as e:
        print(f"  [{doc_id}] extraction error: {e}")
        extracted = {k: "" for k in ce.EXTRACT_KEYS}
        extracted["consultation_type"] = "consultation"
        extracted["sentiment"] = "Neutral"

    filename = info["original_filename"] or audio_name
    client_name = (ce.client_name_from_filename(filename) if filename
                   else ce.client_name_from_email(info["email"]))

    row = {
        "document_id": doc_id,
        "filename": filename,
        "client_name": client_name,
        "call_date": parse_date_from_name(filename),
        "email": info["email"],
        "consultation_type": extracted.get("consultation_type", "consultation"),
        "transcript": transcript_col,
    }
    for k in ce.EXTRACT_KEYS:
        row[k] = extracted.get(k, "")
    return row


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("zip_path")
    ap.add_argument("--out", default=str(HERE / "nutrition_export_analysis.csv"))
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--only", default=None, help="comma-separated doc_ids to process")
    ap.add_argument("--overwrite", action="store_true")
    args = ap.parse_args()

    z, recs = load_manifest_recordings(args.zip_path)

    doc_ids = sorted(recs.keys())
    if args.only:
        wanted = set(args.only.split(","))
        doc_ids = [d for d in doc_ids if d in wanted]
    if args.limit:
        doc_ids = doc_ids[:args.limit]

    tcache = ce._load_json_cache(ce.TRANSCRIPT_CACHE_PATH)
    ecache = ce._load_json_cache(EXTRACT_CACHE_PATH)

    total = len(doc_ids)
    for i, doc_id in enumerate(doc_ids, 1):
        if not args.overwrite and doc_id in ecache:
            print(f"[{i}/{total}] {doc_id} (cached)")
            continue
        print(f"[{i}/{total}] {doc_id} processing...")
        row = build_row(doc_id, recs[doc_id], z, tcache)
        ecache[doc_id] = row
        ce._save_json_cache(EXTRACT_CACHE_PATH, ecache)

    # Write every doc_id ever cached (not just this zip's recs, and not just
    # --only/--limit's scope) so running against a second/later export zip
    # accumulates into one CSV instead of dropping earlier runs' rows.
    out_doc_ids = sorted(ecache.keys())
    with open(args.out, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ce.FIELDNAMES)
        w.writeheader()
        for doc_id in out_doc_ids:
            if doc_id in ecache:
                row = dict(ecache[doc_id])
                row["document_id"] = doc_id
                w.writerow(row)

    print(f"\nWrote {sum(1 for d in out_doc_ids if d in ecache)} rows to {args.out}")


if __name__ == "__main__":
    main()
