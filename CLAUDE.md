# Kenko Call Analysis

Pulls recorded calls from Callyzer, transcribes them with Sarvam, classifies each
call by type, scores it against a weighted checklist, and builds an HTML dashboard.

## Analysis engine: Claude-ready, running on Sarvam until credits land

Role classification (`_classify_role()`), full-call analysis (`analyze()`), and
short-call drop-reason classification (`analyze_short_call()`) all go through
one dispatcher, `_call_llm()`, which branches on `CONFIG["ANALYSIS_PROVIDER"]`
("sarvam" | "claude", default "sarvam" — override with the `ANALYSIS_PROVIDER`
env var or edit `CONFIG` directly). **This is temporary** — running on Sarvam
2026-10-17 only because Anthropic credits weren't available yet; flip the one
config line the moment they land, no other code changes needed.

- `"claude"` calls `_call_claude()` — Claude API (`CONFIG["CLAUDE_MODEL"]`,
  currently `claude-sonnet-5`) with `output_config.format`
  (JSON-schema-constrained output), so the response is always valid JSON
  matching the exact schema — no parsing needed.
- `"sarvam"` calls Sarvam's chat-completions model (`CONFIG["SARVAM_CHAT_MODEL"]`,
  `sarvam-105b`) via `_post_sarvam_chat()`, relying on the prompt's plain-English
  field-by-field instructions (there's no schema enforcement) and parsed with
  `_parse_json()` (tolerates a markdown fence or an escaped apostrophe, raises
  `ValueError` on real failure so it hits the same except-block as a Claude API
  error). `sarvam-105b` is a reasoning model that can return `content=None` with
  the real answer under `reasoning_content` — handled in `_call_llm()`.
  **Known reliability gap, observed directly**: because there's no schema
  enforcement, enum fields can drift off-spec (e.g. `sentiment` came back as
  `"interested"` / `"cautiously interested"` instead of
  `Positive`/`Neutral`/`Negative` in testing) — `_valid_sentiment()` clamps this
  (checked negative-first so "not interested" doesn't false-positive on the
  generic "interest" keyword), applied in both `analyze()` and
  `analyze_short_call()` regardless of provider (a no-op under Claude, which
  already can't produce an off-enum value). A `_post_sarvam_chat()` call can
  also occasionally return an empty response outright (observed once in
  testing) — this is the exact flakiness that motivated moving to Claude in the
  first place; it's an inherent Sarvam limitation, not something layered retry
  logic is expected to fully paper over.

Sarvam is used for speech-to-text regardless of `ANALYSIS_PROVIDER`
(`transcribe_batch()`, `saaras:v4`) — the provider switch only affects the
analysis step. The Anthropic client is a lazy singleton (`_claude_client()`)
with `max_retries=5`, so 429/5xx/connection errors retry via the SDK instead of
a hand-rolled backoff loop; the equivalent for Sarvam is `_post_sarvam_chat()`'s
own retry/backoff loop (restored alongside the Claude path, not removed).

`must_say`/`metric_values` are schema'd as open objects (`additionalProperties`
typed, not per-checkpoint properties) since the checkpoint set depends on which
`call_type` the model picks — the schema can't know that in advance.

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

For call types flagged `order_weighted: True` in `CALL_TYPES` (currently
`sales.enquiry` only, since 2026-10-09 per Pranav) the checklist's own list
order IS the required pitch flow, and `qa_score` is 50% checkpoint coverage +
50% order adherence: how closely the checkpoints the rep *did* cover followed
that order, scored via longest-increasing-subsequence over each covered
checkpoint's position in the ideal list (`_order_adherence()`) — covering 8
checkpoints with 6 of them in correct relative order scores 75 on the order
half. The model reports which checkpoints came up in which order as
`checkpoint_order`, alongside `must_say`; a checkpoint never covered can't be
"out of order" — it's already penalized via coverage. Other call types
(nutrition, `sales.follow_up`) have no `order_weighted` flag and score
coverage only, same as before.

`checkpoint_order` entries (2026-10-17 change) are `{"id", "customer_initiated"}`
objects, not bare id strings — `customer_initiated` is true when the customer
asked about that topic first and the rep responded, false when the rep brought
it up on their own initiative. `_order_adherence()` excludes
customer_initiated=true entries from the longest-increasing-subsequence check
entirely: answering a question the customer asked out of the normal pitch
order is correct rep behavior, not a pitch-flow failure, so it's neither
rewarded nor penalized on the order half. If every covered checkpoint was
customer-initiated, there's no evidence of the rep's own ordering being off,
so the order half scores 100 (neutral) rather than 0 — but if the rep covered
nothing at all, it's still 0, same as before. `_order_adherence()` also
accepts a bare id string per entry (customer_initiated defaults to False),
so it stays backward-compatible with any cached `results.json` data from
before this field existed.

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

`sales.enquiry`'s `multiple_cuisines` checkpoint was gated 2026-10-09 to only
count if the customer asked about cuisine variety first — mentioning it
unprompted didn't count. Reverted 2026-10-17 per Pranav: that gate rewarded
reps for staying quiet unless asked, when proactively mentioning cuisine
variety is good salesmanship and shouldn't score worse than reactively
answering the same question. Credit now applies either way. (This is
unrelated to `pause_carry_forward_example`'s "must give a concrete example"
requirement, which stays as-is — that's an outcome/depth bar, not a
who-brought-it-up gate, so it wasn't affected by the same reasoning.
`discovery_duration`'s "customer must state a duration to count" requirement
was reviewed under the same question and also left unchanged, for the same
reason: it's checking whether the discovery question actually extracted
useful information, not penalizing the rep for not being asked first.)

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

Unlike a genuine sub-30s call, an early-exit call DID go through the full
`analyze()` prompt, so it may have real `must_say`/`checkpoint_order`/
`metric_values`/`call_type`/`summary` for whatever the rep covered before the
cutoff (2026-10-17 change — previously these were discarded along with the
rest of the analysis). `_row()`/`reprocess()` now carry that data through to
`_short_row()` instead of hardcoding blanks. This is purely additive: `isScored`
in `dashboard_template.html` still gates every rollup on `qa_score != null`,
and early-exit calls keep `qa_score: null`, so nothing here affects an
average — it only surfaces partial progress in the call-detail view/checkpoint
dots as a coaching signal. Spot-check the model's early-exit judgment calls
with `spot_check_early_exits.py` (lists excluded calls that ran well past
`MIN_DURATION_SEC` before being excused — the longer such a call ran, the more
worth a human listen, since a customer who really isn't interested usually
shows it fast).

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
python3 pipeline.py --check --days N   # connection test, no Sarvam/Claude charges
python3 pipeline.py --days N        # full run (transcribes -> bills Sarvam + Claude), refreshes results.json
```

Full taxonomy: `CALL_TYPES` in `pipeline.py`. See `START_HERE.md` and `docs/` for
architecture, integration, and roadmap details.
