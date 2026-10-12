"""Generates sales_enquiry_scoring_update.pdf -- dev-team summary of the two
scoring changes made 2026-10-09: multiple_cuisines criteria + 50/50
coverage-vs-order weighting for sales.enquiry. Scenario numbers are computed
live from pipeline.py's weighted_score()/_order_adherence() so they always
match the real implementation. One-off doc generator, mirrors the reportlab
style already used by customer_insights_report.py."""
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet, ParagraphStyle
from reportlab.lib.units import inch
from reportlab.platypus import (SimpleDocTemplate, Table, TableStyle, Paragraph,
                                 Spacer, ListFlowable, ListItem, KeepTogether)

import pipeline as p

OUT = "sales_enquiry_scoring_update.pdf"

cps = p.CALL_TYPES["sales"]["enquiry"]["checkpoints"]
ideal_ids = [c["id"] for c in cps]
weight_of = {c["id"]: c["weight"] for c in cps}
labels = {
    "intro": "Intro",
    "healthy_food_doorstep": "Healthy food to your doorstep",
    "macros_calories": "Macro & calorie counted meals",
    "nutritionist_handholding": "Nutritionist handholds you through the journey",
    "menu_no_repeat_month": "Menu doesn't repeat for a month",
    "multiple_cuisines": "Multiple cuisines (only if customer asked)",
    "delivery_6_days": "6 days a week delivery",
    "pause_carry_forward_example": "Pause & carry-forward, with example",
    "discovery_duration": "Discovery: “what are you looking for?”",
    "pricing": "Pricing",
    "freshness": "Freshness",
    "delivery_address": "Delivery address taken",
    "next_steps_close": "Next steps to close",
}
TOTAL_WEIGHT = sum(weight_of.values())


def score(hit_ids, order_ids):
    must_say = {cid: True for cid in hit_ids}
    coverage = round(100 * sum(weight_of[c] for c in hit_ids) / TOTAL_WEIGHT)
    order = p._order_adherence(ideal_ids, order_ids)
    final = p.weighted_score("sales", "enquiry", must_say, order_ids)
    return coverage, order, final


# ---- scenarios, computed from the real scoring function ------------------
scenario_A_hit, scenario_A_order = ideal_ids, ideal_ids

scenario_B_miss = ["pricing", "delivery_address"]
scenario_B_hit = [c for c in ideal_ids if c not in scenario_B_miss]
scenario_B_order = scenario_B_hit

scenario_C_hit = ideal_ids
scenario_C_order = ["pricing", "freshness", "intro", "healthy_food_doorstep",
                     "macros_calories", "nutritionist_handholding", "menu_no_repeat_month",
                     "multiple_cuisines", "delivery_6_days", "pause_carry_forward_example",
                     "discovery_duration", "delivery_address", "next_steps_close"]

scenario_D_hit = ["pricing", "intro", "next_steps_close", "macros_calories", "delivery_address"]
scenario_D_order = ["pricing", "next_steps_close", "intro", "delivery_address", "macros_calories"]

scenarios = [
    ("A — Covers all 13, in the right order",
     "Textbook call: every checkpoint hit, in the order reps are trained to pitch.",
     score(scenario_A_hit, scenario_A_order)),
    ("B — Misses pricing & delivery address, rest in order",
     "Covers 11 of 13 checkpoints, but never quotes a price or takes the delivery address. "
     "Everything else said in the right sequence.",
     score(scenario_B_hit, scenario_B_order)),
    ("C — Covers all 13, but pitches price/freshness first",
     "Every checkpoint is covered eventually, but the rep leads with pricing and freshness "
     "before the intro and core pitch — full coverage, disorganized flow.",
     score(scenario_C_hit, scenario_C_order)),
    ("D — Weak call: only 5 checkpoints, scrambled",
     "Short, thin pitch — covers pricing, intro, next steps, macros and delivery address, "
     "and not in a sensible order either.",
     score(scenario_D_hit, scenario_D_order)),
]

# ---- PDF -------------------------------------------------------------------
styles = getSampleStyleSheet()
title_style = ParagraphStyle("TitleX", parent=styles["Title"], fontSize=19, spaceAfter=4)
meta_style = ParagraphStyle("Meta", parent=styles["Normal"], textColor=colors.grey, spaceAfter=18)
h2 = ParagraphStyle("H2", parent=styles["Heading2"], spaceBefore=16, spaceAfter=6,
                     textColor=colors.HexColor("#1a1a1a"))
body = ParagraphStyle("Body", parent=styles["Normal"], spaceAfter=8, leading=15)
small = ParagraphStyle("Small", parent=styles["Normal"], fontSize=9, textColor=colors.grey, leading=12)
scen_title = ParagraphStyle("ScenTitle", parent=styles["Normal"], fontName="Helvetica-Bold",
                             fontSize=10.5, spaceAfter=2)
scen_desc = ParagraphStyle("ScenDesc", parent=styles["Normal"], fontSize=9.5,
                            textColor=colors.HexColor("#444444"), spaceAfter=6, leading=13)

HEADER_BG = colors.HexColor("#2d2d2d")
ROW_ALT = colors.HexColor("#f6f6f6")
GRID = colors.HexColor("#dddddd")


def styled_table(rows, col_widths, align_center_from=1, font_size=9.5):
    t = Table(rows, colWidths=col_widths, repeatRows=1)
    style = [
        ("BACKGROUND", (0, 0), (-1, 0), HEADER_BG),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("FONTSIZE", (0, 0), (-1, -1), font_size),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, ROW_ALT]),
        ("GRID", (0, 0), (-1, -1), 0.5, GRID),
        ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
        ("ALIGN", (0, 0), (0, -1), "CENTER"),
    ]
    if align_center_from is not None:
        style.append(("ALIGN", (align_center_from, 0), (-1, -1), "CENTER"))
    t.setStyle(TableStyle(style))
    return t


story = []
story.append(Paragraph("Sales Enquiry Scoring Update", title_style))
story.append(Paragraph("2026-10-09 &middot; for the dev team", meta_style))

# ---- What changed ----------------------------------------------------------
story.append(Paragraph("What changed", h2))
story.append(ListFlowable([
    ListItem(Paragraph(
        "<b>Multiple cuisines checkpoint:</b> only counts as a hit if the rep mentions "
        "cuisine variety <i>in response to the customer asking</i> what kind of menu/food/"
        "cuisines Kenko serves. Mentioning it unprompted no longer counts.", body)),
    ListItem(Paragraph(
        "<b>Order now matters:</b> the enquiry call score is 50% checkpoint coverage "
        "(as before) and 50% whether the rep covered those checkpoints in the required "
        "order.", body)),
], bulletType="bullet", start="circle"))

# ---- How the 50/50 split works --------------------------------------------
story.append(Paragraph("How the 50% + 50% split works", h2))
story.append(Paragraph(
    "Every enquiry call now gets two sub-scores, averaged evenly into the final "
    "<font face='Courier'>qa_score</font>:", body))
story.append(ListFlowable([
    ListItem(Paragraph(
        "<b>Coverage score (50%)</b> &mdash; unchanged from before. Each checkpoint is worth "
        "a number of points (see table below, out of 56 total). Coverage score = points "
        "earned &divide; 56.", body)),
    ListItem(Paragraph(
        "<b>Order score (50%)</b> &mdash; new. Looks only at the checkpoints the rep actually "
        "covered, and checks how many of them came up in the right relative sequence "
        "compared to the required order. Skipped checkpoints are not penalized twice here "
        "&mdash; they already cost the rep points in the coverage score.", body)),
], bulletType="bullet", start="circle"))
story.append(Paragraph(
    "In short: say everything, in the right order &rarr; 100. Say everything, out of "
    "order &rarr; roughly half credit on the order half. Skip things but say the rest in "
    "order &rarr; full order credit, reduced coverage credit.", body))

# ---- Checkpoint points table ------------------------------------------------
story.append(Paragraph("Checkpoints, points, and required order", h2))
story.append(Paragraph(
    "This is the exact order reps should pitch in. Points shown are out of a total of "
    f"{TOTAL_WEIGHT}.", body))

rows = [["#", "Checkpoint", "Points"]]
for i, cid in enumerate(ideal_ids, start=1):
    rows.append([str(i), labels[cid], str(weight_of[cid])])
rows.append(["", "Total", str(TOTAL_WEIGHT)])

t = styled_table(rows, [0.4*inch, 4.4*inch, 0.7*inch])
# bold the total row
t.setStyle(TableStyle([
    ("FONTNAME", (0, len(rows)-1), (-1, len(rows)-1), "Helvetica-Bold"),
    ("LINEABOVE", (0, len(rows)-1), (-1, len(rows)-1), 1, colors.black),
]))
story.append(t)

# ---- Scenarios --------------------------------------------------------------
story.append(Paragraph("Example scenarios", h2))
story.append(Paragraph(
    "Numbers below come straight from the scoring code (not hand-estimated), so they "
    "show exactly how coverage and order combine.", body))

scen_blocks = []
for name, desc, (cov, order, final) in scenarios:
    tbl = styled_table(
        [["Coverage score", "Order score", "Final qa_score"],
         [f"{cov}", f"{order}", f"{final}"]],
        [1.7*inch, 1.7*inch, 1.7*inch], align_center_from=0, font_size=10)
    block = KeepTogether([
        Paragraph(name, scen_title),
        Paragraph(desc, scen_desc),
        tbl,
        Spacer(1, 10),
    ])
    scen_blocks.append(block)
story.extend(scen_blocks)

story.append(Spacer(1, 6))
story.append(Paragraph(
    "Implemented in <font face='Courier'>pipeline.py</font> &mdash; "
    "<font face='Courier'>CALL_TYPES[\"sales\"][\"enquiry\"]</font> and "
    "<font face='Courier'>weighted_score()</font> / "
    "<font face='Courier'>_order_adherence()</font>. Nutrition and sales follow-up calls "
    "are unaffected; they're still scored on coverage only.", small))

doc = SimpleDocTemplate(OUT, pagesize=letter,
                         topMargin=0.7*inch, bottomMargin=0.7*inch,
                         leftMargin=0.75*inch, rightMargin=0.75*inch)
doc.build(story)
print(f"Wrote {OUT}")
print("Scenario numbers:")
for name, _, nums in scenarios:
    print(" ", name, "->", nums)
