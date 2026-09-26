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

Each item has a weight: **3 = core**, **2 = important**, **1 = hygiene**.
Your score = (weight of items you covered) ÷ (total weight of all items) × 100.

| # | Checkpoint | What it means | Weight |
|---|---|---|---|
| 1 | Intro | Introduce yourself by name and mention The Kenko Life | 1 |
| 2 | Health goals & conditions | Ask about the customer's health/nutrition goals and any relevant conditions | 3 |
| 3 | Delivery location | Ask for and note the delivery address; mention multiple-address support if relevant | 2 |
| 4 | Macros & calories | Mention that meals are macro- and calorie-counted | 2 |
| 5 | Nutrition support | Mention ongoing nutrition/coach support throughout the journey, not just at signup | 2 |
| 6 | Cuisine variety | Mention variety of meals/cuisines; menu doesn't repeat for 4 weeks | 2 |
| 7 | Delivery schedule | Explain delivery happens 6 days/week within a delivery window | 2 |
| 8 | Meal wallet flexibility | Explain 1 month = 26 meals, usable flexibly, with pause/cancel options | 3 |
| 9 | Food temperature & handling | Explain food is delivered cold/refrigerated and must be refrigerated + reheated | 3 |
| 10 | Veg/non-veg preference | Ask and note the customer's preference | 1 |
| 11 | Price/offer | State the price and any current offer clearly | 3 |
| 12 | App link for payment | Send the Kenko app link the moment the customer says they're ready to proceed | 3 |
| 13 | Next step | Set a clear next step or follow-up | 1 |

**Total possible weight: 28.**

### About the app link checkpoint
This only counts against you if the customer actually indicated they were
ready to proceed on that call and you didn't send the link. If that moment
never came up, or it's a trial/non-standard plan, it's not held against you.

## Follow-up checklist (6 items)

Intro/reconnect, recap of the prior conversation, addressing objections,
restating value/offer, sending the app link if the customer is ready, and
asking for a decision/next step. These are currently unweighted (each counts
equally toward the follow-up score).

Follow-up calls are scored individually, but they're **not included** in the
overall team/rep averages on the dashboard — only shown in the calls table.

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
