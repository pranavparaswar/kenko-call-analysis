# Kenko Call Analysis

Pulls recorded calls from Callyzer, transcribes them with Sarvam, classifies each
call by type, scores it against a weighted checklist, and builds an HTML dashboard.

## Scoring model

Each call type (nutrition: onboarding/consultation/escalation; sales:
enquiry/follow_up/closing/escalation) has its own list of checkpoints, defined in
`CALL_TYPES` in `pipeline.py`. Each checkpoint carries a `weight`:
- **3** = core (central to the call's purpose)
- **2** = important
- **1** = hygiene (nice-to-have / procedural)

`qa_score` (in `weighted_score()`) = weighted % of checkpoints covered — sum of
weights for checkpoints the rep hit, divided by the total possible weight for
that call type. This ties the visible QA number directly to the checklist. The
model's own independent rating is kept alongside as `qa_score_model`.

Note: sales only has two call types today — `enquiry` and `follow_up` (`closing`
and `escalation` were removed 2026-08-15; the business doesn't have those as
distinct call types). `sales.enquiry` was redesigned 2026-09-28 per Pranav's own
weightage table — 13 checkpoints, weighted 2-5 (not nutrition's 1-3 scale), total
possible weight 56: intro (3); core pitch/doorstep delivery, macro & calorie
counting, nutritionist handholding, menu-doesn't-repeat-for-a-month, multiple
cuisines, 6-day delivery, pause & carry-forward *with a concrete example*,
pricing, and freshness/handling (5 each); a discovery question asking what the
customer's looking for, credited only if the customer states a duration (4);
delivery address and next-steps-to-close (2 each). The old `health_goal`,
`veg_nonveg_pref`, and `app_link_payment` checkpoints were dropped, not folded
in elsewhere — confirmed intentional. `follow_up` is unchanged and still has no
explicit `weight` set, so it defaults to 1 (flat/unweighted) until redesigned.

Both `enquiry` and `follow_up` share an `app_link_payment` checkpoint: payment
is via the Kenko app link (not a payment gateway), and reps must send it the
moment a customer says they're convinced and asks how to proceed — on whichever
call that happens, not a separate "closing" call. It's not applicable (doesn't
count against the rep) if that moment never came up on the call, or for trial /
non-standard plans.

## Calls that end through no fault of the rep

Calls under `MIN_DURATION_SEC` (30s) never get the full checklist — they go
through `analyze_short_call()` instead, which just classifies why the call was
short (`drop_reason`: `not_interested` / `wrong_number` / `no_answer` /
`busy_hangup` / `undeliverable_location` / `call_later` / `other`) and gets
`qa_score: null`, excluded from every rollup.

But a call can run well past 30s and still be a dead end through no fault of
the rep — e.g. the customer engages briefly then flatly isn't interested, or
the delivery location turns out undeliverable, before the rep had a real
chance to work through the pitch. For these, the full `analyze()` prompt also
asks the model for `early_exit_reason` (one of the same `DROP_REASONS`, or `""`
if the rep had a genuine shot at the checklist even if the customer declined
at the end). When set, `_row()` (and the mirrored branch in `reprocess()`)
routes the call through `_short_row()` just like a sub-30s call: `qa_score`
stays `null`, `drop_reason`/`drop_note` are set, `status: "short"` — same
dashboard treatment (excluded from averages, shown with its drop reason
instead of a score).

`follow_up` calls are tracked and individually scored, but excluded from the
dashboard's "overall" rollups (top KPIs, per-rep compliance chart, sentiment
chart) — see `forOverall()` in `dashboard_template.html`. They still show in the
calls table with their own qa_score.

## Nutrition checkpoint notes

- **consultation** checkpoints come from Kenko's app tabs (Meals / Progress /
  Feedback / Coach Notes).
- **onboarding** additionally must check for plan **start date** and **food
  storage/handling** (refrigerate / microwave) instructions.

## Running

```bash
python3 pipeline.py --demo          # dashboard from sample data, no keys needed
python3 pipeline.py --check --days N   # connection test, no Sarvam charges
python3 pipeline.py --days N        # full run (transcribes -> bills Sarvam), refreshes results.json
```

Full taxonomy: `CALL_TYPES` in `pipeline.py`. See `START_HERE.md` and `docs/` for
architecture, integration, and roadmap details.
