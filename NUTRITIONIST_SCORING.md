# Nutritionist Call Scoring

How nutritionist calls are scored, combining the original `pipeline.py`
checklists with additions from "Backend APP Nutritionist - New OB" (the
onboarding SOP). Same model as sales (see `SALES_SCORING.md`): **score =
weighted % of checklist items covered**, weight 3 = core / 2 = important /
1 = hygiene.

Implemented in `CALL_TYPES["nutrition"]` in `pipeline.py`.

## Onboarding call (Day 0)

Original 8 checkpoints, plus 6 new ones from the SOP doc (medical history,
body composition, dietary recall, activity, digestive health, lifestyle).

| Checkpoint | Criteria | Weight | Source |
|---|---|---|---|
| Greet + identify as Kenko | Greets by name, identifies as The Kenko Life | 1 | original |
| Understand goals & history | Asks about health goals, medical history, lifestyle | 3 | original |
| Health & medical history | Specifically asks about conditions, allergies, medications | 3 | **new** |
| Record baseline weight/measures | Notes current weight/measurements as baseline | 3 | original |
| Body composition (BCA) | Discusses BCA metrics (fat %, muscle mass) if available | 2 | **new** |
| Dietary recall & meal plan discussion | Asks what the client currently eats + discusses the subscription plan | 3 | **new** |
| Training & activity level | Asks about workout/physical activity | 2 | **new** |
| Digestive health | Asks about gut health | 2 | **new** |
| Recovery & lifestyle | Asks about sleep/stress/lifestyle factors | 1 | **new** |
| Explain the program | Explains how the Kenko program/meal plan works | 2 | original |
| Set initial plan & portions | Gives the initial diet plan with portion sizes | 3 | original |
| Confirm plan start date | States a clear start date for the plan | 3 | original |
| Explain food storage & handling | Refrigerate/reheat instructions, how long food keeps | 3 | original |
| Set first follow-up | Sets a clear date for the first check-in | 2 | original |

**Total weight: 33** (was 20).

## Consultation call (routine follow-up)

Redesigned 2026-09-06 to a simpler 4-checkpoint structure matching the SOP's
day-15/30 follow-up flow directly: **meal feedback, progress on metrics,
future plan/guidance**, plus a **plan modification** checkpoint that's
conditional (only applies if the call actually warranted a change). The
three core checkpoints are what makes a consultation "good." Order of
coverage is explicitly NOT scored — only whether each topic was genuinely
covered somewhere on the call. This replaces the earlier 9-checkpoint
breakdown (greet / weight / measurements / BCA progress / portions / satiety
/ feedback / plan_adjust / next_checkin).

| Checkpoint | Criteria | Weight |
|---|---|---|
| Meal feedback | Asks for feedback on meals — taste, variety, portion size, satisfaction | 3 |
| Progress on metrics | Discusses progress on tracked metrics — weight, measurements, BCA, or other numbers from prior check-ins | 3 |
| Future plan & guidance | Discusses the plan going forward and gives clear guidance/next steps | 3 |
| Diet plan modification (if needed) | Proposes/makes a plan change if feedback or metrics warrant one; N/A if nothing warranted a change | 1 |

**Total weight: 10** (was 19).

## Out of scope (per your call)

- **Renewal call (Day 5)** — not implemented, ignored for now.
- **Day 1 / Day 3 text touchpoints** — these are WhatsApp texts, not calls;
  the pipeline only scores Callyzer voice recordings, so they're not
  scoreable here regardless.

## What gets logged, not just what got covered

For data-bearing checkpoints — baseline weight, BCA metrics, portion/satiety
notes, the plan given, dates confirmed, etc. — the system now logs the actual
value or detail mentioned on the call, not just a Yes/Missed mark. E.g. under
"Record weight change" you'd see the actual figure discussed, if one was
given. This shows up under each checkpoint in the call detail view on the
dashboard, and is stored per-call in `results.json` as `metric_values`.
