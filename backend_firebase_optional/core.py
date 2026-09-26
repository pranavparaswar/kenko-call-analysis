"""
Kenko Call Analysis — shared core logic (backend).
Ported from the proven batch pipeline (../pipeline.py). Pure functions, no
web/Firebase deps here so they're easy to unit-test. Used by main.py (the
Cloud Functions).

CALL_TYPES is a self-contained COPY of pipeline.py's CALL_TYPES (Cloud
Functions deploy this `functions/` folder in isolation, so it can't import a
sibling file one level up the way the on-demand REST API's server.py does).
KEEP IN SYNC with pipeline.py's CALL_TYPES if checkpoints/weights change there.
"""
import os
import re
import json
import requests

SARVAM_API_KEY = os.environ.get("SARVAM_API_KEY", "")
SARVAM_STT_MODEL = "saaras:v3"        # batch model, handles long calls + diarization
SARVAM_CHAT_MODEL = "sarvam-105b"     # analysis model
SARVAM_LANGUAGE = "en-IN"

# ----------------------------------------------------------------------------
# CALL TYPE TAXONOMY — role -> call_type -> {desc, checkpoints}.
# Two-level classification: role comes from the employee tag (role_for());
# call_type is AI-classified from the transcript (see analyze()). Each
# checkpoint has a weight, 1-3 (3 = core, 2 = important, 1 = hygiene).
# qa_score = weighted % of checkpoints covered (see weighted_score()).
# ----------------------------------------------------------------------------
CALL_TYPES = {
    "nutrition": {
        "onboarding": {
            "desc": "First interaction with a brand-new client — kickoff / welcome.",
            "checkpoints": [
                {"id": "greet",        "label": "Greet + identify as Kenko", "weight": 1,
                 "criteria": "Greets the client by name and identifies as The Kenko Life."},
                {"id": "goals",        "label": "Understand goals & history", "weight": 3,
                 "criteria": "Asks about the client's health goals, medical history, and lifestyle."},
                {"id": "baseline",     "label": "Record baseline weight/measures", "weight": 3,
                 "criteria": "Notes current weight and/or body measurements as a starting baseline."},
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
            "checkpoints": [
                {"id": "greet",         "label": "Greet by name", "weight": 1,
                 "criteria": "Greets the client by name and identifies as Kenko."},
                {"id": "weight",        "label": "Record weight change", "weight": 3,
                 "criteria": "Asks for and notes current weight or how it changed since last check-in."},
                {"id": "measurements",  "label": "Record measurements change", "weight": 2,
                 "criteria": "Asks about or notes changes in body measurements."},
                {"id": "portions",      "label": "Check portion adequacy", "weight": 3,
                 "criteria": "Checks whether meal portions are too low, adequate, or too high."},
                {"id": "satiety",       "label": "Check satiety after meals", "weight": 2,
                 "criteria": "Asks whether the client feels satisfied / full after meals."},
                {"id": "feedback",      "label": "Gather client feedback", "weight": 2,
                 "criteria": "Invites the client's feedback on the plan and any challenges."},
                {"id": "plan_adjust",   "label": "Give plan changes / next steps", "weight": 3,
                 "criteria": "Gives clear next steps or adjusts the plan for the coming period."},
                {"id": "next_checkin",  "label": "Set next check-in", "weight": 1,
                 "criteria": "Sets a clear date for the next check-in and closes positively."},
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
            "desc": "First contact with a prospect — discovery / new enquiry.",
            "checkpoints": [
                {"id": "intro",       "label": "Intro w/ name + Kenko",
                 "criteria": "Introduces themselves by name and mentions The Kenko Life."},
                {"id": "health_goal", "label": "Ask health goal",
                 "criteria": "Asks about the customer's health/nutrition goal."},
                {"id": "explain_plan","label": "Explain program/plan",
                 "criteria": "Clearly explains the program or product on offer, including: "
                             "multiple cuisines and a rotating menu; meal wallet has no "
                             "expiry date with unlimited carry-forwards; supports multiple "
                             "delivery addresses; 3-hour delivery windows; can pause/cancel "
                             "from the app a day prior, by the 6 PM cutoff time; food is "
                             "microwave-reheatable and may arrive slightly cold; and asks/"
                             "notes veg or non-veg preference."},
                {"id": "price",       "label": "State price/offer clearly",
                 "criteria": "States the price and any offer clearly."},
                {"id": "next_step",   "label": "Set next step",
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
                 "criteria": "Addresses the customer's concerns or objections."},
                {"id": "value",     "label": "Restate value / offer",
                 "criteria": "Restates the value and the current offer."},
                {"id": "ask",       "label": "Ask for decision / next step",
                 "criteria": "Asks for a decision or sets a concrete next step."},
            ],
        },
        "closing": {
            "desc": "Closing the sale — customer is ready to buy / finalize.",
            "checkpoints": [
                {"id": "confirm_plan","label": "Confirm plan chosen",
                 "criteria": "Confirms which plan/program the customer is taking."},
                {"id": "confirm_price","label": "Confirm price",
                 "criteria": "Confirms the final price clearly."},
                {"id": "payment",    "label": "Explain payment / next steps",
                 "criteria": "Explains payment and onboarding next steps."},
                {"id": "thank_close","label": "Thank & close",
                 "criteria": "Thanks the customer and closes politely."},
            ],
        },
        "escalation": {
            "desc": "Prospect/customer called with a complaint or issue.",
            "checkpoints": [
                {"id": "ack",      "label": "Acknowledge the issue",
                 "criteria": "Acknowledges the customer's issue."},
                {"id": "understand","label": "Understand the problem",
                 "criteria": "Understands the problem fully."},
                {"id": "resolve",  "label": "Resolve / escalate",
                 "criteria": "Provides a resolution or escalates appropriately."},
                {"id": "followup", "label": "Set follow-up",
                 "criteria": "Sets a follow-up on the issue."},
            ],
        },
    },
}

ANALYZED_ROLES = set(CALL_TYPES.keys())   # nutrition, sales. Ops/other are skipped.


# ----------------------------------------------------------------------------
# Roles
# ----------------------------------------------------------------------------
def normalize_role(s):
    s = (s or "").strip().lower()
    if not s:
        return ""
    if "nutri" in s or "diet" in s or "coach" in s:
        return "nutrition"
    if "sale" in s:
        return "sales"
    return s  # e.g. 'ops' -> not analyzed


def norm_num(s):
    return re.sub(r"\D", "", str(s or ""))[-10:]


def role_for(emp_number, emp_name, roles_map):
    """roles_map: {norm_number: role, name_lower: role}. Returns role or None.
    Build it once from Firestore `employees` collection (see seed_roles.py)."""
    if emp_number and norm_num(emp_number) in roles_map:
        return roles_map[norm_num(emp_number)]
    if emp_name and emp_name.strip().lower() in roles_map:
        return roles_map[emp_name.strip().lower()]
    return None


def valid_type(role, t):
    """Keep the model's call_type only if it's a known type for this role.
    Returns "" (not a guessed default) when t is empty/unrecognized AND the
    caller has already flagged the analysis as failed — see analyze()."""
    t = (t or "").strip().lower().replace(" ", "_")
    return t if t in CALL_TYPES.get(role, {}) else (
        next(iter(CALL_TYPES.get(role, {})), ""))


def weighted_score(role, call_type, must_say):
    """QA score (0-100) = weighted % of this call type's checkpoints covered.
    Ties the score directly to the checklist instead of the model's own guess."""
    cps = CALL_TYPES.get(role, {}).get(call_type, {}).get("checkpoints", [])
    if not cps:
        return 0
    total = sum(c.get("weight", 1) for c in cps)
    got = sum(c.get("weight", 1) for c in cps if (must_say or {}).get(c["id"]))
    return round(100 * got / total) if total else 0


# ----------------------------------------------------------------------------
# Transcript formatting (from Sarvam batch output JSON)
# ----------------------------------------------------------------------------
def format_transcript(sarvam_output):
    """Turn a Sarvam batch output dict into diarized 'Speaker N: ...' text."""
    dia = sarvam_output.get("diarized_transcript")
    if dia and dia.get("entries"):
        return "\n".join(
            f"Speaker {e.get('speaker_id','?')}: {e.get('transcript','').strip()}"
            for e in dia["entries"] if e.get("transcript")
        )
    return (sarvam_output.get("transcript") or "").strip()


# ----------------------------------------------------------------------------
# Analysis (sarvam-105b chat completions) — classifies call_type FIRST, then
# scores only that type's checkpoints (same two-step prompt as pipeline.py).
# ----------------------------------------------------------------------------
def analyze(transcript, role, rep_name=""):
    types = CALL_TYPES.get(role, CALL_TYPES["sales"])
    role_label = "nutritionist" if role == "nutrition" else "sales"

    types_txt = ""
    for tkey, t in types.items():
        cps = "\n".join(f'      - "{c["id"]}": {c["criteria"]}' for c in t["checkpoints"])
        types_txt += f'  * "{tkey}": {t["desc"]}\n    checkpoints:\n{cps}\n'
    type_keys = ", ".join(f'"{k}"' for k in types.keys())

    system = (f"You are a QA analyst for The Kenko Life, a nutrition/health company. "
              f"Analyze this {role_label} call transcript. Return STRICT JSON only.")
    user = f"""This is a {role_label} call. First decide which TYPE of call it is,
then evaluate ONLY that type's checkpoints.

Call types (pick exactly one):
{types_txt}

Return JSON with EXACTLY these keys:
- "call_type": one of {type_keys}.
- "summary": 2-3 sentence summary of the call.
- "sentiment": one of "Positive", "Neutral", "Negative" (customer's overall sentiment).
- "sentiment_reason": one short sentence.
- "qa_score": integer 0-100 rating the rep's overall call quality FOR THAT CALL TYPE.
- "qa_notes": one or two sentences of coaching feedback.
- "must_say": an object mapping each checkpoint id of the CHOSEN call_type to true/false.
- "action_items": array of short strings (follow-ups / next steps).

Transcript:
\"\"\"{transcript[:12000]}\"\"\"

Return ONLY the JSON object."""

    r = requests.post(
        "https://api.sarvam.ai/v1/chat/completions",
        headers={"Authorization": f"Bearer {SARVAM_API_KEY}",
                 "Content-Type": "application/json"},
        json={"model": SARVAM_CHAT_MODEL,
              "messages": [{"role": "system", "content": system},
                           {"role": "user", "content": user}],
              "temperature": 0.2},
        timeout=120,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Sarvam chat {r.status_code}: {r.text[:300]}")
    msg = r.json()["choices"][0].get("message", {})
    # sarvam-105b can return content=None with the text under reasoning_content
    content = msg.get("content") or msg.get("reasoning_content") or ""
    return parse_json(content)


def parse_json(text):
    """Parse the model's JSON reply. The model sometimes wraps it in a markdown
    code fence, or escapes an apostrophe as \\' (invalid in strict JSON) — both
    are tolerated here before giving up, so a formatting quirk doesn't silently
    zero out a rep's score. On real failure, _analysis_failed=True is set so
    callers (main.py) can skip scoring instead of recording a false 0."""
    default = {"call_type": "", "summary": "", "sentiment": "Neutral",
               "sentiment_reason": "", "qa_score": 0, "qa_notes": "",
               "must_say": {}, "action_items": [], "_analysis_failed": True}
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
