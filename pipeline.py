#!/usr/bin/env python3
"""
Kenko Call Analysis Pipeline
============================
Weekly pipeline: Callyzer call logs  ->  download recordings  ->  Sarvam
speech-to-text (saaras:v4)  ->  LLM analysis (summary / sentiment / QA /
must-say checklist)  ->  results.json  ->  dashboard.html

The analysis step runs on Sarvam or Claude depending on
CONFIG["ANALYSIS_PROVIDER"] (env var ANALYSIS_PROVIDER, default "sarvam" as of
2026-10-17 pending Anthropic credits -- flip to "claude" when ready, see
CLAUDE.md "Analysis engine").

Run:  python pipeline.py                 (uses last 7 days by default)
      python pipeline.py --days 7
      python pipeline.py --demo           (no API calls, regenerates sample data)

Requirements:
      pip install requests sarvamai anthropic pydub
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

import drive_cache          # Google-Drive-backed cache sync; inert unless enabled()

# ----------------------------------------------------------------------------
# CONFIG  — edit this section
# ----------------------------------------------------------------------------
CONFIG = {
    # --- Callyzer ---
    "CALLYZER_TOKEN": os.environ.get("CALLYZER_TOKEN", "PASTE_CALLYZER_BEARER_TOKEN"),
    "CALLYZER_BASE": "https://api1.callyzer.co/api/v2.2",

    # --- Sarvam (speech-to-text, always; analysis too when ANALYSIS_PROVIDER=sarvam) ---
    "SARVAM_API_KEY": os.environ.get("SARVAM_API_KEY", "PASTE_SARVAM_API_KEY"),
    "SARVAM_STT_MODEL": "saaras:v4",     # Batch API model (handles up to 2h/file);
                                         # moved from v3 2026-10-17 per Sarvam's
                                         # retirement notice -- lower WER, better
                                         # Indic/code-mixed accuracy, drop-in swap
    "SARVAM_LANGUAGE": "en-IN",          # calls are mostly English
    "SARVAM_CHAT_MODEL": "sarvam-105b",  # analysis step, only used when
                                         # ANALYSIS_PROVIDER=sarvam
    "DIARIZATION": True,                 # separate agent vs customer
    "NUM_SPEAKERS": 2,
    "BATCH_SIZE": 20,                    # max files per Sarvam batch job

    # --- Analysis provider: role classification, QA scoring, must-say checklist ---
    # One-line switch between the two -- same CALL_TYPES/prompts/schema-derived
    # instructions feed both, see _call_llm(). Flip to "claude" the moment you
    # have Anthropic credits (export ANALYSIS_PROVIDER=claude, or edit here).
    "ANALYSIS_PROVIDER": os.environ.get("ANALYSIS_PROVIDER", "sarvam"),  # "sarvam" | "claude"
    "ANTHROPIC_API_KEY": os.environ.get("ANTHROPIC_API_KEY", "PASTE_ANTHROPIC_API_KEY"),
    "CLAUDE_MODEL": "claude-sonnet-5",
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
            # Redesigned 2026-09-28 per Pranav's new weightage table (weight scale
            # is now 1-5, total possible weight 56 -- not the old 1-3 scale nutrition
            # still uses).
            # Redesigned again 2026-10-09: unlike nutrition's checkpoints (where
            # order of coverage is NOT scored), enquiry now scores order too --
            # order_weighted=True means weighted_score() blends 50% checkpoint
            # coverage with 50% adherence to the checkpoint order below (the
            # list order IS the required pitch flow). See weighted_score().
            "order_weighted": True,
            "desc": "First contact with a prospect — discovery / new enquiry.",
            "checkpoints": [
                {"id": "intro", "label": "Intro", "weight": 3,
                 "criteria": "Introduces themselves by name and mentions The Kenko Life."},
                {"id": "healthy_food_doorstep", "label": "Healthy food to your doorstep", "weight": 5,
                 "criteria": "States the core pitch — healthy food delivered to the "
                             "customer's doorstep."},
                {"id": "macros_calories", "label": "Macro & calorie counted meals", "weight": 5,
                 "criteria": "Mentions that meals are macro- and calorie-counted."},
                {"id": "nutritionist_handholding", "label": "Nutritionist handholds you through the journey", "weight": 5,
                 "criteria": "Explains that a nutritionist/coach handholds the customer "
                             "through their journey, not just at signup."},
                {"id": "menu_no_repeat_month", "label": "Menu doesn't repeat for a month", "weight": 5,
                 "criteria": "States that the menu doesn't repeat for a month."},
                {"id": "multiple_cuisines", "label": "Multiple cuisines", "weight": 5,
                 "criteria": "Mentions the variety of cuisines on offer, whether the "
                             "customer asked about the menu/food/cuisine type or the rep "
                             "brought it up on their own. Proactively mentioning cuisine "
                             "variety counts the same as answering a direct question about "
                             "it — both show the customer this information either way."},
                {"id": "delivery_6_days", "label": "6 days a week delivery", "weight": 5,
                 "criteria": "Explains that delivery happens 6 days a week."},
                {"id": "pause_carry_forward_example", "label": "Pause & carry-forward flexibility, with example", "weight": 5,
                 "criteria": "Explains the pause/carry-forward flexibility AND gives a "
                             "concrete example (e.g. if travelling, pause the next 3 days' "
                             "meals by 6 PM the previous day). Explaining the flexibility "
                             "without a concrete example does not count as a hit."},
                {"id": "discovery_duration", "label": "Discovery question: what are you looking for?", "weight": 4,
                 "criteria": "Asks a discovery question along the lines of \"may I know what "
                             "exactly you're looking for?\" AND the customer responds with a "
                             "duration. If the rep asks but the customer never states a "
                             "duration, this does not count as a hit."},
                {"id": "pricing", "label": "Pricing", "weight": 5,
                 "criteria": "States the price and any current offer clearly."},
                {"id": "freshness", "label": "Freshness", "weight": 5,
                 "criteria": "Explains that meals arrive refrigerated/below room temperature "
                             "and just need to be refrigerated/microwaved before eating."},
                {"id": "delivery_address", "label": "Delivery address taken", "weight": 2,
                 "criteria": "Asks for and notes the customer's delivery address."},
                {"id": "next_steps_close", "label": "Next steps to close", "weight": 2,
                 "criteria": "Sets clear next steps to close the sale."},
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
# Claude — client/call helper
# ----------------------------------------------------------------------------
_CLAUDE_CLIENT = None

def _claude_client():
    """Lazily-constructed singleton client. max_retries covers 429/5xx/
    connection errors automatically (SDK default backoff) so callers don't
    need their own retry loop."""
    global _CLAUDE_CLIENT
    if _CLAUDE_CLIENT is None:
        import anthropic
        _CLAUDE_CLIENT = anthropic.Anthropic(
            api_key=CONFIG["ANTHROPIC_API_KEY"], max_retries=5)
    return _CLAUDE_CLIENT


def _call_claude(system, user, schema, max_tokens=8000, effort="high"):
    """Call Claude with a JSON-schema-constrained response — output_config.format
    guarantees the returned text block is valid JSON matching `schema`, so
    callers can json.loads() it directly with no markdown-fence/escaping
    cleanup (unlike the old Sarvam chat-completions path)."""
    response = _claude_client().messages.create(
        model=CONFIG["CLAUDE_MODEL"],
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={
            "effort": effort,
            "format": {"type": "json_schema", "schema": schema},
        },
        messages=[{"role": "user", "content": user}],
    )
    text = next(b.text for b in response.content if b.type == "text")
    return json.loads(text)


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


def _parse_json(text):
    """Parse a free-text model reply into JSON (Sarvam path only — Claude's
    output_config.format guarantees valid JSON already, so it skips this
    entirely). Tolerates a markdown code fence or an escaped apostrophe
    (\\', invalid in strict JSON) before giving up. Raises ValueError on real
    failure so callers hit the same except-block as a Claude API error,
    instead of silently fabricating an all-missed score."""
    if not text:
        raise ValueError("empty model response")
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```\s*$", "", text.strip())
    m = re.search(r"\{.*\}", cleaned, re.DOTALL)
    if m:
        candidate = m.group(0)
        for attempt in (candidate, candidate.replace("\\'", "'")):
            try:
                return json.loads(attempt)
            except Exception:
                continue
    raise ValueError(f"could not parse JSON from model response: {text[:200]!r}")


def _call_llm(system, user, schema=None, max_tokens=8000, effort="high"):
    """Dispatches to Claude or Sarvam per CONFIG['ANALYSIS_PROVIDER'] — same
    system/user prompt text drives both, since the prompt already spells out
    the exact JSON shape in plain English for every field (Claude additionally
    gets `schema` as a structural guarantee; Sarvam relies on the text alone,
    parsed via _parse_json). Switching providers is the one-line CONFIG change
    described above ANALYSIS_PROVIDER — nothing else in analyze()/
    analyze_short_call()/_classify_role() needs to change."""
    if CONFIG["ANALYSIS_PROVIDER"] == "claude":
        return _call_claude(system, user, schema=schema, max_tokens=max_tokens, effort=effort)
    r = _post_sarvam_chat({
        "model": CONFIG["SARVAM_CHAT_MODEL"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": 0.2,
        # sarvam-105b is a reasoning model — its chain-of-thought eats into the
        # completion budget before it reaches the final JSON, so callers pass
        # a generous max_tokens (see analyze()'s comment for specifics).
        "max_tokens": max_tokens,
    })
    msg = r.json()["choices"][0].get("message", {})
    # sarvam-105b can return content=None with the text under reasoning_content
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return _parse_json(content)


# ----------------------------------------------------------------------------
# Callyzer
# ----------------------------------------------------------------------------
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


def ist_today_at(hour, minute=0):
    """Epoch seconds for today's `hour`:`minute` India time (Asia/Kolkata),
    regardless of the machine's own timezone -- the cloud sandbox this runs in
    is not guaranteed to be IST, so naive dt.datetime.now() would silently
    compute the wrong boundary. Used for the twice-daily sales reports (Mid
    Day / End of Day), which need a same-calendar-day window rather than
    fetch_call_logs's default rolling N-day window."""
    from zoneinfo import ZoneInfo
    now_ist = dt.datetime.now(ZoneInfo("Asia/Kolkata"))
    start = now_ist.replace(hour=hour, minute=minute, second=0, microsecond=0)
    return int(start.timestamp())


def fetch_call_logs(days, since_ts=None):
    now = int(time.time())
    call_from = since_ts if since_ts is not None else now - days * 86400
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
    # Mirror to Drive so a run killed mid-transcription doesn't lose the
    # transcripts it already paid Sarvam for. Throttled (this is called after
    # every single transcription, and the file runs to several MB) and a no-op
    # unless --cache-sync-drive was passed.
    drive_cache.push_throttled(CONFIG["OUT_DIR"], "transcripts_cache.json", min_seconds=180)


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
# Claude — Analysis (role classification, QA scoring, must-say checklist)
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


_ROLE_SCHEMA = {
    "type": "object",
    "properties": {"role": {"type": "string", "enum": ["sales", "nutrition"]}},
    "required": ["role"],
    "additionalProperties": False,
}

def _classify_role(transcript):
    result = _call_llm(
        system="You classify Kenko Life phone call transcripts by role. "
               "Return STRICT JSON only.",
        user="Classify this call as either 'sales' (pitching/selling a plan to a "
             "prospect) or 'nutrition' (a nutritionist reviewing an existing "
             "client's diet/progress). Return a JSON object: {\"role\": \"sales\"} "
             "or {\"role\": \"nutrition\"}.\n\n" + transcript[:3000],
        schema=_ROLE_SCHEMA, max_tokens=2048, effort="low")
    return result.get("role", "sales")


def _analyze_schema(type_keys, drop_reasons):
    """must_say/metric_values stay open objects (keyed by whichever
    checkpoint ids belong to the call_type the model picks) rather than
    fixed per-checkpoint properties, since the checkpoint set differs by
    call_type and the schema can't know in advance which one will be chosen."""
    return {
        "type": "object",
        "properties": {
            "call_type": {"type": "string", "enum": type_keys},
            "summary": {"type": "string"},
            "sentiment": {"type": "string", "enum": ["Positive", "Neutral", "Negative"]},
            "sentiment_reason": {"type": "string"},
            "early_exit_reason": {"type": "string", "enum": [""] + drop_reasons},
            "drop_note": {"type": "string"},
            "qa_score": {"type": "integer"},
            "qa_notes": {"type": "string"},
            # Combined per-checkpoint judgment: whether it was covered AND
            # what detail was mentioned, decided together in one object so
            # the two can't drift apart -- previously must_say and
            # metric_values were separately-generated JSON blobs with no
            # structural link, so the model could write real evidence into
            # metric_values for a checkpoint it independently marked false in
            # must_say (observed live: a rep clearly explained macro/calorie
            # counting, metric_values captured that detail verbatim, but
            # must_say still said false, showing "Missed" right next to the
            # proof it wasn't). This is a fixed-shape ARRAY (not a
            # dynamically-keyed object -- Claude's schema requires
            # additionalProperties: false, which can't express "one entry per
            # checkpoint id" since the id set depends on the call_type the
            # model just picked) so every field is schema-enforced JSON, same
            # as checkpoint_order below -- an earlier version used a bare
            # "type": "string" here relying on the model to hand-produce
            # valid JSON inside it, which free-tested as unreliable (it often
            # wrote prose like "intro, pricing hit; freshness missed"
            # instead of real JSON). analyze() converts this array into the
            # must_say/metric_values dicts every caller expects.
            "checkpoints": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "covered": {"type": "boolean"},
                        "detail": {"type": "string"},
                    },
                    "required": ["id", "covered", "detail"],
                    "additionalProperties": False,
                },
            },
            # Each entry tags whether the customer asked about this checkpoint
            # first (customer_initiated=true) or the rep brought it up on their
            # own -- used by _order_adherence() to exempt "the customer asked,
            # the rep correctly answered" moments from the pitch-order penalty
            # (answering a direct question isn't a pitch-flow failure).
            "checkpoint_order": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "id": {"type": "string"},
                        "customer_initiated": {"type": "boolean"},
                    },
                    "required": ["id", "customer_initiated"],
                    "additionalProperties": False,
                },
            },
            "action_items": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["call_type", "summary", "sentiment", "sentiment_reason",
                     "early_exit_reason", "drop_note", "qa_score", "qa_notes",
                     "checkpoints", "checkpoint_order", "action_items"],
        "additionalProperties": False,
    }


def analyze(transcript, role, name=""):
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

Fill in these fields:
- "call_type": one of {type_keys}.
- "summary": 2-3 sentence summary of the call.
- "sentiment": the customer's overall sentiment.
- "sentiment_reason": one short sentence.
- "early_exit_reason": "" if the rep had a genuine opportunity to work through
  the checklist (even if the customer ultimately declined at the end). Otherwise
  set it ONLY when the call ended early through no fault of the rep (e.g.
  customer refused to engage almost immediately, or the location turned out to
  be undeliverable), before most checkpoints could reasonably come up.
- "drop_note": if early_exit_reason is set, one short sentence on what happened;
  otherwise "".
- "qa_score": integer 0-100 rating the rep's overall call quality FOR THAT CALL TYPE.
  Only meaningful if early_exit_reason is "" — set to 0 if early_exit_reason is set.
- "qa_notes": one or two sentences of coaching feedback. If early_exit_reason is
  set, note what the rep had covered well before the customer cut the call short.
- "checkpoints": an array with ONE ENTRY per checkpoint id of the CHOSEN
  call_type, each {{"id": ..., "covered": true/false, "detail": "..."}}.
  Decide "covered" and "detail" BOTH together for each checkpoint so they can
  never contradict each other: if "detail" holds real evidence from the call,
  "covered" must be true; if "covered" is false, "detail" must be "". "detail"
  is the actual value or detail mentioned on the call, where the checkpoint
  involves one — e.g. an actual weight ("78kg"), a
  measurement, a price quoted, a specific date, or a short summary of what the
  client said (their diet, activity level, symptoms, feedback). Use "" for
  checkpoints that are purely behavioral (e.g. a greeting) or where no
  concrete detail was mentioned, even if covered is true. Fill this in based
  on what ACTUALLY came up on the call, even if early_exit_reason is set — if
  the rep covered 4 checkpoints before the customer disengaged, mark those 4
  covered:true. This isn't used for scoring when early_exit_reason is set (the
  rep isn't penalized for the rest), only to show what progress was made.
- "checkpoint_order": an array of {{"id": ..., "customer_initiated": ...}}
  objects — ONE ENTRY per checkpoint id marked covered:true in "checkpoints" —
  listed in the order they actually came up on the call (earliest first). E.g.
  if the rep covered pricing before intro, pricing comes first in this array.
  "customer_initiated" is true if the CUSTOMER brought this topic up first
  (asked a question about it, or raised it themselves) and the rep responded
  to that; false if the REP brought it up on their own as part of running the
  pitch. This distinction matters: a rep who answers a question the customer
  asked out of the normal pitch order is doing their job correctly, not
  failing to follow the pitch flow — don't let "the customer asked about
  pricing early" make the call look disordered. Fill this in even if
  early_exit_reason is set, same as "checkpoints".
- "action_items": array of short strings (follow-ups / next steps).

Transcript:
\"\"\"{transcript[:12000]}\"\"\"

Return ONLY the JSON object."""

    try:
        result = _call_llm(system, user,
                            schema=_analyze_schema(list(types.keys()), DROP_REASONS),
                            # 16000, not 8000: "checkpoints" is now a full
                            # {id, covered, detail} object per checkpoint
                            # (up to 13 for sales.enquiry) instead of two flat
                            # maps, and thinking tokens share this same budget
                            # -- 8000 was observed truncating mid-response
                            # ("Unterminated string...") on a live test call.
                            max_tokens=16000, effort="high")
        result["sentiment"] = _valid_sentiment(result.get("sentiment"))
        # "checkpoints" unifies the covered/detail judgment per checkpoint
        # (see _analyze_schema) so they can't drift apart -- split back into
        # the must_say/metric_values dicts every other caller expects.
        checkpoints = result.pop("checkpoints", [])
        if isinstance(checkpoints, str):
            try:
                checkpoints = json.loads(checkpoints) if checkpoints.strip() else []
            except ValueError:
                checkpoints = []
        must_say, metric_values = {}, {}
        for item in checkpoints if isinstance(checkpoints, list) else []:
            cid = isinstance(item, dict) and item.get("id")
            if not cid:
                continue
            must_say[cid] = bool(item.get("covered"))
            metric_values[cid] = item.get("detail", "")
        result["must_say"] = must_say
        result["metric_values"] = metric_values
        result["_analysis_failed"] = False
        return result
    except Exception as e:
        print(f"\n   analysis error: {e}")
        return {
            "call_type": "", "summary": "", "sentiment": "Neutral",
            "sentiment_reason": "", "early_exit_reason": "", "drop_note": "",
            "qa_score": 0, "qa_notes": "", "must_say": {}, "checkpoint_order": [],
            "metric_values": {}, "action_items": [], "_analysis_failed": True}


# Calls under MIN_DURATION_SEC still get a recording sometimes (quick connects,
# fast hangups) — not enough substance for the full checklist, but worth knowing
# WHY the call was short. Feeds rep callback reminders later (see CLAUDE.md).
DROP_REASONS = ["call_later", "not_interested", "wrong_number", "no_answer",
                "busy_hangup", "undeliverable_location", "other"]

_SHORT_CALL_SCHEMA = {
    "type": "object",
    "properties": {
        "drop_reason": {"type": "string", "enum": DROP_REASONS},
        "drop_note": {"type": "string"},
        "callback_time": {"type": "string"},
        "sentiment": {"type": "string", "enum": ["Positive", "Neutral", "Negative"]},
    },
    "required": ["drop_reason", "drop_note", "callback_time", "sentiment"],
    "additionalProperties": False,
}

def analyze_short_call(transcript, role, name=""):
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

    try:
        result = _call_llm(system, user, schema=_SHORT_CALL_SCHEMA,
                            max_tokens=3000, effort="low")
        result["sentiment"] = _valid_sentiment(result.get("sentiment"))
        result["_analysis_failed"] = False
        return result
    except Exception as e:
        print(f"\n   analysis error: {e}")
        return {"drop_reason": "other", "drop_note": "", "callback_time": "",
                "sentiment": "Neutral", "_analysis_failed": True}


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
            c["checkpoint_order"] = []
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
            # was undeliverable) — treat like a short call, no qa_score. Keep
            # whatever checklist progress the rep made before the cutoff for
            # the call-detail view (coaching signal, no scoring impact).
            c["call_type"] = _valid_type(role, a.get("call_type", ""))
            c["summary"] = a.get("summary", "")
            c["sentiment"] = a.get("sentiment", "Neutral")
            c["sentiment_reason"] = ""
            c["must_say"] = a.get("must_say", {})
            c["checkpoint_order"] = a.get("checkpoint_order", [])
            c["metric_values"] = a.get("metric_values", {})
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
            c["checkpoint_order"] = a.get("checkpoint_order", [])
            c["metric_values"] = a.get("metric_values", {})
            c["qa_score"] = None if failed else weighted_score(
                role, c["call_type"], c["must_say"], c["checkpoint_order"])
            c["qa_score_model"] = a.get("qa_score", 0)
            c["qa_notes"] = a.get("qa_notes", "")
            c["action_items"] = a.get("action_items", [])
            c["drop_reason"] = ""; c["drop_note"] = ""; c["callback_time"] = ""
            c["status"] = "error" if failed else "done"

    _checkpoint()
    build_dashboard(data)
    print(f"Done. Reprocessed {len(items)} calls with correct 1:1 mapping "
          f"-> results.json / dashboard.html")


def run(days, since_ts=None):
    global _CSV_ROLES
    _CSV_ROLES = load_csv_roles()
    n_tagged = sum(1 for k in _CSV_ROLES if k.startswith("#"))
    print(f"Loaded {n_tagged} tagged employees from employees.csv")

    # Pull the shared caches down FIRST -- before results.json is read for the
    # skip-already-analyzed set below, and before transcribe_batch() consults
    # transcripts_cache.json. A cloud run starts from a fresh git clone, so
    # without this there is nothing on disk to resume from. No-op locally.
    if drive_cache.is_enabled():
        print("Syncing caches from Google Drive...")
        drive_cache.pull(CONFIG["OUT_DIR"])

    if since_ts is not None:
        from zoneinfo import ZoneInfo
        since_label = dt.datetime.fromtimestamp(
            since_ts, tz=ZoneInfo("Asia/Kolkata")).strftime("%Y-%m-%d %H:%M IST")
        print(f"Fetching Callyzer logs (since {since_label})...")
    else:
        print(f"Fetching Callyzer logs (last {days} days)...")
    logs = fetch_call_logs(days, since_ts=since_ts)
    print(f"  {len(logs)} calls found.")

    # Intraday reruns (Mid Day / End of Day both pass since_ts="today midnight")
    # re-fetch the SAME calls from earlier today. Reuse any call already
    # resolved by an earlier run today instead of re-billing Claude/Sarvam for
    # it -- only "error" rows are retried, since those never got a real result.
    # Scoped to since_ts (not the plain --days N historical path) since that's
    # the only caller that re-requests a window it already processed.
    existing_by_id = {}
    if since_ts is not None:
        try:
            prior = json.loads((Path(CONFIG["OUT_DIR"]) / "results.json").read_text())
            existing_by_id = {c["call_id"]: c for c in prior.get("calls", [])
                               if c.get("status") in ("done", "short", "missed")}
        except (FileNotFoundError, json.JSONDecodeError):
            pass

    # 1) download recordings (skip roles with no checklist, e.g. Ops, before
    #    spending anything on transcription). Calls with no recording (0-duration
    #    / didn't connect) never had audio to begin with — log them as "missed"
    #    directly, no download/transcription needed.
    items, by_id, role_hint = [], {}, {}
    results = []
    skipped_role = 0
    untagged = {}                       # rep -> count, for reps not in employees.csv
    missed = 0
    reused = 0
    print("  Downloading recordings...")
    for i, log in enumerate(logs, 1):
        _bar(i, len(logs), prefix="")
        cid = str(log.get("id", i))
        if cid in existing_by_id:
            results.append(existing_by_id[cid])
            reused += 1
            continue
        rec_url = log.get("call_recording_url")
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
    if reused:
        print(f"  reused {reused} call(s) already analyzed in an earlier run today.")
    print(f"  {len(items)} recordings downloaded for analysis.")

    # 2) transcribe in batches (Sarvam Batch API)
    transcripts = transcribe_batch(items)

    # 3) analyze each transcript — full checklist if >= MIN_DURATION_SEC,
    #    otherwise a lightweight drop-reason classification.
    # Checkpointed to results.json every CHECKPOINT_EVERY calls. A cloud
    # routine's backgrounded run has a hard ~30min cap; analyzing 100+ calls
    # one at a time can exceed it. Transcripts survive a kill either way
    # (cached per-call in transcripts_cache.json), but analysis results
    # didn't -- without this, a kill mid-loop lost everything analyzed so
    # far. With it, the next run picks the checkpoint up via the
    # existing_by_id reuse above instead of redoing the whole day.
    CHECKPOINT_EVERY = 10
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
        if n % CHECKPOINT_EVERY == 0:
            _write(results, days)
            # Mirror the checkpoint to Drive (throttled) so the ~30min
            # background-task kill can't discard analysis already paid for.
            drive_cache.push_throttled(CONFIG["OUT_DIR"], "results.json", min_seconds=120)

    _write(results, days)
    # Final, unthrottled push so the next scheduled run resumes from a complete
    # picture rather than the last throttled checkpoint.
    if drive_cache.is_enabled():
        print("Syncing caches to Google Drive...")
        drive_cache.push(CONFIG["OUT_DIR"])
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
        # qa_score/rollup exclusion is unaffected -- the rep still isn't
        # penalized for checkpoints they never had a chance to hit. But
        # whatever WAS covered before the customer cut the call short is
        # worth keeping for the call-detail view (coaching signal), instead
        # of discarding it the way a genuine sub-30s call (which never had
        # this data to begin with) does.
        return _short_row(log, transcript, {
            "drop_reason": analysis["early_exit_reason"],
            "drop_note": analysis.get("drop_note", ""),
            "callback_time": "",
            "sentiment": analysis.get("sentiment", "Neutral"),
            "call_type": _valid_type(role, analysis.get("call_type", "")),
            "summary": analysis.get("summary", ""),
            "must_say": analysis.get("must_say", {}),
            "checkpoint_order": analysis.get("checkpoint_order", []),
            "metric_values": analysis.get("metric_values", {}),
            "_analysis_failed": False,
        }, role)
    call_type = "" if failed else _valid_type(role, analysis.get("call_type", ""))
    must_say = analysis.get("must_say", {})
    checkpoint_order = analysis.get("checkpoint_order", [])
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
        "qa_score": None if failed else weighted_score(role, call_type, must_say, checkpoint_order),
        "qa_score_model": analysis.get("qa_score", 0),
        "qa_notes": analysis.get("qa_notes", ""),
        "must_say": must_say,
        "checkpoint_order": checkpoint_order,
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
        # call_type/summary/must_say/etc. are "" / {} / [] for a genuine
        # sub-30s call (analyze_short_call never produces them) but carry the
        # partial checklist for a longer early-exit call (see _row()).
        "call_type": analysis.get("call_type", ""),
        "transcript": transcript,
        "summary": analysis.get("summary", ""),
        "sentiment": analysis.get("sentiment", "Neutral"),
        "sentiment_reason": "",
        "qa_score": None, "qa_score_model": None, "qa_notes": "",
        "must_say": analysis.get("must_say", {}),
        "checkpoint_order": analysis.get("checkpoint_order", []),
        "metric_values": analysis.get("metric_values", {}), "action_items": [],
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
        "must_say": {}, "checkpoint_order": [], "metric_values": {}, "action_items": [],
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


def _valid_sentiment(s):
    """Clamp to the Positive/Neutral/Negative enum. Claude's output_config.format
    already guarantees this, so it's a no-op there -- but Sarvam has no schema
    enforcement and has been observed drifting off-enum (e.g. "interested",
    "cautiously interested") despite the prompt spelling out the three options,
    which would otherwise pollute the dashboard's sentiment chart with stray
    categories."""
    s = (s or "").strip().lower()
    # Check negative first -- "not interested" / "uninterested" otherwise matches
    # the generic "interest" positive keyword below and would flip the polarity.
    if ("neg" in s or "frustrat" in s or "annoy" in s or "angry" in s or
            "upset" in s or "disappoint" in s or "not interest" in s or
            "no interest" in s or "uninterest" in s or "disinterest" in s):
        return "Negative"
    if "pos" in s or "interest" in s or "happy" in s or "excit" in s:
        return "Positive"
    return "Neutral"


def _order_adherence(ideal_ids, checkpoint_order):
    """0-100: how well `checkpoint_order` (the ids the rep actually covered,
    in the order they came up) follows `ideal_ids` (the required pitch flow).
    Scored as the longest run of checkpoints that stayed in the right relative
    order (longest increasing subsequence of each id's position in
    `ideal_ids`), as a share of the checkpoints the rep covered at all —
    e.g. covering 8 checkpoints with 6 of them in correct relative order
    scores 75. A checkpoint id not in the ideal list (model typo) is ignored;
    duplicates are collapsed to their first occurrence.

    Entries tagged customer_initiated=True (the customer asked about this
    topic and the rep answered) are excluded entirely -- answering a question
    the customer asked out of the normal pitch order is correct behavior, not
    a pitch-flow failure, so it shouldn't count against order adherence either
    way. If every covered checkpoint was customer-initiated, there's nothing
    to judge the rep's own ordering on, so this returns 100 (neutral) rather
    than 0. Each entry may be the new {"id", "customer_initiated"} shape or a
    bare id string (older cached data predating this field) -- a bare string
    is treated as customer_initiated=False, preserving its original scoring."""
    pos = {cid: i for i, cid in enumerate(ideal_ids)}
    seq, seen, any_covered = [], set(), False
    for entry in checkpoint_order or []:
        if isinstance(entry, dict):
            cid, customer_initiated = entry.get("id"), entry.get("customer_initiated", False)
        else:
            cid, customer_initiated = entry, False
        if cid not in pos or cid in seen:
            continue
        any_covered = True
        seen.add(cid)
        if customer_initiated:
            continue
        seq.append(pos[cid])
    if not seq:
        # Nothing rep-initiated to judge order on. If the rep covered zero
        # checkpoints at all, that's a real coverage failure -- score 0, same
        # as before. If everything covered was customer-initiated, there's no
        # evidence the rep's OWN ordering was ever off -- score 100 (neutral).
        return 100 if any_covered else 0
    # Longest increasing subsequence length (patience-sorting, O(n log n)).
    tails = []
    for x in seq:
        lo, hi = 0, len(tails)
        while lo < hi:
            mid = (lo + hi) // 2
            if tails[mid] < x:
                lo = mid + 1
            else:
                hi = mid
        if lo == len(tails):
            tails.append(x)
        else:
            tails[lo] = x
    return round(100 * len(tails) / len(seq))


def weighted_score(role, call_type, must_say, checkpoint_order=None):
    """QA score (0-100). Each checkpoint carries a `weight` (default 1);
    coverage_score is the share of total possible weight the rep actually
    earned — this ties the QA number directly to the checklist instead of
    being a separate model guess.

    For call types with `order_weighted: True` (currently sales.enquiry
    only — see CALL_TYPES), the final score blends 50% coverage_score with
    50% order_score (see `_order_adherence`): how closely the checkpoints
    that WERE covered followed the checklist's own order, which is the
    required pitch flow. Call types without the flag (nutrition, follow_up)
    are unaffected — order is not scored for them."""
    ctype = CALL_TYPES.get(role, {}).get(call_type, {})
    cps = ctype.get("checkpoints", [])
    if not cps:
        return 0
    total = sum(c.get("weight", 1) for c in cps)
    got = sum(c.get("weight", 1) for c in cps if (must_say or {}).get(c["id"]))
    coverage_score = 100 * got / total if total else 0
    if not ctype.get("order_weighted"):
        return round(coverage_score)
    order_score = _order_adherence([c["id"] for c in cps], checkpoint_order)
    return round(0.5 * coverage_score + 0.5 * order_score)


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
    ak = "set" if not CONFIG["ANTHROPIC_API_KEY"].startswith("PASTE") else "NOT set"
    print(f"  ANTHROPIC_API_KEY is {ak}.")
    print(f"  ANALYSIS_PROVIDER is {CONFIG['ANALYSIS_PROVIDER']!r} "
          f"(switch via ANALYSIS_PROVIDER env var or CONFIG in pipeline.py).")


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
