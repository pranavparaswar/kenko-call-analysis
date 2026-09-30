#!/usr/bin/env python3
"""Nightly sales-team report: filters pipeline.py's results.json down to
whichever reps are tagged role=sales in employees.csv (so a new hire is
picked up automatically, no code change needed), and sends EACH rep their
OWN separate email -- only their own calls, never a teammate's -- with
management CC'd on every one. Each email carries a PDF coverage summary and
a scoped interactive dashboard (transcript + checklist per call, no audio).

Run:
  python3 sales_nightly_report.py --days 1 --send
      -> refreshes results.json via pipeline.run(1) (bills Sarvam for any new
         recordings), filters + builds outputs, and emails each rep via raw
         SMTP (smtplib). Local machines only -- see --cloud below.
  python3 sales_nightly_report.py --no-refresh
      -> skips the pipeline run and the send; just rebuilds from the existing
         results.json, for local testing.

--cloud: the cloud sandbox this runs in as a scheduled routine only permits
  outbound HTTPS (via its proxy), not raw SMTP sockets -- smtplib.SMTP_SSL
  fails there with OSError: [Errno 97] Address family not supported by
  protocol. So with --cloud, no email is ever sent directly: instead, each
  email's content is printed as a `CLOUD_EMAIL_JSON: {...}` line on stdout,
  and the calling agent (which has the Gmail MCP tool, itself HTTPS-based)
  is responsible for actually sending each one. Without --cloud, emails are
  sent for real via smtplib, unchanged from before.

  The dashboard HTML (150-330KB) is never put in a CLOUD_EMAIL_JSON
  attachment -- past runs proved the calling agent can't reliably retype
  that much content into a single tool-call parameter (it silently
  truncates). Instead, in --cloud mode this script uploads each rep's
  dashboard.html straight to Google Drive itself (GDRIVE_SERVICE_ACCOUNT_JSON
  + GDRIVE_SHARED_DRIVE_ID), shares it with just that rep + CC_LIST, and
  puts the resulting link in the email body. Only the small PDF (~2-3KB)
  still goes through as a real attachment.
"""
import argparse
import csv
import datetime as dt
import json
import os
import smtplib
import traceback
from email.message import EmailMessage
from pathlib import Path

from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import SimpleDocTemplate, Table, TableStyle, Paragraph, Spacer

OUT_DIR = Path(__file__).parent
EMPLOYEES_CSV = OUT_DIR / "employees.csv"

# Management: CC'd on every rep's individual email.
CC_LIST = [
    "pranav@thekenkolife.com",
    "parimala@thekenkolife.com",
    "vivek@thekenkolife.com",
    "neeraj@thekenkolife.com",
]
FAILURE_RECIPIENT = "pranav@thekenkolife.com"


def load_sales_reps():
    """Read employees.csv -> list of sales reps: {name, alias, email}.
    `alias` is the lowercase first word of emp_name, since Callyzer's
    emp_name on each call is often just a first name (e.g. "Sophiya"),
    not employees.csv's fuller display name ("Sophiya Dash")."""
    reps = []
    if not EMPLOYEES_CSV.exists():
        return reps
    with open(EMPLOYEES_CSV, newline="", encoding="utf-8-sig") as f:
        for row in csv.DictReader(f):
            role = (row.get("role") or "").strip().lower()
            name = (row.get("emp_name") or "").strip()
            if role != "sales" or not name:
                continue
            reps.append({
                "name": name.title(),
                "alias": name.split()[0].lower(),
                "email": (row.get("email") or "").strip(),
            })
    return reps


def _canon_builder(sales_reps):
    def _canon(rep_raw):
        key = (rep_raw or "").strip().lower()
        for rep in sales_reps:
            if key == rep["alias"] or key.startswith(rep["alias"]):
                return rep["name"]
        return None
    return _canon


def load_filtered(results_path, canon_fn):
    payload = json.loads(Path(results_path).read_text())
    filtered = []
    for c in payload.get("calls", []):
        canon = canon_fn(c.get("rep"))
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


def build_pdf(stats, payload, out_path, title):
    doc = SimpleDocTemplate(str(out_path), pagesize=letter,
                             topMargin=0.6 * inch, bottomMargin=0.6 * inch)
    styles = getSampleStyleSheet()
    story = [
        Paragraph(title, styles["Title"]),
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

    doc.build(story)
    print(f"Wrote {out_path}")


def _smtp_send(msg):
    user = os.environ.get("SMTP_USER")
    pw = os.environ.get("SMTP_APP_PASSWORD")
    if not user or not pw:
        raise RuntimeError("SMTP_USER / SMTP_APP_PASSWORD not set in environment")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465) as s:
        s.login(user, pw)
        s.send_message(msg)


def _emit_cloud_email(payload):
    """Cloud sandboxes here can't open raw SMTP sockets (only HTTPS via their
    proxy), so instead of sending, print the email as one JSON line for the
    calling agent to dispatch via its (HTTPS-based) Gmail tool."""
    print("CLOUD_EMAIL_JSON:" + json.dumps(payload, ensure_ascii=False))


def _gdrive_service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON not set in environment")
    info = json.loads(creds_json)
    creds = service_account.Credentials.from_service_account_info(
        info, scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def upload_dashboard_to_drive(local_path, filename, rep_email, cc_list):
    """Upload a dashboard HTML file to the Kenko Sales Reports Shared Drive
    and share it with just that rep + cc_list (never "anyone with the link"
    -- these contain real customer call transcripts). Uploading via the
    Drive API directly means the file goes from disk to Google over HTTPS
    in this one process; no giant blob ever has to pass through an LLM
    tool-call parameter. Returns the file's webViewLink."""
    from googleapiclient.http import MediaFileUpload

    drive_id = os.environ.get("GDRIVE_SHARED_DRIVE_ID")
    if not drive_id:
        raise RuntimeError("GDRIVE_SHARED_DRIVE_ID not set in environment")

    service = _gdrive_service()
    media = MediaFileUpload(str(local_path), mimetype="text/html", resumable=False)
    file = service.files().create(
        body={"name": filename, "parents": [drive_id]},
        media_body=media,
        fields="id, webViewLink",
        supportsAllDrives=True,
    ).execute()
    file_id = file["id"]

    for email in [rep_email] + list(cc_list):
        if not email:
            continue
        service.permissions().create(
            fileId=file_id,
            body={"type": "user", "role": "reader", "emailAddress": email},
            sendNotificationEmail=False,
            supportsAllDrives=True,
        ).execute()

    return file.get("webViewLink") or f"https://drive.google.com/file/d/{file_id}/view"


def send_rep_email(rep_name, rep_email, single_stats, pdf_path, dashboard_path, cloud=False, test_mode=False):
    today = dt.date.today().isoformat()
    subject = f"{rep_name} — Call Coverage — {today}"
    if test_mode:
        subject = "[TEST] " + subject
    cc_list = [] if test_mode else CC_LIST

    s = single_stats[rep_name]
    avg = f"{s['avg_qa']}" if s["avg_qa"] is not None else "n/a"

    dashboard_link = None
    if cloud:
        dashboard_link = upload_dashboard_to_drive(
            dashboard_path, f"{rep_name} — Call Coverage — {today}.html", rep_email, cc_list)

    lines = [
        f"Hi {rep_name.split()[0]},", "",
        f"Your call coverage for {today}:",
        f"- {s['recorded']}/{s['total']} calls recorded ({s['rec_pct']}%)",
        f"- Avg QA score: {avg}",
        "",
    ]
    if cloud:
        lines += [
            f"Full interactive dashboard (click into any call for its transcript + "
            f"checklist): {dashboard_link}",
            "(Shared directly with you on Drive — sign in with your @thekenkolife.com "
            "account to view.)",
        ]
    else:
        lines.append(
            "Open the attached dashboard.html in a browser to click into any call "
            "and see its transcript + checklist (which items were hit/missed).")
    if s["total"] and s["recorded"] == 0:
        lines.insert(2, "NOTE: 0% recording coverage today — check call recording "
                         "is enabled on your phone in the Callyzer app.")
    if cloud:
        lines += ["", "— Ran via cloud routine"]
    if test_mode:
        lines += ["", "*** TEST RUN -- redirected from the real rep/CC recipients, not a real report ***"]
    body = "\n".join(lines)

    if cloud:
        _emit_cloud_email({
            "kind": "rep_report",
            "to": rep_email,
            "cc": cc_list,
            "subject": subject,
            "body": body,
            "attachments": [
                {"path": str(pdf_path), "filename": Path(pdf_path).name,
                 "mime_type": "application/pdf"},
            ],
        })
        print(f"[cloud] Queued report email for {rep_email} (cc: {', '.join(cc_list)}) "
              f"-- agent must send it via the Gmail tool using the CLOUD_EMAIL_JSON line above. "
              f"Dashboard uploaded to Drive: {dashboard_link}")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", FAILURE_RECIPIENT)
    msg["To"] = rep_email
    msg["Cc"] = ", ".join(cc_list)
    msg.set_content(body)
    msg.add_attachment(Path(pdf_path).read_bytes(), maintype="application",
                        subtype="pdf", filename=Path(pdf_path).name)
    msg.add_attachment(Path(dashboard_path).read_bytes(), maintype="text",
                        subtype="html", filename=Path(dashboard_path).name)
    _smtp_send(msg)
    print(f"Report email sent to {rep_email} (cc: {', '.join(CC_LIST)}).")


def send_failure_email(error_text, cloud=False):
    subject = f"Kenko Sales Call Coverage FAILED — {dt.date.today().isoformat()}"
    body = ("Tonight's sales call-QA report did not run. No report was sent to anyone.\n\n"
            f"Error:\n{error_text}")
    if cloud:
        body += "\n\n— Ran via cloud routine"
        _emit_cloud_email({"kind": "failure", "to": FAILURE_RECIPIENT,
                            "subject": subject, "body": body})
        print(f"[cloud] Queued failure email for {FAILURE_RECIPIENT} "
              f"-- agent must send it via the Gmail tool using the CLOUD_EMAIL_JSON line above.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", FAILURE_RECIPIENT)
    msg["To"] = FAILURE_RECIPIENT
    msg.set_content(body)
    _smtp_send(msg)
    print(f"Failure email sent to {FAILURE_RECIPIENT}.")


def send_warning_email(warning_text, cloud=False):
    subject = f"Kenko Sales Call Coverage — action needed — {dt.date.today().isoformat()}"
    body = warning_text
    if cloud:
        body += "\n\n— Ran via cloud routine"
        _emit_cloud_email({"kind": "warning", "to": FAILURE_RECIPIENT,
                            "subject": subject, "body": body})
        print(f"[cloud] Queued warning email for {FAILURE_RECIPIENT} "
              f"-- agent must send it via the Gmail tool using the CLOUD_EMAIL_JSON line above.")
        return

    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = os.environ.get("SMTP_USER", FAILURE_RECIPIENT)
    msg["To"] = FAILURE_RECIPIENT
    msg.set_content(body)
    _smtp_send(msg)
    print(f"Warning email sent to {FAILURE_RECIPIENT}.")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1,
                     help="days of Callyzer history to refresh via pipeline.run() before filtering")
    ap.add_argument("--no-refresh", action="store_true",
                     help="skip pipeline.run(), just rebuild from the existing results.json")
    ap.add_argument("--send", action="store_true",
                     help="email each rep their own report on success")
    ap.add_argument("--cloud", action="store_true",
                     help="running in the cloud sandbox: never send via raw SMTP "
                          "(it can't open one there); instead print each email as "
                          "a CLOUD_EMAIL_JSON line for the calling agent to send "
                          "via its Gmail tool, with a footer note added")
    ap.add_argument("--test-recipient", default=None,
                     help="TESTING ONLY: for every rep, override both the email "
                          "'to' and the Drive share target to this single address "
                          "instead of their real employees.csv email, and drop CC_LIST "
                          "entirely -- so a test run never emails real reps/managers or "
                          "grants them Drive access to test output.")
    ap.add_argument("--max-calls", type=int, default=None,
                     help="TESTING ONLY: after filtering to sales reps, keep only "
                          "the first N calls (across all reps) before building or "
                          "sending anything -- combine with --no-refresh for a fast, "
                          "zero-Sarvam-cost end-to-end test of the email/Drive flow.")
    args = ap.parse_args()

    try:
        if not args.no_refresh:
            import pipeline
            pipeline.run(args.days)

        sales_reps = load_sales_reps()
        if not sales_reps:
            raise RuntimeError("No employees.csv rows tagged role=sales — nothing to report.")

        canon_fn = _canon_builder(sales_reps)
        payload = load_filtered(OUT_DIR / "results.json", canon_fn)
        if args.max_calls is not None:
            payload = dict(payload)
            payload["calls"] = payload["calls"][:args.max_calls]
        stats = summarize(payload)

        print(f"\nFiltered calls: {len(payload['calls'])}")
        for rep, s in stats.items():
            print(f"  {rep}: {s['recorded']}/{s['total']} recorded ({s['rec_pct']}%), avg QA {s['avg_qa']}")
    except Exception:
        err = traceback.format_exc()
        print(err)
        if args.send:
            send_failure_email(err, cloud=args.cloud)
        raise

    reps_with_calls = [s for s in stats.values() if s["total"] > 0]
    systemic_failure = (reps_with_calls
                         and all(s["recorded"] == 0 for s in reps_with_calls))
    if systemic_failure:
        detail = "\n".join(f"{rep}: {s['recorded']}/{s['total']} recorded"
                            for rep, s in stats.items())
        err = ("Every rep shows 0% recording coverage — this looks like a systemic "
               "transcription/download failure, not a normal quiet night. Refusing "
               f"to send the misleading report to anyone.\n\n{detail}")
        print(err)
        if args.send:
            send_failure_email(err, cloud=args.cloud)
        return

    missing_email = []
    for rep in sales_reps:
        name = rep["name"]
        if name not in stats or stats[name]["total"] == 0:
            continue  # no calls this period -- nothing to send
        if not rep["email"] and not args.test_recipient:
            missing_email.append(name)
            continue

        rep_calls = [c for c in payload["calls"] if c["rep"] == name]
        rep_payload = dict(payload)
        rep_payload["calls"] = rep_calls
        alias = rep["alias"]
        pdf_path = OUT_DIR / f"sales_nightly_{alias}_summary.pdf"
        dashboard_path = OUT_DIR / f"sales_nightly_{alias}_dashboard.html"

        build_dashboard(rep_payload, dashboard_path)
        build_pdf({name: stats[name]}, rep_payload, pdf_path,
                  title=f"{name} — Call Coverage")

        if args.send:
            send_to = args.test_recipient or rep["email"]
            send_rep_email(name, send_to, stats, pdf_path, dashboard_path,
                            cloud=args.cloud, test_mode=bool(args.test_recipient))

    if missing_email and args.send:
        send_warning_email(
            "These sales reps had calls tonight but no `email` set in employees.csv, "
            "so they did NOT get their own report (add their email to the `email` "
            "column to fix):\n\n" + "\n".join(f"- {n}" for n in missing_email),
            cloud=args.cloud,
        )


if __name__ == "__main__":
    main()
