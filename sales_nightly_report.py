#!/usr/bin/env python3
"""Nightly sales-team report: filters pipeline.py's results.json down to a
fixed rep list, builds a scoped interactive dashboard (transcript + checklist
per call, no audio) and a PDF coverage summary for email.

Run:
  python3 sales_nightly_report.py --days 1
      -> refreshes results.json via pipeline.run(1) (bills Sarvam for any new
         recordings), then filters + builds outputs.
  python3 sales_nightly_report.py --no-refresh
      -> skips the pipeline run, just rebuilds from the existing results.json.
"""
import argparse
import datetime as dt
import json
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

OUT_DIR = Path(__file__).parent

# Callyzer's emp_name is often just a first name and doesn't match
# employees.csv's fuller display name -- match on lowercase prefix instead.
TARGET_REPS = {
    "sadaf": "sadaf",
    "nida": "Nida Masood",
    "sophiya": "Sophiya Dash",
}


def _canon(rep_raw):
    key = (rep_raw or "").strip().lower()
    for alias, canon in TARGET_REPS.items():
        if key == alias or key.startswith(alias):
            return canon
    return None


def load_filtered(results_path):
    payload = json.loads(Path(results_path).read_text())
    filtered = []
    for c in payload.get("calls", []):
        canon = _canon(c.get("rep"))
        if canon:
            c = dict(c)
            c["rep"] = canon
            filtered.append(c)
    payload = dict(payload)
    payload["calls"] = filtered
    return payload


def summarize(payload):
    stats = {}
    for c in payload["calls"]:
        rep = c["rep"]
        s = stats.setdefault(rep, {
            "total": 0, "recorded": 0, "scored": 0, "score_sum": 0.0,
            "short": 0, "error": 0,
        })
        s["total"] += 1
        status = c.get("status")
        if status != "missed":
            s["recorded"] += 1
        if status == "short":
            s["short"] += 1
        if status == "error":
            s["error"] += 1
        if status == "done" and c.get("qa_score") is not None:
            s["scored"] += 1
            s["score_sum"] += c["qa_score"]
    for s in stats.values():
        s["rec_pct"] = round(100 * s["recorded"] / max(s["total"], 1))
        s["avg_qa"] = round(s["score_sum"] / s["scored"], 1) if s["scored"] else None
    return stats


def build_dashboard(payload, out_path):
    tpl = OUT_DIR / "dashboard_template.html"
    html = tpl.read_text()
    html = html.replace("/*__DATA__*/{}",
                         "/*__DATA__*/" + json.dumps(payload, ensure_ascii=False))
    Path(out_path).write_text(html)
    print(f"Wrote {out_path}")


def build_pdf(stats, payload, out_path, dashboard_url=None):
    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                             topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    story = [
        Paragraph("Kenko Sales Team — Nightly Call Coverage", styles["Title"]),
        Paragraph(
            f"Generated: {payload.get('generated_at', dt.datetime.now().isoformat(timespec='seconds'))}"
            f"  ·  Period: last {payload.get('period_days', '?')} day(s)",
            styles["Normal"]),
        Spacer(1, 16),
    ]

    header = ["Rep", "Total", "Recorded", "Coverage %", "Fully Scored", "Avg QA Score", "Errors"]
    rows = [header]
    for rep in sorted(stats, key=lambda r: -stats[r]["total"]):
        s = stats[rep]
        rows.append([
            rep, s["total"], s["recorded"], f'{s["rec_pct"]}%',
            s["scored"], s["avg_qa"] if s["avg_qa"] is not None else "-",
            s["error"],
        ])
    table = Table(rows, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#1f2937")),
        ("TEXTCOLOR", (0, 0), (-1, 0), colors.white),
        ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
        ("GRID", (0, 0), (-1, -1), 0.5, colors.HexColor("#d1d5db")),
        ("ROWBACKGROUNDS", (0, 1), (-1, -1), [colors.white, colors.HexColor("#f9fafb")]),
        ("FONTSIZE", (0, 0), (-1, -1), 9),
        ("TOPPADDING", (0, 0), (-1, -1), 6),
        ("BOTTOMPADDING", (0, 0), (-1, -1), 6),
    ]))
    story.append(table)
    story.append(Spacer(1, 16))

    flags = []
    for rep, s in stats.items():
        if s["total"] and s["recorded"] == 0:
            flags.append(f"{rep}: 0% recording coverage across {s['total']} calls — "
                          f"check call recording is enabled on their phone.")
        elif s["rec_pct"] < 50:
            flags.append(f"{rep}: only {s['rec_pct']}% of calls recorded.")
        if s["error"]:
            flags.append(f"{rep}: {s['error']} call(s) failed analysis (parse error) — "
                          f"excluded from the QA average, may need re-processing.")
    if flags:
        story.append(Paragraph("Flags", styles["Heading2"]))
        for f in flags:
            story.append(Paragraph(f"• {f}", styles["Normal"]))
        story.append(Spacer(1, 12))

    if dashboard_url:
        story.append(Paragraph("Click-through dashboard (per-call transcript + checklist detail):",
                                styles["Heading2"]))
        story.append(Paragraph(dashboard_url, styles["Normal"]))

    doc.build(story)
    print(f"Wrote {out_path}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1,
                     help="days of Callyzer history to refresh via pipeline.run() before filtering")
    ap.add_argument("--no-refresh", action="store_true",
                     help="skip pipeline.run(), just rebuild from the existing results.json")
    ap.add_argument("--dashboard-url", default="",
                     help="stable hosted dashboard URL to print in the PDF")
    args = ap.parse_args()

    if not args.no_refresh:
        import pipeline
        pipeline.run(args.days)

    payload = load_filtered(OUT_DIR / "results.json")
    stats = summarize(payload)

    build_dashboard(payload, OUT_DIR / "sales_nightly_dashboard.html")
    build_pdf(stats, payload, OUT_DIR / "sales_nightly_summary.pdf",
              dashboard_url=args.dashboard_url or None)

    print(f"\nFiltered calls: {len(payload['calls'])}")
    for rep, s in stats.items():
        print(f"  {rep}: {s['recorded']}/{s['total']} recorded ({s['rec_pct']}%), avg QA {s['avg_qa']}")


if __name__ == "__main__":
    main()
