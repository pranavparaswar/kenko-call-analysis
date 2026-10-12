#!/usr/bin/env python3
"""One-off report: what customers ask, where they drop off, and what
convinces them -- built from reading all 120 fully-transcribed sales calls
(sadaf / Nida Masood / Sophiya Dash) plus all 141 short-call drop reasons,
cross-referenced from results_backup*.json + results.json against
transcripts_cache.json. Not part of the nightly pipeline -- run manually."""
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                 Spacer, ListFlowable, ListItem)

OUT = Path(__file__).parent / "customer_insights_report.pdf"

styles = getSampleStyleSheet()
h1 = ParagraphStyle("h1", parent=styles["Heading1"], spaceBefore=18, spaceAfter=8)
h2 = ParagraphStyle("h2", parent=styles["Heading2"], spaceBefore=10, spaceAfter=4,
                     textColor=colors.HexColor("#1f2937"))
body = ParagraphStyle("body", parent=styles["Normal"], spaceAfter=4, leading=14)
quote = ParagraphStyle("quote", parent=styles["Normal"], leftIndent=14,
                        textColor=colors.HexColor("#4b5563"), fontName="Helvetica-Oblique",
                        spaceAfter=6, leading=13)

story = []

story.append(Paragraph("Kenko Sales Calls — Customer Insights", styles["Title"]))
story.append(Paragraph(
    "Based on 120 fully-transcribed sales calls (sadaf, Nida Masood, Sophiya Dash) "
    "read in full, plus drop-reason data from 141 additional short calls. "
    "230 missed (no recording) and 47 analysis-error calls excluded (no transcript).",
    styles["Normal"]))
story.append(Spacer(1, 16))

# ---- Section 1: Top questions ----
story.append(Paragraph("1. Top Questions Customers Ask", h1))

questions = [
    ("Delivery timing precision", "Asked in 30+ calls",
     "Customers push for a narrower guarantee than the standard 2–3 hour delivery "
     "window (\"what exact time\", \"can I get a tighter slot\"). Reps cannot commit "
     "to anything narrower than the standard window."),
    ("Trial length shorter than 6 days", "At least 8 calls",
     "Customers ask for a 1, 3, or 5-day trial instead of the 6-day minimum. Reps "
     "hold a hard floor with no exceptions — costs leads outright, e.g. one prospect "
     "(\"Ishwar\") replied: “Okay, then I won't be requiring it.”"),
    ("Cold-food / reheating mechanics", "Near-universal",
     "“Will it arrive cold, do I need a microwave, is it safe?” Reps explain "
     "“chill-blast” preparation and microwave-safe packaging in almost every call."),
    ("Exact macro / protein breakdown", "Increasingly specific in later calls",
     "Customers want precise numbers, not ranges. One customer (“Amit”) pushed back: "
     "“if you say 600 calories, it has to actually be 600.” Another (“Rahul”) asked "
     "for exact 40g/50g protein figures."),
    ("Pause / carry-forward rules", "Recurring point of confusion",
     "“Unlimited” pauses are answered consistently, but the math on how a "
     "carry-forward extends the plan by extra months confused some customers badly — "
     "one pair of customers (“Jaydev”/“Sarthak”) needed roughly 15 exchanges to "
     "understand it."),
    ("Veg / non-veg price parity and daily switching", "Consistently asked, consistently yes",
     "“Is it the same price, can I switch day to day?” — answered yes every time."),
    ("Discounts / first-time trial pricing", "Several calls (Akshata, Om Saran, Vinesh)",
     "Reps have zero discount lever — prices are stated as fixed. A clean, repeated "
     "objection with no flexibility offered in response."),
]
for i, (title, freq, detail) in enumerate(questions, 1):
    story.append(Paragraph(f"{i}. {title} <font color='#6b7280'>— {freq}</font>", h2))
    story.append(Paragraph(detail, body))

# ---- Section 2: Drop-off points ----
story.append(Paragraph("2. Drop-off Points", h1))
story.append(Paragraph("Short-call drop reasons (141 calls that never reached a full conversation):", body))

rows = [["Reason", "Count"],
        ["no_answer (voicemail)", "38"],
        ["call_later", "30"],
        ["not_interested", "27"],
        ["busy_hangup", "24"],
        ["undeliverable_location", "8"],
        ["other", "7"],
        ["wrong_number", "5"],
        ["callback_time / callback_requested", "2"]]
t = Table(rows, hAlign="LEFT", colWidths=[3.2 * inch, 1 * inch])
t.setStyle(TableStyle([
    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
    ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
    ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
    ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
    ("FONTSIZE", (0, 0), (-1, -1), 9),
    ("TOPPADDING", (0, 0), (-1, -1), 5),
    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
]))
story.append(t)
story.append(Spacer(1, 10))

story.append(Paragraph("From the fully-transcribed calls (deeper, qualitative):", body))
dropoffs = [
    "<b>Delivery-area rejection is the #1 instant-kill</b> — the moment an address falls "
    "outside the service zone, the call ends with no workaround. Seen across multiple "
    "Bangalore neighborhoods and out-of-city numbers (Ghaziabad, Odisha, Mumbai).",
    "<b>Cold-food / no-microwave is a structural dealbreaker</b> — confirmed in at least "
    "4 calls, including one customer who had a personal referral from Kenko's co-founder "
    "and still walked away solely because his workplace had no microwave.",
    "<b>The rigid 6-day trial floor</b> loses leads outright who want to try something shorter.",
    "<b>The hard “no discount” stance</b> loses price-sensitive leads with no fallback offer.",
    "<b>Fully-engaged customers still stall post-pitch</b> — “let me think”, “let me check "
    "with my mom/brother/boyfriend who's paying”, “let me compare options” — happens even "
    "after a flawless, fully-answered 15+ minute call (seen with “Kritika”, “Chirag”, "
    "“Sai”, “Anush”).",
    "<b>Pre-payment nutritionist consultation refused</b> — at least 2 calls (including "
    "“Amit”, a ~₹10k order) directly asked to speak to a nutritionist before paying "
    "and were told this isn't possible. Directly cost at least one high-value order.",
    "<b>Weekend-only or non-standard schedule requests</b> are unresolvable dead ends — "
    "Kenko delivers Monday–Saturday only.",
]
story.append(ListFlowable([ListItem(Paragraph(d, body)) for d in dropoffs],
                           bulletType="bullet", start="•"))

# ---- Section 3: What convinces customers ----
story.append(Paragraph("3. What Convinces Customers / Closing Moments", h1))
convinces = [
    "<b>Precise numbers beat vague reassurance.</b> The highest-scoring calls (QA 89–100) "
    "all featured reps giving exact figures on request — protein grams, delivery windows, "
    "reheat times — instead of generic “trust us” language.",
    "<b>Proactively offering a refund / no-risk safety net before being asked</b> correlated "
    "with the single best outcome in the sample (“Kevin”, QA 100 — customer proceeded "
    "<i>and</i> volunteered to refer friends).",
    "<b>An ongoing nutritionist relationship</b>, not a one-off call — a persistent rep + "
    "nutritionist + customer WhatsApp group — was explicitly cited as reassuring in the "
    "highest-scoring call (“Hardik”, QA 100).",
    "<b>Trust-analogy objections handled directly work well.</b> One customer compared Kenko "
    "to Zomato/Swiggy on hygiene trust; the rep's concrete answer (fresh daily-procured "
    "ingredients, in-house chef team, eco packaging) kept the call moving toward close.",
    "<b>A fast, low-friction trial-to-payment path.</b> The cleanest full close in the sample "
    "(“Rahul”, QA 89) featured a rep who patiently resolved a genuinely complex "
    "multi-location, three-meal request, then went straight to a payment link with clear "
    "next-day-start logistics — no unresolved objections left hanging.",
    "<b>Warm referrals</b> (an existing customer mentioning Kenko to a friend/colleague) show "
    "up repeatedly as the entry point for the most-engaged calls — though even these aren't "
    "immune to the cold-food/microwave dealbreaker.",
]
story.append(ListFlowable([ListItem(Paragraph(c, body)) for c in convinces],
                           bulletType="bullet", start="•"))

doc = SimpleDocTemplate(str(OUT), pagesize=letter,
                         topMargin=0.7 * inch, bottomMargin=0.7 * inch,
                         leftMargin=0.75 * inch, rightMargin=0.75 * inch)
doc.build(story)
print(f"Wrote {OUT}")
