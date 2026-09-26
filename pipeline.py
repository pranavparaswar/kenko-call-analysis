#!/usr/bin/env python3
"""
Kenko Call Analysis Pipeline
============================
Weekly pipeline: Callyzer call logs  ->  download recordings  ->  Sarvam
speech-to-text (saaras:v4)  ->  LLM analysis (summary / sentiment / QA /
must-say checklist)  ->  results.json  ->  dashboard.html

Run:  python pipeline.py                 (uses last 7 days by default)
      python pipeline.py --days 7
      python pipeline.py --demo           (no API calls, regenerates sample data)

Requirements:
      pip install requests sarvamai pydub
      # pydub needs ffmpeg installed for chunking long audio (brew install ffmpeg)

Set your keys below in CONFIG (or via environment variables).
"""

import os
import re
import csv
import json
import time
import argparse
import datetime as dt
from pathlib import Path

# ----------------------------------------------------------------------------
# CONFIG  — edit this section
# ----------------------------------------------------------------------------
CONFIG = {
    # --- Callyzer ---
    "CALLYZER_TOKEN": os.environ.get("CALLYZER_TOKEN", "PASTE_CALLYZER_BEARER_TOKEN"),
    "CALLYZER_BASE": "https://api1.callyzer.co/api/v2.2",

    # --- Sarvam ---
    "SARVAM_API_KEY": os.environ.get("SARVAM_API_KEY", "PASTE_SARVAM_API_KEY"),
    "SARVAM_STT_MODEL": "saaras:v3",     # Batch API model (handles up to 2h/file)
    "SARVAM_LANGUAGE": "en-IN",          # calls are mostly English
    "SARVAM_CHAT_MODEL": "sarvam-105b",  # analysis step (sarvam-m / -30b deprecated)
    "DIARIZATION": True,                 # separate agent vs customer
    "NUM_SPEAKERS": 2,
    "BATCH_SIZE": 20,                    # max files per Sarvam batch job
    "ROLE_LLM_FALLBACK": False,          # guess role from transcript when a rep
                                         # isn't in employees.csv. Keep False so
                                         # roles come only from your tags.

    # --- Scope ---
    "DEFAULT_DAYS": 7,
    "CALL_TYPES": ["Outgoing", "Incoming"],
    "MIN_DURATION_SEC": 30,              # calls >= this get the full QA checklist;
                                         # shorter calls with a recording get a
                                         # lightweight drop-reason classification
                                         # instead; 0-duration/no-recording calls
                                         # are logged as "missed" with no analysis.
                                         # Nothing is dropped from fetch/results.
    "EMP_NUMBERS": [],                   # [] = all reps; else ["91-98xxxxxxx", ...]

    # --- Output ---
    "OUT_DIR": str(Path(__file__).parent),
    "AUDIO_DIR": str(Path(__file__).parent / "audio"),
    "ROLES_CSV": str(Path(__file__).parent / "employees.csv"),  # role tagging lives here
}

# ----------------------------------------------------------------------------
# ROLE ROUTING  — Callyzer has both SALES and NUTRITIONIST calls, each with its
# own must-cover checklist. Role is decided per call in this order:
#   1) employees.csv  (run `python pipeline.py --employees`, then fill the
#      'role' column with sales/nutrition)  <-- EASIEST, no code editing
#   2) REP_ROLES map below (emp number OR exact name -> "sales"/"nutrition")
#   3) Callyzer emp_tags containing "sales" / "nutrition"
#   4) LLM classification from the transcript (fallback)
# ----------------------------------------------------------------------------
REP_ROLES = {
    # Optional — you can leave this empty and just use employees.csv instead.
    # "91-9812345678": "sales",
    # "Priya Nair": "nutrition",
}

# ----------------------------------------------------------------------------
# CALL-TYPE TAXONOMY  — role -> call type -> {desc, checkpoints}.
# The AI first classifies WHICH type of call it is (from `desc`), then scores
# only THAT type's checkpoints. Fully editable: add/rename types, edit
# checkpoints, add roles. Checkpoints below are PLACEHOLDERS — refine later.
#   checkpoint: {id, label (shown in UI), criteria (what the model looks for)}
# ----------------------------------------------------------------------------
CALL_TYPES = {
    "nutrition": {
        # Checkpoints and weights derived from Kenko's nutritionist app tabs
        # (Meals / Progress / Feedback / Coach Notes fields). weight is 1-3:
        #   3 = core clinical value of the call, 2 = important, 1 = call hygiene.
        # qa_score = weighted % of checkpoints covered (see weighted_score()).
        "onboarding": {
            "desc": "First interaction with a brand-new client — kickoff / welcome.",
            "checkpoints": [
                {"id": "greet",        "label": "Greet + identify as Kenko", "weight": 1,
                 "criteria": "Greets the client by name and identifies as The Kenko Life."},
                {"id": "goals",        "label": "Understand goals & history", "weight": 3,
                 "criteria": "Asks about the client's health goals, medical history, and lifestyle."},
                {"id": "medical_history", "label": "Health & medical history", "weight": 3,
                 "criteria": "Specifically asks about medical history, existing conditions, "
                             "allergies, or medications — not just general goals."},
                {"id": "baseline",     "label": "Record baseline weight/measures", "weight": 3,
                 "criteria": "Notes current weight and/or body measurements as a starting baseline."},
                {"id": "body_composition", "label": "Body composition (BCA)", "weight": 2,
                 "criteria": "Discusses body composition analysis (BCA) metrics — e.g. fat %, "
                             "muscle mass — if available on record."},
                {"id": "dietary_recall", "label": "Dietary recall & meal plan discussion", "weight": 3,
                 "criteria": "Asks what the client currently/typically eats (dietary recall) and "
                             "discusses the subscription meal plan specifics."},
                {"id": "activity",     "label": "Training & activity level", "weight": 2,
                 "criteria": "Asks about the client's workout / physical activity level."},
                {"id": "digestive_health", "label": "Digestive health", "weight": 2,
                 "criteria": "Asks about gut/digestive health."},
                {"id": "lifestyle",    "label": "Recovery & lifestyle", "weight": 1,
                 "criteria": "Asks about sleep, stress, or other recovery/lifestyle factors."},
                {"id": "explain_prog", "label": "Explain the program", "weight": 2,
                 "criteria": "Explains how the Kenko program / meal plan works and what to expect."},
                {"id": "first_plan",   "label": "Set initial plan & portions", "weight": 3,
                 "criteria": "Gives the initial diet plan, including meals and portion sizes."},
                {"id": "start_date",   "label": "Confirm plan start date", "weight": 3,
                 "criteria": "States a clear start date for the plan / when deliveries begin."},
                {"id": "food_handling","label": "Explain food storage & handling", "weight": 3,
                 "criteria": "Tells the client how to store and reheat the food — e.g. keep "
                             "refrigerated, microwave before eating, and how long it keeps."},
                {"id": "set_followup", "label": "Set first follow-up", "weight": 2,
                 "criteria": "Sets a clear date for the first follow-up / check-in."},
            ],
        },
        "consultation": {
            "desc": "A routine follow-up / ongoing check-in with an existing client.",
            # Redesigned 2026-09-06: four checkpoints — meal feedback, metrics
            # progress, future plan/guidance, and a conditional plan
            # modification. Order of coverage is explicitly NOT scored — only
            # whether each topic was genuinely covered somewhere on the call.
            "checkpoints": [
                {"id": "meal_feedback", "label": "Meal feedback", "weight": 3,
                 "criteria": "Asks for the client's feedback on the meals — taste, "
                             "variety, portion size, satisfaction."},
                {"id": "metrics_progress", "label": "Progress on metrics", "weight": 3,
                 "criteria": "Discusses progress on tracked metrics — weight, measurements, "
                             "body composition, or other numbers from prior check-ins."},
                {"id": "future_plan", "label": "Future plan & guidance", "weight": 3,
                 "criteria": "Discusses the plan going forward and gives the client clear "
                             "guidance/next steps."},
                {"id": "plan_modification", "label": "Diet plan modification (if needed)", "weight": 1,
                 "criteria": "If the client's feedback or metrics progress warrants a change, "
                             "the rep proposes or makes a diet plan modification. Not "
                             "applicable if nothing on the call warranted a change."},
            ],
        },
        "escalation": {
            "desc": "Client called with a problem, complaint, or issue to resolve.",
            "checkpoints": [
                {"id": "ack",       "label": "Acknowledge the issue", "weight": 2,
                 "criteria": "Greets and acknowledges the client's issue/concern."},
                {"id": "understand","label": "Understand the problem fully", "weight": 3,
                 "criteria": "Asks questions to fully understand the problem."},
                {"id": "empathy",   "label": "Empathize", "weight": 1,
                 "criteria": "Responds with empathy / reassurance."},
                {"id": "resolve",   "label": "Provide resolution / next step", "weight": 3,
                 "criteria": "Gives a concrete resolution or a clear next step to fix it."},
                {"id": "followup",  "label": "Set follow-up on the issue", "weight": 2,
                 "criteria": "Commits to a specific follow-up on the issue."},
                {"id": "close",     "label": "Close politely", "weight": 1,
                 "criteria": "Closes the call politely."},
            ],
        },
    },
    "sales": {
        "enquiry": {
            # Checkpoints and weights below (1-3, same scale as nutrition) cover the
            # full sales pitch — split into granular, independently-scored items
            # instead of one bundled "explain the plan" checkpoint.
            "desc": "First contact with a prospect — discovery / new enquiry.",
            "checkpoints": [
                {"id": "intro",        "label": "Intro w/ name + Kenko", "weight": 1,
                 "criteria": "Introduces themselves by name and mentions The Kenko Life."},
                {"id": "health_goal",  "label": "Ask health goals & conditions", "weight": 3,
                 "criteria": "Asks about the customer's health/nutrition goals and any "
                             "relevant health conditions."},
                {"id": "delivery_location", "label": "Ask delivery location", "weight": 2,
                 "criteria": "Asks for and notes the customer's delivery location/address; "
                             "mentions support for multiple delivery addresses if relevant."},
                {"id": "macros_calories", "label": "Mention macro & calorie counting", "weight": 2,
                 "criteria": "Mentions that meals are macro- and calorie-counted."},
                {"id": "nutrition_support", "label": "Nutrition support throughout journey", "weight": 2,
                 "criteria": "Talks about ongoing nutrition/coach support being available "
                             "throughout the customer's journey, not just at signup."},
                {"id": "cuisine_variety", "label": "Varied cuisines / menu rotation", "weight": 2,
                 "criteria": "Talks about the variety of meals and cuisines on offer, and "
                             "mentions the menu doesn't repeat for 4 weeks if relevant."},
                {"id": "delivery_schedule", "label": "Delivery schedule", "weight": 2,
                 "criteria": "Explains that delivery happens 6 days a week, within a "
                             "delivery window."},
                {"id": "meal_wallet_flexibility", "label": "Meal wallet / pause & cancel flexibility", "weight": 3,
                 "criteria": "Explains that 1 month = 26 meals which can be used flexibly "
                             "over any period, with the ability to pause or cancel deliveries."},
                {"id": "food_temp_handling", "label": "Food temperature & handling", "weight": 3,
                 "criteria": "Explains that food is delivered cold/refrigerated and must be "
                             "refrigerated and reheated before eating."},
                {"id": "veg_nonveg_pref", "label": "Ask veg/non-veg preference", "weight": 1,
                 "criteria": "Asks and notes whether the customer prefers veg or non-veg meals."},
                {"id": "price",        "label": "State price/offer clearly", "weight": 3,
                 "criteria": "States the price and any current offer clearly."},
                {"id": "app_link_payment", "label": "Send app link if customer is ready to buy", "weight": 3,
                 "criteria": "If at any point on the call the customer says they're convinced "
                             "and asks how to proceed, mentions/sends the Kenko app link and "
                             "directs them to download the app to complete payment there "
                             "(payment is via app link, not a payment gateway). Not applicable "
                             "if the customer never indicated readiness to proceed on this "
                             "call, or for trial plans / plans outside the standard "
                             "1/2/3-month options — for those, explains next steps instead."},
                {"id": "next_step",    "label": "Set next step", "weight": 1,
                 "criteria": "Sets a clear next step or follow-up."},
            ],
        },
        "follow_up": {
            "desc": "Following up with a prospect from an earlier conversation.",
            "checkpoints": [
                {"id": "intro",     "label": "Intro / reconnect",
                 "criteria": "Reconnects and identifies self + Kenko."},
                {"id": "recap",     "label": "Recap prior discussion",
                 "criteria": "Recaps the earlier conversation / what was discussed."},
                {"id": "objections","label": "Address objections",
                 "criteria": "Addresses the customer's concerns or objections, or answers "
                             "any pending questions from the customer."},
                {"id": "value",     "label": "Restate value / offer",
                 "criteria": "Restates the value and the current offer."},
                {"id": "app_link_payment", "label": "Send app link if customer is ready to buy",
                 "criteria": "If at any point on the call the customer says they're convinced "
                             "and asks how to proceed, mentions/sends the Kenko app link and "
                             "directs them to download the app to complete payment there "
                             "(payment is via app link, not a payment gateway). Not applicable "
                             "if the customer never indicated readiness to proceed on this "
                             "call, or for trial plans / plans outside the standard "
                             "1/2/3-month options — for those, explains next steps instead."},
                {"id": "ask",       "label": "Ask for decision / next step",
                 "criteria": "Asks for a decision or sets a concrete next step."},
            ],
        },
    },
}

ANALYZED_ROLES = set(CALL_TYPES.keys())   # roles that get analyzed (sales, nutrition)

# ----------------------------------------------------------------------------
# Progress helpers (stdlib only)
# ----------------------------------------------------------------------------
def _bar(n, total, prefix="", width=28):
    total = max(total, 1)
    filled = int(width * n / total)
    bar = "#" * filled + "-" * (width - filled)
    pct = int(100 * n / total)
    end = "\n" if n >= total else ""
    print(f"\r  {prefix}[{bar}] {n}/{total} ({pct}%)", end=end, flush=True)


def _attr(obj, key):
    if obj is None:
        return None
    if isinstance(obj, dict):
        return obj.get(key)
    return getattr(obj, key, None)


def _wait_with_progress(job, total_files):
    """Poll the batch job and show a real file-completion % + elapsed time.
    Falls back to a spinner if the SDK doesn't expose a status method."""
    # find a callable that returns job status
    status_fn = None
    for name in ("get_status", "status", "get_job_status"):
        f = getattr(job, name, None)
        if callable(f):
            status_fn = f
            break
    if not status_fn:
        return _run_with_spinner("transcribing (Sarvam batch job running)...",
                                 job.wait_until_complete)

    start = time.time()
    done_states = ("completed", "failed", "success", "succeeded")
    while True:
        try:
            st = status_fn()
        except Exception:
            return _run_with_spinner("transcribing (finishing)...",
                                     job.wait_until_complete)
        state = str(_attr(st, "job_state") or "").lower()
        done = (_attr(st, "successful_files_count") or 0) + \
               (_attr(st, "failed_files_count") or 0)
        el = int(time.time() - start)
        w = 28
        fill = int(w * done / max(total_files, 1))
        pct = int(100 * done / max(total_files, 1))
        print(f"\r  transcribing [{'#'*fill}{'-'*(w-fill)}] "
              f"{done}/{total_files} files ({pct}%) · {el}s",
              end="", flush=True)
        if state in done_states or el > 2400:   # 40-min safety cap
            break
        time.sleep(3)
    print()
    try:
        job.wait_until_complete()   # ensure outputs are fully ready
    except Exception:
        pass


def _run_with_spinner(label, fn):
    """Run blocking fn() in a thread while animating a spinner + elapsed time."""
    import threading, itertools
    box = {}

    def target():
        try:
            box["val"] = fn()
        except Exception as e:  # noqa
            box["err"] = e

    t = threading.Thread(target=target)
    t.start()
    frames = itertools.cycle("|/-\\")
    start = time.time()
    while t.is_alive():
        el = int(time.time() - start)
        print(f"\r  {label} {next(frames)} {el}s elapsed", end="", flush=True)
        time.sleep(0.5)
    t.join()
    print("\r" + " " * 60 + "\r", end="", flush=True)
    if "err" in box:
        raise box["err"]
    return box.get("val")


# ----------------------------------------------------------------------------
# Callyzer
# ----------------------------------------------------------------------------
def _post_sarvam_chat(payload, timeout=120, max_retries=4):
    """POST to Sarvam's chat completions endpoint with retry/backoff on
    transient network failures (DNS blips, dropped connections, timeouts) as
    well as 429/5xx — a single flaky connection shouldn't sink an otherwise-
    good analysis call. Raises on a real 4xx (bad request/auth) immediately."""
    import requests
    delay = 5
    last_err = None
    for attempt in range(max_retries):
        try:
            r = requests.post(
                "https://api.sarvam.ai/v1/chat/completions",
                headers={"Authorization": f"Bearer {CONFIG['SARVAM_API_KEY']}",
                         "Content-Type": "application/json"},
                json=payload,
                timeout=timeout,
            )
        except (requests.exceptions.ConnectionError, requests.exceptions.Timeout) as e:
            last_err = e
            print(f"\n   network error; retrying in {delay}s "
                  f"(attempt {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if r.status_code == 429 or r.status_code >= 500:
            print(f"\n   {r.status_code} from Sarvam; retrying in {delay}s "
                  f"(attempt {attempt + 1}/{max_retries})...")
            time.sleep(delay)
            delay = min(delay * 2, 30)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"{r.status_code}: {r.text[:300]}")
        return r
    raise RuntimeError(f"Sarvam chat completions failed after {max_retries} attempts: {last_err}")


def _post(url, headers, body, timeout=60, max_retries=5):
    """POST with automatic backoff on 429 (rate limit) / 5xx."""
    import requests
    delay = 5
    for attempt in range(max_retries):
        r = requests.post(url, headers=headers, json=body, timeout=timeout)
        if r.status_code == 429 or r.status_code >= 500:
            ra = r.headers.get("Retry-After", "")
            try:
                wait = int(float(ra))          # header is plain seconds
            except (ValueError, TypeError):
                wait = delay                    # header missing or a timestamp
            wait = wait or delay
            print(f"   rate-limited ({r.status_code}); waiting {wait}s "
                  f"(attempt {attempt + 1}/{max_retries})...")
            time.sleep(wait)
            delay = min(delay * 2, 60)
            continue
        if r.status_code >= 400:
            raise RuntimeError(f"Callyzer {r.status_code} at {url}: {r.text[:400]}")
        return r
    raise RuntimeError(f"Callyzer {r.status_code} at {url}: {r.text[:400]}")


def fetch_call_logs(days):
    now = int(time.time())
    call_from = now - days * 86400
    headers = {
        "Authorization": f"Bearer {CONFIG['CALLYZER_TOKEN']}",
        "Content-Type": "application/json",
    }
    logs, page = [], 1
    while True:
        body = {
            "call_from": call_from,
            "call_to": now,
            "call_types": CONFIG["CALL_TYPES"],
            # no duration_grt_than — every call is fetched, including 0-duration
            # no-answers, so nothing is dropped before it's even seen.
            "call_method": "PhoneCall",
            "call_mode": "Voice",
            "page_no": page,
            "page_size": 100,
        }
        if CONFIG["EMP_NUMBERS"]:
            body["emp_numbers"] = CONFIG["EMP_NUMBERS"]
        r = _post(f"{CONFIG['CALLYZER_BASE']}/call-log/history", headers, body)
        data = r.json()
        batch = data.get("result", data.get("data", []))
        if not batch:
            break
        logs.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return logs


def download_recording(url, dest):
    import requests
    if not url:
        return None
    Path(CONFIG["AUDIO_DIR"]).mkdir(parents=True, exist_ok=True)
    # Recording URLs may or may not require the same bearer token; try both.
    for hdrs in ({"Authorization": f"Bearer {CONFIG['CALLYZER_TOKEN']}"}, {}):
        try:
            r = requests.get(url, headers=hdrs, timeout=120)
            if r.status_code == 200 and r.content:
                Path(dest).write_bytes(r.content)
                return dest
        except Exception:
            continue
    return None


def _norm_num(s):
    """Last 10 digits of a number, for tolerant matching across formats."""
    return re.sub(r"\D", "", str(s or ""))[-10:]


def fetch_employees(days=90):
    """List all employees seen in the last `days`, with their Callyzer tags."""
    now = int(time.time())
    headers = {"Authorization": f"Bearer {CONFIG['CALLYZER_TOKEN']}",
               "Content-Type": "application/json"}
    emps, page = {}, 1
    while True:
        body = {"call_from": now - days * 86400, "call_to": now,
                "call_method": "PhoneCall", "call_mode": "Voice",
                "page_no": page, "page_size": 100}
        r = _post(f"{CONFIG['CALLYZER_BASE']}/call-log/employee-summary", headers, body)
        res = r.json().get("result", [])
        # `result` is a list of employee objects
        batch = res if isinstance(res, list) else res.get("employees", [])
        if not batch:
            break
        for e in batch:
            cc = e.get("emp_country_code") or ""
            local = e.get("emp_number") or ""
            full = f"{cc}-{local}" if cc else local
            emps[_norm_num(local)] = {
                "emp_name": e.get("emp_name", ""),
                "emp_number": full,
                "emp_code": e.get("emp_code") or "",
                "callyzer_tags": ", ".join(e.get("emp_tags", []) or []),
            }
        if len(batch) < 100:
            break
        page += 1
    return list(emps.values())


def export_employees(days=90):
    """Write employees.csv with a blank `role` column for you to fill in."""
    if CONFIG["CALLYZER_TOKEN"].startswith("PASTE"):
        print("CALLYZER_TOKEN is not set. Run:  export CALLYZER_TOKEN=\"...\"")
        return
    print(f"Fetching employees active in the last {days} days...")
    emps = fetch_employees(days)
    path = Path(CONFIG["ROLES_CSV"])
    existing = load_csv_roles()          # keep any roles already tagged
    with open(path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["emp_name", "emp_number", "emp_code", "callyzer_tags", "role"])
        for e in sorted(emps, key=lambda x: x["emp_name"].lower()):
            role = existing.get(e["emp_number"]) or existing.get(e["emp_name"]) or ""
            w.writerow([e["emp_name"], e["emp_number"], e["emp_code"],
                        e["callyzer_tags"], role])
    print(f"  Wrote {len(emps)} employees -> {path.name}")
    print("  Open it, put 'sales' or 'nutrition' in the 'role' column, and save.")


def _normalize_role(s):
    """Map free-text role labels to canonical roles.
    'Nutritionist'/'Diet Coach' -> 'nutrition', 'Sales' -> 'sales',
    anything else (e.g. 'Ops', 'Admin') kept lowercased -> not analyzed."""
    s = (s or "").strip().lower()
    if not s:
        return ""
    if "nutri" in s or "diet" in s or "coach" in s:
        return "nutrition"
    if "sale" in s:
        return "sales"
    return s  # e.g. 'ops' — has no checklist, so those calls are skipped


def load_csv_roles():
    """Read employees.csv -> {emp_number: role, emp_name: role} (only filled rows).
    Robust to Excel quirks: BOM (utf-8-sig), and header names with different
    case / surrounding spaces."""
    path = Path(CONFIG["ROLES_CSV"])
    roles = {}
    if not path.exists():
        return roles
    with open(path, newline="", encoding="utf-8-sig") as f:
        text = f.read()
    lines = text.splitlines()
    if not lines:
        return roles
    # auto-detect delimiter (Excel may save ; or tab depending on locale)
    delim = max([",", ";", "\t", "|"], key=lambda d: lines[0].count(d))
    rows = list(csv.reader(lines, delimiter=delim))
    if not rows:
        return roles
    header = [(h or "").strip().lower() for h in rows[0]]

    def idx(colname):
        return header.index(colname) if colname in header else -1

    i_num, i_name, i_role = idx("emp_number"), idx("emp_name"), idx("role")
    for r in rows[1:]:
        if not r:
            continue
        role = _normalize_role(r[i_role] if 0 <= i_role < len(r) else "")
        if not role:
            continue
        num = (r[i_num].strip() if 0 <= i_num < len(r) else "")
        nm = (r[i_name].strip() if 0 <= i_name < len(r) else "")
        if num:
            roles[num] = role
            roles["#" + _norm_num(num)] = role     # tolerant number (last 10 digits)
        if nm:
            roles[nm] = role
            roles["@" + nm.lower()] = role          # tolerant name (case-insensitive)
    return roles


# ----------------------------------------------------------------------------
# Sarvam — Speech to Text (Batch API, saaras:v3)
# Handles calls up to 2h each, up to BATCH_SIZE files per job, with speaker
# diarization so agent vs customer speech is separated.
# ----------------------------------------------------------------------------
def _cache_path():
    return Path(CONFIG["OUT_DIR"]) / "transcripts_cache.json"


def _load_transcript_cache():
    p = _cache_path()
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:
            return {}
    return {}


def _save_transcript_cache(cache):
    _cache_path().write_text(json.dumps(cache, ensure_ascii=False, indent=2))


def transcribe_batch(items):
    """
    items: list of dicts with keys {"call_id", "audio"} (audio = local file path).
    Returns: {call_id: transcript_text}. Transcript is diarized and formatted as
             "Speaker 0: ...\nSpeaker 1: ..." when diarization is on.
    Transcripts are cached to transcripts_cache.json so re-runs (e.g. after an
    analysis error) never re-charge you for transcription.
    """
    from sarvamai import SarvamAI
    import tempfile, glob
    cache = _load_transcript_cache()
    out = {}
    todo = []
    for it in items:
        if it["call_id"] in cache and cache[it["call_id"]]:
            out[it["call_id"]] = cache[it["call_id"]]
        else:
            todo.append(it)
    if out:
        print(f"  {len(out)} transcript(s) reused from cache.")
    if not todo:
        return out

    client = SarvamAI(api_subscription_key=CONFIG["SARVAM_API_KEY"])
    total = len(todo)
    print(f"  Transcribing {total} recordings (one job per call for exact "
          f"call-to-transcript matching)...")
    for n, it in enumerate(todo, 1):
        cid, path = it["call_id"], it["audio"]
        try:
            job = client.speech_to_text_job.create_job(
                model=CONFIG["SARVAM_STT_MODEL"],
                mode="transcribe",
                language_code=CONFIG["SARVAM_LANGUAGE"],
                with_diarization=CONFIG["DIARIZATION"],
                num_speakers=CONFIG["NUM_SPEAKERS"],
            )
            job.upload_files(file_paths=[path])      # exactly ONE file
            job.start()
            job.wait_until_complete()

            outdir = tempfile.mkdtemp(prefix="sarvam_out_")
            job.download_outputs(output_dir=outdir)
            # one input => exactly one output JSON => zero ambiguity
            js = glob.glob(os.path.join(outdir, "*.json"))
            text = _read_transcript(js[0]) if js else ""
            out[cid] = text
            if text:
                cache[cid] = text
                _save_transcript_cache(cache)        # persist after each call
        except Exception as e:
            print(f"\n   transcribe error (call {cid}): {e}")
            out.setdefault(cid, "")
        _bar(n, total, prefix="transcribing ")
    return out


def _output_cid_map(job):
    """Return {output_file_name: call_id} by pairing each job input file
    ("{call_id}.mp3") with its output file, read from the job status.
    Returns {} if the SDK doesn't expose the structure (caller falls back)."""
    m = {}
    fn = None
    for n in ("get_status", "status", "get_job_status"):
        f = getattr(job, n, None)
        if callable(f):
            fn = f
            break
    if not fn:
        return m
    try:
        st = fn()
    except Exception:
        return m
    for d in (_attr(st, "job_details") or []):
        ins = _attr(d, "inputs") or []
        outs = _attr(d, "outputs") or []
        if ins and outs:
            in_name = _attr(ins[0], "file_name") or ""
            out_name = _attr(outs[0], "file_name") or ""
            cid = Path(in_name).stem            # "abc123.mp3" -> "abc123"
            if out_name and cid:
                m[out_name] = cid
    return m


def _read_transcript(json_path):
    try:
        data = json.loads(Path(json_path).read_text())
    except Exception:
        return ""
    dia = data.get("diarized_transcript")
    if dia and dia.get("entries"):
        return "\n".join(
            f"Speaker {e.get('speaker_id','?')}: {e.get('transcript','').strip()}"
            for e in dia["entries"] if e.get("transcript")
        )
    return (data.get("transcript") or "").strip()


# ----------------------------------------------------------------------------
# Sarvam — Analysis (chat completions)
# ----------------------------------------------------------------------------
_CSV_ROLES = None

def detect_role(log, transcript=None, allow_llm=True):
    """Return a role string, or None if undetermined and allow_llm is False.
    See ROLE ROUTING notes above for the order of precedence."""
    global _CSV_ROLES
    if _CSV_ROLES is None:
        _CSV_ROLES = load_csv_roles()
    # 1) employees.csv tagging (highest priority)
    num = log.get("emp_number")
    name = (log.get("emp_name") or "").strip()
    keys = [num, ("#" + _norm_num(num)) if num else None, name,
            ("@" + name.lower()) if name else None]
    for key in keys:
        if key and key in _CSV_ROLES:
            return _CSV_ROLES[key]
    # 2) explicit REP_ROLES map in this file
    for key in (log.get("emp_number"), log.get("emp_name")):
        if key and key in REP_ROLES:
            return _normalize_role(REP_ROLES[key])
    # 3) Callyzer employee tags
    tags = " ".join(str(t) for t in (log.get("emp_tags") or [])).lower()
    if "nutrition" in tags or "diet" in tags or "coach" in tags:
        return "nutrition"
    if "sales" in tags:
        return "sales"
    # 4) LLM classification fallback — OFF by default. Only guesses from the
    #    transcript when explicitly enabled (CONFIG['ROLE_LLM_FALLBACK']).
    #    With all reps tagged in employees.csv, this should stay off so the same
    #    person is never labelled differently on different calls.
    if allow_llm and transcript and CONFIG.get("ROLE_LLM_FALLBACK"):
        try:
            return _classify_role(transcript)
        except Exception:
            return None
    return None


def _classify_role(transcript):
    import requests
    r = requests.post(
        "https://api.sarvam.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {CONFIG['SARVAM_API_KEY']}",
                 "Content-Type": "application/json"},
        json={"model": CONFIG["SARVAM_CHAT_MODEL"], "temperature": 0,
              "messages": [{"role": "user", "content":
                "Classify this call as either 'sales' (pitching/selling a plan to a "
                "prospect) or 'nutrition' (a nutritionist reviewing an existing "
                "client's diet/progress). Reply with ONLY one word: sales or "
                "nutrition.\n\n" + transcript[:3000]}]},
        timeout=60)
    ans = r.json()["choices"][0]["message"]["content"].strip().lower()
    return "nutrition" if "nutri" in ans else "sales"


def analyze(transcript, role, name=""):
    import requests
    types = CALL_TYPES.get(role, CALL_TYPES["sales"])
    role_label = "nutritionist" if role == "nutrition" else "sales"

    # describe every call type + its checkpoints so the model classifies THEN scores
    types_txt = ""
    for tkey, t in types.items():
        cps = "\n".join(f'      - "{c["id"]}": {c["criteria"]}' for c in t["checkpoints"])
        types_txt += f'  * "{tkey}": {t["desc"]}\n    checkpoints:\n{cps}\n'
    type_keys = ", ".join(f'"{k}"' for k in types.keys())

    system = (
        f"You are a QA analyst for The Kenko Life, a nutrition/health company. "
        f"Analyze this {role_label} call transcript. Return STRICT JSON only."
    )
    user = f"""This is a {role_label} call. First decide which TYPE of call it is,
then evaluate ONLY that type's checkpoints.

Call types (pick exactly one):
{types_txt}

Before scoring, check whether the customer ever gave the rep a real chance to
run the pitch. Some calls last over {CONFIG['MIN_DURATION_SEC']}s but the
customer shuts things down almost immediately — flatly not interested, or the
rep discovers early on that the delivery location isn't serviceable. In those
cases the rep never had the opportunity to hit most checkpoints, so it isn't
fair to score them against the full checklist.

Return JSON with EXACTLY these keys:
- "call_type": one of {type_keys}.
- "summary": 2-3 sentence summary of the call.
- "sentiment": one of "Positive", "Neutral", "Negative" (customer's overall sentiment).
- "sentiment_reason": one short sentence.
- "early_exit_reason": "" if the rep had a genuine opportunity to work through
  the checklist (even if the customer ultimately declined at the end). Otherwise
  one of {", ".join(f'"{d}"' for d in DROP_REASONS)} — use this ONLY when the
  call ended early through no fault of the rep (e.g. customer refused to engage
  almost immediately, or the location turned out to be undeliverable), before
  most checkpoints could reasonably come up.
- "drop_note": if early_exit_reason is set, one short sentence on what happened;
  otherwise "".
- "qa_score": integer 0-100 rating the rep's overall call quality FOR THAT CALL TYPE.
  Only meaningful if early_exit_reason is "".
- "qa_notes": one or two sentences of coaching feedback. Only meaningful if
  early_exit_reason is "".
- "must_say": an object mapping each checkpoint id of the CHOSEN call_type to
  true/false. Only meaningful if early_exit_reason is "".
- "metric_values": an object mapping each checkpoint id of the CHOSEN call_type
  to the actual value or detail mentioned on the call, where the checkpoint
  involves one — e.g. an actual weight ("78kg"), a measurement, a price quoted,
  a specific date, or a short summary of what the client said (their diet,
  activity level, symptoms, feedback). Use "" for checkpoints that are purely
  behavioral (e.g. a greeting) or where no concrete detail was mentioned. Only
  meaningful if early_exit_reason is "".
- "action_items": array of short strings (follow-ups / next steps).

Transcript:
\"\"\"{transcript[:12000]}\"\"\"

Return ONLY the JSON object."""

    r = _post_sarvam_chat({
        "model": CONFIG["SARVAM_CHAT_MODEL"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        # sarvam-105b is a reasoning model — its chain-of-thought eats into the
        # completion budget before it reaches the final JSON. The API's default
        # cap (2048) is too low for this prompt (long checkpoint list + long
        # transcript): the response hits finish_reason="length" mid-reasoning,
        # content comes back None, and reasoning_content alone (no closing JSON)
        # fails to parse. 8000 leaves headroom (~5300 tokens observed in testing).
        "max_tokens": 8000,
    }, timeout=120)
    msg = r.json()["choices"][0].get("message", {})
    # sarvam-105b can return content=None with the text under reasoning_content
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _parse_json(content, default={
        "call_type": "", "summary": "", "sentiment": "Neutral",
        "sentiment_reason": "", "early_exit_reason": "", "drop_note": "",
        "qa_score": 0, "qa_notes": "", "must_say": {}, "metric_values": {},
        "action_items": [], "_analysis_failed": True})


# Calls under MIN_DURATION_SEC still get a recording sometimes (quick connects,
# fast hangups) — not enough substance for the full checklist, but worth knowing
# WHY the call was short. Feeds rep callback reminders later (see CLAUDE.md).
DROP_REASONS = ["call_later", "not_interested", "wrong_number", "no_answer",
                "busy_hangup", "undeliverable_location", "other"]

def analyze_short_call(transcript, role, name=""):
    import requests
    role_label = "nutritionist" if role == "nutrition" else "sales"
    system = (
        f"You are a QA analyst for The Kenko Life. This is a SHORT phone call "
        f"(under {CONFIG['MIN_DURATION_SEC']} seconds) by a {role_label} rep. "
        f"Figure out why it was short. Return STRICT JSON only."
    )
    user = f"""This call lasted under {CONFIG['MIN_DURATION_SEC']} seconds. Classify why.

Return JSON with EXACTLY these keys:
- "drop_reason": one of {", ".join(f'"{d}"' for d in DROP_REASONS)}.
- "drop_note": one short sentence on what happened.
- "callback_time": if the customer gave a specific time/day to call back, state it
  briefly (e.g. "tomorrow evening", "after 6pm"); otherwise "".
- "sentiment": one of "Positive", "Neutral", "Negative".

Transcript:
\"\"\"{transcript[:3000]}\"\"\"

Return ONLY the JSON object."""

    r = _post_sarvam_chat({
        "model": CONFIG["SARVAM_CHAT_MODEL"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        "max_tokens": 3000,  # same reasoning-budget issue as analyze(), smaller margin
    }, timeout=60)
    msg = r.json()["choices"][0].get("message", {})
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _parse_json(content, default={
        "drop_reason": "other", "drop_note": "", "callback_time": "",
        "sentiment": "Neutral", "_analysis_failed": True})


def _parse_json(text, default):
    """Parse the model's JSON reply. The model sometimes wraps it in a markdown
    code fence, or escapes an apostrophe as \\' (invalid in strict JSON) — both
    are tolerated here before giving up, so a formatting quirk doesn't silently
    zero out a rep's score. On real failure, _analysis_failed=True is set so
    callers can skip scoring instead of recording a false 0/all-missed."""
    if not text:
        return {**default, "qa_notes": "Empty model response"}
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip())
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        candidate = m.group(0)
        for attempt in (candidate, candidate.replace("\\'", "'")):
            try:
                parsed = json.loads(attempt)
                parsed["_analysis_failed"] = False
                return parsed
            except Exception:
                continue
    return {**default, "qa_notes": "Parse error"}


# ----------------------------------------------------------------------------
# Orchestration
# ----------------------------------------------------------------------------
def reprocess():
    """Re-transcribe + re-analyze using ALREADY-DOWNLOADED local audio and the
    call metadata in results.json. Uses only Sarvam (no Callyzer). Fixes data
    without needing a valid Callyzer token. Run after clearing transcripts_cache.json."""
    rj = Path(CONFIG["OUT_DIR"]) / "results.json"
    if not rj.exists():
        print("No results.json found - run a normal --days pass first.")
        return
    data = json.loads(rj.read_text())
    calls = data.get("calls", [])
    rows = {c["call_id"]: c for c in calls}
    items = []
    for c in calls:
        ap = Path(CONFIG["AUDIO_DIR"]) / f"{c['call_id']}.mp3"
        if ap.exists():
            items.append({"call_id": c["call_id"], "audio": str(ap)})
    print(f"Found {len(items)} local recordings (of {len(calls)} calls) in "
          f"{CONFIG['AUDIO_DIR']}")
    if not items:
        print("No local audio found - the audio/ folder is empty. Re-run --days "
              "once Callyzer is back to re-download recordings.")
        return

    transcripts = transcribe_batch(items)

    print("  Analyzing transcripts...")
    total = len(transcripts)
    def _checkpoint():
        data["taxonomy"] = CALL_TYPES
        data["generated_at"] = dt.datetime.now().isoformat(timespec="seconds")
        rj.write_text(json.dumps(data, indent=2, ensure_ascii=False))

    for n, (cid, tr) in enumerate(transcripts.items(), 1):
        _bar(n, total)
        if n % 20 == 0:
            _checkpoint()   # save progress periodically so an interruption
                             # doesn't lose already-billed analysis calls
        c = rows.get(cid)
        if c is None:
            continue
        c["transcript"] = tr
        if not tr:
            continue
        role = c.get("role") or "sales"
        short = (c.get("duration_sec", 0) or 0) < CONFIG["MIN_DURATION_SEC"]
        try:
            if short:
                a = analyze_short_call(tr, role, name=c.get("rep", ""))
            else:
                a = analyze(tr, role, name=c.get("rep", ""))
        except Exception as e:
            print(f"\n   analysis error (call {cid}): {e}")
            continue
        failed = a.get("_analysis_failed", False)
        if short:
            c["call_type"] = ""
            c["summary"] = ""
            c["sentiment"] = a.get("sentiment", "Neutral")
            c["sentiment_reason"] = ""
            c["must_say"] = {}
            c["metric_values"] = {}
            c["qa_score"] = None
            c["qa_score_model"] = None
            c["qa_notes"] = ""
            c["action_items"] = []
            c["drop_reason"] = "other" if failed else a.get("drop_reason", "other")
            c["drop_note"] = "" if failed else a.get("drop_note", "")
            c["callback_time"] = "" if failed else a.get("callback_time", "")
            c["status"] = "error" if failed else "short"
        elif not failed and a.get("early_exit_reason"):
            # Call ran >= MIN_DURATION_SEC but ended through no fault of the
            # rep (customer disengaged almost immediately, or the location
            # was undeliverable) — treat like a short call, no qa_score.
            c["call_type"] = ""
            c["summary"] = ""
            c["sentiment"] = a.get("sentiment", "Neutral")
            c["sentiment_reason"] = ""
            c["must_say"] = {}
            c["metric_values"] = {}
            c["qa_score"] = None
            c["qa_score_model"] = None
            c["qa_notes"] = ""
            c["action_items"] = []
            c["drop_reason"] = a["early_exit_reason"]
            c["drop_note"] = a.get("drop_note", "")
            c["callback_time"] = ""
            c["status"] = "short"
        else:
            c["call_type"] = "" if failed else _valid_type(role, a.get("call_type", ""))
            c["summary"] = a.get("summary", "")
            c["sentiment"] = a.get("sentiment", "Neutral")
            c["sentiment_reason"] = a.get("sentiment_reason", "")
            c["must_say"] = a.get("must_say", {})
            c["metric_values"] = a.get("metric_values", {})
            c["qa_score"] = None if failed else weighted_score(role, c["call_type"], c["must_say"])
            c["qa_score_model"] = a.get("qa_score", 0)
            c["qa_notes"] = a.get("qa_notes", "")
            c["action_items"] = a.get("action_items", [])
            c["drop_reason"] = ""; c["drop_note"] = ""; c["callback_time"] = ""
            c["status"] = "error" if failed else "done"

    _checkpoint()
    build_dashboard(data)
    print(f"Done. Reprocessed {len(items)} calls with correct 1:1 mapping "
          f"-> results.json / dashboard.html")


def run(days):
    global _CSV_ROLES
    _CSV_ROLES = load_csv_roles()
    n_tagged = sum(1 for k in _CSV_ROLES if k.startswith("#"))
    print(f"Loaded {n_tagged} tagged employees from employees.csv")

    print(f"Fetching Callyzer logs (last {days} days)...")
    logs = fetch_call_logs(days)
    print(f"  {len(logs)} calls found.")

    # 1) download recordings (skip roles with no checklist, e.g. Ops, before
    #    spending anything on transcription). Calls with no recording (0-duration
    #    / didn't connect) never had audio to begin with — log them as "missed"
    #    directly, no download/transcription needed.
    items, by_id, role_hint = [], {}, {}
    results = []
    skipped_role = 0
    untagged = {}                       # rep -> count, for reps not in employees.csv
    missed = 0
    print("  Downloading recordings...")
    for i, log in enumerate(logs, 1):
        _bar(i, len(logs), prefix="")
        rec_url = log.get("call_recording_url")
        cid = str(log.get("id", i))
        pre = detect_role(log, allow_llm=False)
        if pre is None:                 # rep not found in employees.csv
            who = f"{log.get('emp_name','?')} ({log.get('emp_number','?')})"
            untagged[who] = untagged.get(who, 0) + 1
            continue
        if pre not in ANALYZED_ROLES:    # tagged but non-analyzed role (e.g. Ops)
            skipped_role += 1
            continue
        if not rec_url:
            results.append(_missed_row(log, pre))
            missed += 1
            continue
        audio = download_recording(rec_url, str(Path(CONFIG["AUDIO_DIR"]) / f"{cid}.mp3"))
        if not audio:
            continue
        items.append({"call_id": cid, "audio": audio})
        by_id[cid] = log
        role_hint[cid] = pre
    if skipped_role:
        print(f"  skipped {skipped_role} calls with a non-analyzed role (e.g. Ops).")
    if untagged:
        print(f"  skipped {sum(untagged.values())} calls from reps NOT in employees.csv:")
        for who, n in sorted(untagged.items(), key=lambda x: -x[1]):
            print(f"     - {who}: {n} call(s)  -> add a role for this number in employees.csv")
    if missed:
        print(f"  logged {missed} missed/unconnected calls (no recording, no analysis).")
    print(f"  {len(items)} recordings downloaded for analysis.")

    # 2) transcribe in batches (Sarvam Batch API)
    transcripts = transcribe_batch(items)

    # 3) analyze each transcript — full checklist if >= MIN_DURATION_SEC,
    #    otherwise a lightweight drop-reason classification.
    total = len(transcripts)
    print("  Analyzing transcripts...")
    for n, (cid, transcript) in enumerate(transcripts.items(), 1):
        _bar(n, total, prefix="")
        if not transcript:
            continue
        log = by_id[cid]
        role = role_hint.get(cid) or detect_role(log, transcript, allow_llm=True)
        if role not in ANALYZED_ROLES:
            continue
        short = (log.get("duration", 0) or 0) < CONFIG["MIN_DURATION_SEC"]
        try:
            if short:
                analysis = analyze_short_call(transcript, role, name=log.get("emp_name", ""))
                results.append(_short_row(log, transcript, analysis, role))
            else:
                analysis = analyze(transcript, role, name=log.get("emp_name", ""))
                results.append(_row(log, transcript, analysis, role))
        except Exception as e:
            print(f"\n   analysis error (call {cid}): {e}")
            continue

    _write(results, days)
    scored = sum(1 for r in results if r["status"] == "done")
    print(f"Done. {len(results)} calls captured ({scored} fully scored, "
          f"{missed} missed, {sum(1 for r in results if r['status']=='short')} short) "
          f"-> results.json / dashboard.html")


def _row(log, transcript, analysis, role):
    # A call whose analysis failed to parse gets NO call_type guess and NO
    # score (qa_score stays null) instead of defaulting to the first call type
    # and an all-missed 0 — that 0 would look like the rep's fault when it's
    # actually a model/formatting failure. The dashboard excludes qa_score:null
    # calls from every average and flags them for re-analysis instead.
    failed = analysis.get("_analysis_failed", False)
    # A call over MIN_DURATION_SEC can still end through no fault of the rep —
    # customer refuses to engage almost immediately, or the location turns out
    # undeliverable — before most checkpoints could reasonably come up. Treat
    # it like a short call (drop_reason, no qa_score) instead of penalizing
    # the rep for checkpoints they never had a chance to hit.
    if not failed and analysis.get("early_exit_reason"):
        return _short_row(log, transcript, {
            "drop_reason": analysis["early_exit_reason"],
            "drop_note": analysis.get("drop_note", ""),
            "callback_time": "",
            "sentiment": analysis.get("sentiment", "Neutral"),
            "_analysis_failed": False,
        }, role)
    call_type = "" if failed else _valid_type(role, analysis.get("call_type", ""))
    must_say = analysis.get("must_say", {})
    return {
        "call_id": str(log.get("id", "")),
        "role": role,
        "date": _fmt_date(log.get("call_date") or log.get("call_time")),
        "rep": log.get("emp_name") or log.get("emp_number") or "Unknown",
        "client": log.get("client_name") or log.get("client_number") or "Unknown",
        "client_number": (f"{log.get('client_country_code')}-{log.get('client_number')}"
                          if log.get("client_country_code") and log.get("client_number")
                          else (log.get("client_number") or "")),
        "duration_sec": log.get("duration", 0),
        "direction": log.get("call_type", ""),          # Callyzer: Incoming/Outgoing
        "call_type": call_type,                         # AI: onboarding/consultation/...
        "transcript": transcript,
        "summary": analysis.get("summary", ""),
        "sentiment": analysis.get("sentiment", "Neutral"),
        "sentiment_reason": analysis.get("sentiment_reason", ""),
        # qa_score is DERIVED from weighted checkpoint coverage, so it always
        # matches the checklist. The model's own rating is kept as qa_score_model.
        "qa_score": None if failed else weighted_score(role, call_type, must_say),
        "qa_score_model": analysis.get("qa_score", 0),
        "qa_notes": analysis.get("qa_notes", ""),
        "must_say": must_say,
        "metric_values": analysis.get("metric_values", {}),
        "action_items": analysis.get("action_items", []),
        "drop_reason": "", "drop_note": "", "callback_time": "",
        "status": "error" if failed else "done",
    }


def _short_row(log, transcript, analysis, role):
    """A call with a recording but under MIN_DURATION_SEC — too short for the
    full checklist, so it gets a drop-reason instead of qa_score/must_say."""
    failed = analysis.get("_analysis_failed", False)
    return {
        "call_id": str(log.get("id", "")),
        "role": role,
        "date": _fmt_date(log.get("call_date") or log.get("call_time")),
        "rep": log.get("emp_name") or log.get("emp_number") or "Unknown",
        "client": log.get("client_name") or log.get("client_number") or "Unknown",
        "client_number": (f"{log.get('client_country_code')}-{log.get('client_number')}"
                          if log.get("client_country_code") and log.get("client_number")
                          else (log.get("client_number") or "")),
        "duration_sec": log.get("duration", 0),
        "direction": log.get("call_type", ""),
        "call_type": "",
        "transcript": transcript,
        "summary": "",
        "sentiment": analysis.get("sentiment", "Neutral"),
        "sentiment_reason": "",
        "qa_score": None, "qa_score_model": None, "qa_notes": "",
        "must_say": {}, "metric_values": {}, "action_items": [],
        "drop_reason": "other" if failed else analysis.get("drop_reason", "other"),
        "drop_note": "" if failed else analysis.get("drop_note", ""),
        "callback_time": "" if failed else analysis.get("callback_time", ""),
        "status": "error" if failed else "short",
    }


def _missed_row(log, role):
    """A call with no recording (0-duration / didn't connect) — nothing to
    transcribe, logged for volume + callback-reminder visibility only."""
    return {
        "call_id": str(log.get("id", "")),
        "role": role,
        "date": _fmt_date(log.get("call_date") or log.get("call_time")),
        "rep": log.get("emp_name") or log.get("emp_number") or "Unknown",
        "client": log.get("client_name") or log.get("client_number") or "Unknown",
        "client_number": (f"{log.get('client_country_code')}-{log.get('client_number')}"
                          if log.get("client_country_code") and log.get("client_number")
                          else (log.get("client_number") or "")),
        "duration_sec": log.get("duration", 0),
        "direction": log.get("call_type", ""),
        "call_type": "",
        "transcript": "",
        "summary": "",
        "sentiment": "Neutral",
        "sentiment_reason": "",
        "qa_score": None, "qa_score_model": None, "qa_notes": "",
        "must_say": {}, "metric_values": {}, "action_items": [],
        "drop_reason": "no_answer",
        "drop_note": "No recording — call did not connect.",
        "callback_time": "",
        "status": "missed",
    }


def _valid_type(role, t):
    """Keep the model's call_type only if it's a known type for this role."""
    t = (t or "").strip().lower().replace(" ", "_")
    return t if t in CALL_TYPES.get(role, {}) else (
        next(iter(CALL_TYPES.get(role, {})), ""))


def weighted_score(role, call_type, must_say):
    """QA score (0-100) = weighted % of this call type's checkpoints covered.
    Each checkpoint carries a `weight` (default 1); the score is the share of
    total possible weight the rep actually earned. This ties the QA number
    directly to the checklist instead of being a separate model guess."""
    cps = CALL_TYPES.get(role, {}).get(call_type, {}).get("checkpoints", [])
    if not cps:
        return 0
    total = sum(c.get("weight", 1) for c in cps)
    got = sum(c.get("weight", 1) for c in cps if (must_say or {}).get(c["id"]))
    return round(100 * got / total) if total else 0


def _fmt_date(v):
    if not v:
        return dt.date.today().isoformat()
    try:
        return dt.datetime.utcfromtimestamp(int(v)).date().isoformat()
    except Exception:
        return str(v)[:10]


def _write(results, days):
    out = Path(CONFIG["OUT_DIR"])
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "period_days": days,
        "taxonomy": CALL_TYPES,          # role -> call_type -> {desc, checkpoints}
        "calls": results,
    }
    (out / "results.json").write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    # flat CSV (per-checkpoint columns aren't stable across call types, so keep summary)
    with open(out / "results.csv", "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["call_id", "role", "call_type", "date", "rep", "client",
                    "client_number", "duration_sec", "direction", "status", "sentiment",
                    "qa_score", "checkpoints_met", "checkpoints_total",
                    "drop_reason", "drop_note", "callback_time",
                    "summary", "action_items"])
        for r in results:
            ms = r.get("must_say", {})
            met = sum(1 for v in ms.values() if v)
            w.writerow([r["call_id"], r["role"], r.get("call_type", ""), r["date"],
                        r["rep"], r["client"], r.get("client_number", ""),
                        r["duration_sec"], r.get("direction", ""), r.get("status", ""),
                        r["sentiment"], r["qa_score"], met, len(ms),
                        r.get("drop_reason", ""), r.get("drop_note", ""),
                        r.get("callback_time", ""), r["summary"],
                        "; ".join(r["action_items"])])
    build_dashboard(payload)


def build_dashboard(payload):
    """Inject data into dashboard_template.html -> dashboard.html"""
    tpl = Path(CONFIG["OUT_DIR"]) / "dashboard_template.html"
    if not tpl.exists():
        return
    html = tpl.read_text()
    html = html.replace("/*__DATA__*/{}",
                        "/*__DATA__*/" + json.dumps(payload, ensure_ascii=False))
    (Path(CONFIG["OUT_DIR"]) / "dashboard.html").write_text(html)
    print("dashboard.html updated.")


def check(days):
    """Verify the Callyzer connection only — no transcription, no Sarvam calls."""
    if CONFIG["CALLYZER_TOKEN"].startswith("PASTE"):
        print("CALLYZER_TOKEN is not set. Run:  export CALLYZER_TOKEN=\"...\"")
        return
    print(f"Testing Callyzer connection (last {days} days)...")
    try:
        logs = fetch_call_logs(days)
    except Exception as e:
        print(f"  FAILED: {e}")
        print("  Check the token, admin permission, and that API access is enabled.")
        return
    print(f"  OK — Callyzer returned {len(logs)} calls.")
    with_rec = sum(1 for l in logs if l.get("call_recording_url"))
    print(f"  {with_rec} of them have a recording URL "
          f"({round(100*with_rec/max(len(logs),1))}%).")

    # per-rep recording coverage
    global _CSV_ROLES
    if _CSV_ROLES is None:
        _CSV_ROLES = load_csv_roles()
    stats = {}
    for l in logs:
        rep = l.get("emp_name") or l.get("emp_number") or "?"
        s = stats.setdefault(rep, {"total": 0, "rec": 0})
        s["total"] += 1
        if l.get("call_recording_url"):
            s["rec"] += 1
    print("\n  Recording coverage by rep (calls with audio / total):")
    print(f"  {'REP':<24}{'ROLE':<12}{'REC':>5}{'TOTAL':>7}{'%':>6}")
    for rep, s in sorted(stats.items(), key=lambda x: -x[1]["total"]):
        role = detect_role({"emp_name": rep}, allow_llm=False) or "-"
        pct = round(100 * s["rec"] / max(s["total"], 1))
        flag = "  <-- recording OFF?" if s["rec"] == 0 else ("  (partial)" if pct < 50 else "")
        print(f"  {rep[:23]:<24}{role:<12}{s['rec']:>5}{s['total']:>7}{pct:>5}%{flag}")
    print("\n  Reps with 0 recordings likely need call recording enabled on their "
          "phone in the Callyzer app.")

    if logs:
        print("\n  Sample record fields:", ", ".join(sorted(logs[0].keys())))
    sk = "set" if not CONFIG["SARVAM_API_KEY"].startswith("PASTE") else "NOT set"
    print(f"  SARVAM_API_KEY is {sk}.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=CONFIG["DEFAULT_DAYS"])
    ap.add_argument("--demo", action="store_true", help="rebuild dashboard from sample_results.json")
    ap.add_argument("--check", action="store_true", help="test Callyzer connection only (no transcription)")
    ap.add_argument("--employees", action="store_true", help="export all employees to employees.csv for role tagging")
    ap.add_argument("--rebuild", action="store_true", help="rebuild dashboard.html from existing results.json (no API calls)")
    ap.add_argument("--reprocess", action="store_true", help="re-transcribe+analyze from local audio/ (Sarvam only, no Callyzer)")
    args = ap.parse_args()

    if args.reprocess:
        reprocess()
    elif args.demo:
        sample = Path(CONFIG["OUT_DIR"]) / "sample_results.json"
        build_dashboard(json.loads(sample.read_text()))
        print("Demo dashboard built from sample_results.json")
    elif args.rebuild:
        rj = Path(CONFIG["OUT_DIR"]) / "results.json"
        build_dashboard(json.loads(rj.read_text()))
        print("Dashboard rebuilt from results.json (with transcript viewer).")
    elif args.employees:
        export_employees()
    elif args.check:
        check(args.days)
    else:
        run(args.days)
