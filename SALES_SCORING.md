# Sales Call Scoring — How It Works

This explains how your calls are scored by the QA system. Every recorded sales
call is transcribed and checked against a checklist for that call's type. Your
score is simply **the share of the checklist you covered, weighted by how
important each item is** — not a subjective rating.

## The two call types

**Enquiry** — first contact with a new prospect.
**Follow-up** — following up with a prospect from an earlier conversation.

The system figures out which type a call is automatically from the transcript.

## Enquiry checklist (13 items)

Each item has a weight from 2 (lowest) to 5 (highest) reflecting how central
it is to the pitch.

| # | Checkpoint | What it means | Weight |
|---|---|---|---|
| 1 | Intro | Introduce yourself by name and mention The Kenko Life | 3 |
| 2 | Healthy food to your doorstep | State the core pitch — healthy food delivered to the customer's doorstep | 5 |
| 3 | Macro & calorie counted meals | Mention that meals are macro- and calorie-counted | 5 |
| 4 | Nutritionist handholding | Explain a nutritionist/coach handholds the customer through their journey, not just at signup | 5 |
| 5 | Menu doesn't repeat for a month | State the menu doesn't repeat for a month | 5 |
| 6 | Multiple cuisines | Mention cuisine variety — **only counts if the customer asked** what kind of menu/food/cuisines Kenko serves; mentioning it unprompted doesn't count | 5 |
| 7 | 6 days a week delivery | Explain delivery happens 6 days a week | 5 |
| 8 | Pause & carry-forward, with example | Explain the pause/carry-forward flexibility **with a concrete example** (e.g. "if travelling, pause the next 3 days by 6 PM the day before") — explaining it without an example doesn't count | 5 |
| 9 | Discovery question (duration) | Ask what the customer is looking for — **only counts if the customer responds with a duration**; asking without getting one doesn't count | 4 |
| 10 | Pricing | State the price and any current offer clearly | 5 |
| 11 | Freshness | Explain food arrives refrigerated/cold and needs refrigeration + reheating before eating | 5 |
| 12 | Delivery address taken | Ask for and note the customer's delivery address | 2 |
| 13 | Next steps to close | Set clear next steps to close the sale | 2 |

**Total possible weight: 56.**

### Your score has two halves
`qa_score` = **50% checkpoint coverage + 50% pitch-order adherence.**

- **Coverage** is the weighted share of the 13 checkpoints above you covered.
- **Order adherence** checks how closely the checkpoints you *did* cover
  followed this list's own order — that order is the required pitch flow.
  It's scored by finding the longest run of checkpoints you covered that
  stayed in the right relative sequence (so hitting 8 of 13, with 6 of those
  in correct relative order, scores 75 on the order half). A checkpoint you
  never covered isn't separately penalized for "being out of order" — missing
  it already costs you on the coverage half.

## Follow-up checklist (6 items)

Intro/reconnect, recap of the prior conversation, addressing objections,
restating value/offer, sending the app link if the customer is ready, and
asking for a decision/next step. These are currently unweighted (each counts
equally toward the follow-up score) and there's no order-adherence component.

Follow-up calls are scored individually, but they're **not included** in the
overall team/rep averages on the dashboard — only shown in the calls table.

## The app link checkpoint

Both enquiry and follow-up calls share this rule: payment is via the Kenko
app link (not a payment gateway). The moment a customer says they're
convinced and asks how to proceed — on whichever call that happens, not a
separate "closing" call — you need to send the link. It's **not** held
against you if that moment never came up on the call, or for trial /
non-standard plans.

## Calls that don't count against you

Not every call gets scored. If a call ends early for a reason outside your
control, it's excluded from scoring entirely (no score is recorded, and it
doesn't drag down your average):

- Call under 30 seconds (wrong number, no answer, busy hangup, "call me later")
- Customer flatly not interested almost immediately, before you had a real
  chance to run the pitch
- **Delivery location turns out to be undeliverable**

These show up on the dashboard with a reason tag instead of a score — they're
tracked for visibility, not held against you. The one thing that **does**
count against you is not covering checklist items on a call where you had a
genuine opportunity to.

## Sentiment

Each call also gets a sentiment tag (Positive / Neutral / Negative) reflecting
the customer's tone on the call. This is shown for context but is separate
from your QA score.

## What gets logged, not just what got covered

For checkpoints where a real number or detail was discussed — the price
quoted, a specific date, etc. — the system logs the actual value alongside
the Yes/Missed mark, not just whether you covered it. This shows up under
each checkpoint in the call detail view on the dashboard.
