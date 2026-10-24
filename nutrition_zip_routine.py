#!/usr/bin/env python3
"""
Nutrition zip -> consultation_analysis CSV, as a daily Drive-backed routine
===========================================================================
Wraps the two existing zip parsers so a scheduled cloud run can do the whole
job unattended:

  1. find the export zip in a fixed Google Drive folder (newest one wins),
  2. auto-detect which zip shape it is and parse it with the matching module,
  3. upload the resulting consultation_analysis CSV back to that same folder,
  4. email a link + a short run summary.

Zip shapes (detected from the archive's own contents, never from its name):
  - root `manifest.csv`      -> nutrition_export_run.py  (flat Storage pull)
  - `<doc_id>/document.json` -> consultation_export.py   (Firebase export)
Neither present is a hard error -- better to shout than to silently email an
empty CSV.

Run:
    python3 nutrition_zip_routine.py --ensure-folder
        -> creates (or just finds) the Drive folder, prints its id + link.
           Idempotent; run once at setup, and any time you want the link.
    python3 nutrition_zip_routine.py --cloud
        -> the daily routine: pick up the newest zip, parse, upload, queue
           the email as a CLOUD_EMAIL_JSON line for the calling agent.
    python3 nutrition_zip_routine.py --dry-run --limit 3
        -> local smoke test: parses 3 records, writes the CSV, uploads and
           emails nothing.

WHY A TIME BUDGET
-----------------
A backgrounded cloud routine run is killed at ~30 minutes, and transcription
is the slow part (one Sarvam STT call per recording). So record processing
stops at --max-minutes and the run still finishes properly: the CSV is written
from everything cached so far, uploaded, and the email says how many records
are left. Caches are pulled from / pushed to Drive around the run, so the next
day's run resumes instead of re-paying for work already done.
"""

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import tempfile
import time
import traceback
import zipfile
from pathlib import Path

HERE = Path(__file__).parent
sys.path.insert(0, str(HERE))

import consultation_export as ce
import drive_cache

# The Drive folder zips are dropped into. Resolved by name inside the Shared
# Drive the pipeline already uses, so there's no new id to configure -- but
# NUTRITION_ZIP_FOLDER_ID / --folder-id override it if you'd rather pin one.
FOLDER_NAME = os.environ.get("NUTRITION_ZIP_FOLDER_NAME", "Nutrition Call Exports")

# Who gets the run email and the CSV's Drive share. A CC list isn't wired in
# here on purpose: these CSVs carry raw transcripts, so the default is the
# narrowest useful audience -- the person who actually works the export.
DEFAULT_TO = os.environ.get("NUTRITION_REPORT_TO", "anurag@thekenkolife.com")

OUT_CSV = HERE / "consultation_analysis.csv"

# Caches worth carrying between runs. The transcript cache is shared by both
# shapes (keyed by doc_id); each shape keeps its own extraction cache.
CACHE_FILES = (
    "consultation_transcripts_cache.json",
    "consultation_extract_cache.json",
    "nutrition_export_extract_cache.json",
)


# --------------------------------------------------------------------------
# Drive
# --------------------------------------------------------------------------

def _service():
    from google.oauth2 import service_account
    from googleapiclient.discovery import build

    creds_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
    if not creds_json:
        raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON not set in environment")
    creds = service_account.Credentials.from_service_account_info(
        json.loads(creds_json), scopes=["https://www.googleapis.com/auth/drive"])
    return build("drive", "v3", credentials=creds, cache_discovery=False)


def _drive_id():
    did = os.environ.get("GDRIVE_SHARED_DRIVE_ID")
    if not did:
        raise RuntimeError("GDRIVE_SHARED_DRIVE_ID not set in environment")
    return did


def ensure_folder(service, create=True, share_with=()):
    """Folder id for FOLDER_NAME in the Shared Drive, creating it if absent.

    Returns (folder_id, web_link). Raises if it doesn't exist and create=False,
    so a daily run fails loudly rather than quietly inventing a second folder
    nobody is dropping zips into.
    """
    pinned = os.environ.get("NUTRITION_ZIP_FOLDER_ID")
    if pinned:
        meta = service.files().get(
            fileId=pinned, fields="id, name, webViewLink",
            supportsAllDrives=True).execute()
        return meta["id"], meta.get("webViewLink", "")

    res = service.files().list(
        q=("name = '%s' and mimeType = 'application/vnd.google-apps.folder' "
           "and trashed = false" % FOLDER_NAME.replace("'", "\\'")),
        corpora="drive", driveId=_drive_id(),
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        fields="files(id, name, webViewLink)", pageSize=10,
    ).execute()
    files = res.get("files", [])
    if files:
        return files[0]["id"], files[0].get("webViewLink", "")

    if not create:
        raise RuntimeError(
            f"Drive folder {FOLDER_NAME!r} not found in the Shared Drive. "
            f"Run `python3 {Path(__file__).name} --ensure-folder` once to create it.")

    folder = service.files().create(
        body={"name": FOLDER_NAME,
              "mimeType": "application/vnd.google-apps.folder",
              "parents": [_drive_id()]},
        fields="id, webViewLink", supportsAllDrives=True,
    ).execute()
    fid = folder["id"]
    for email in share_with:
        if email:
            _share(service, fid, email)
    return fid, folder.get("webViewLink", "")


def _share(service, file_id, email):
    """Grant one person read access. Never 'anyone with the link' -- these
    files hold real customer transcripts and phone numbers."""
    service.permissions().create(
        fileId=file_id,
        body={"type": "user", "role": "reader", "emailAddress": email},
        sendNotificationEmail=False, supportsAllDrives=True,
    ).execute()


def newest_zip(service, folder_id):
    """Newest .zip in the folder, or None. Sorted by createdTime so a
    re-uploaded zip of the same name still reads as the current one."""
    res = service.files().list(
        q=f"'{folder_id}' in parents and trashed = false",
        corpora="drive", driveId=_drive_id(),
        includeItemsFromAllDrives=True, supportsAllDrives=True,
        orderBy="createdTime desc",
        fields="files(id, name, size, createdTime, mimeType)", pageSize=100,
    ).execute()
    for f in res.get("files", []):
        if f["name"].lower().endswith(".zip"):
            return f
    return None


def download(service, file_id, dest):
    from googleapiclient.http import MediaIoBaseDownload

    with open(dest, "wb") as fh:
        dl = MediaIoBaseDownload(
            fh, service.files().get_media(fileId=file_id, supportsAllDrives=True),
            chunksize=8 * 1024 * 1024)
        done = False
        while not done:
            _, done = dl.next_chunk()
    return dest


def upload_csv(service, folder_id, local_path, filename, share_with=()):
    from googleapiclient.http import MediaFileUpload

    media = MediaFileUpload(str(local_path), mimetype="text/csv", resumable=False)
    f = service.files().create(
        body={"name": filename, "parents": [folder_id]},
        media_body=media, fields="id, webViewLink", supportsAllDrives=True,
    ).execute()
    for email in share_with:
        if email:
            _share(service, f["id"], email)
    return f.get("webViewLink") or f"https://drive.google.com/file/d/{f['id']}/view"


# --------------------------------------------------------------------------
# Zip shape detection + parsing
# --------------------------------------------------------------------------

# Matched against the paths each loader actually consumes, so detection can't
# pick a parser that then quietly finds zero records: consultation_export's
# load_consultations() only reads `consultations/<doc_id>/...`.
DOC_ENTRY_RE = re.compile(r"(^|/)consultations/[^/]+/document\.json$")


def detect_shape(zip_path):
    """'manifest' | 'document', from the archive's contents."""
    with zipfile.ZipFile(zip_path) as z:
        names = z.namelist()
    if any(n.lstrip("./") == "manifest.csv" for n in names):
        return "manifest"
    if any(DOC_ENTRY_RE.search(n) for n in names):
        return "document"
    raise RuntimeError(
        "Unrecognised export zip: no root manifest.csv and no "
        f"consultations/<doc_id>/document.json. Top entries: {names[:10]}")


def _records(shape, zip_path):
    """(z, {doc_id: info}, build_row, extract_cache_path) for the detected shape."""
    if shape == "manifest":
        import nutrition_export_run as ne
        z, recs = ne.load_manifest_recordings(zip_path)
        return (z, recs,
                lambda doc_id, info, tcache: ne.build_row(doc_id, info, z, tcache),
                ne.EXTRACT_CACHE_PATH)

    z, dirs = ce.load_consultations(zip_path)
    for info in dirs.values():
        info["_z"] = z
    return (z, dirs,
            lambda doc_id, info, tcache: ce.build_row(doc_id, info, tcache),
            ce.EXTRACT_CACHE_PATH)


def process(zip_path, shape, out_csv, limit=None, max_minutes=20.0, overwrite=False):
    """Parse the zip into out_csv. Returns a stats dict for the email."""
    _, recs, build_row, ecache_path = _records(shape, zip_path)

    doc_ids = sorted(recs.keys())
    if limit:
        doc_ids = doc_ids[:limit]

    tcache = ce._load_json_cache(ce.TRANSCRIPT_CACHE_PATH)
    ecache = ce._load_json_cache(ecache_path)

    deadline = time.time() + max_minutes * 60
    total = len(doc_ids)
    new = cached = failed = 0
    remaining = 0

    for i, doc_id in enumerate(doc_ids, 1):
        if not overwrite and doc_id in ecache:
            cached += 1
            continue
        if time.time() > deadline:
            # Out of budget: everything still unprocessed is reported, not lost.
            remaining = total - cached - new - failed
            print(f"[time] {max_minutes:.0f} min budget spent — stopping with "
                  f"{remaining} record(s) left for the next run.")
            break
        print(f"[{i}/{total}] {doc_id} processing...")
        try:
            ecache[doc_id] = build_row(doc_id, recs[doc_id], tcache)
            new += 1
        except Exception as e:
            # One bad record must not cost the whole run: log it, keep going.
            print(f"  [{doc_id}] FAILED: {e}")
            failed += 1
            continue
        ce._save_json_cache(ecache_path, ecache)
        ce._save_json_cache(ce.TRANSCRIPT_CACHE_PATH, tcache)
        drive_cache.push_throttled(HERE, ecache_path.name)
        drive_cache.push_throttled(HERE, ce.TRANSCRIPT_CACHE_PATH.name)

    ce._save_json_cache(ecache_path, ecache)
    ce._save_json_cache(ce.TRANSCRIPT_CACHE_PATH, tcache)

    # Write every doc_id the cache holds, not just this zip's -- same rule as
    # nutrition_export_run.py, so a later zip accumulates into one CSV rather
    # than dropping earlier exports' rows.
    rows = 0
    with open(out_csv, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=ce.FIELDNAMES)
        w.writeheader()
        for doc_id in sorted(ecache.keys()):
            row = dict(ecache[doc_id])
            row["document_id"] = doc_id
            w.writerow({k: row.get(k, "") for k in ce.FIELDNAMES})
            rows += 1

    return {"records_in_zip": total, "newly_processed": new, "from_cache": cached,
            "failed": failed, "remaining": remaining, "csv_rows": rows}


# --------------------------------------------------------------------------
# Email
# --------------------------------------------------------------------------

def build_email(to, zip_name, link, stats, shape):
    today = dt.date.today().isoformat()
    subject = f"Nutrition call analysis — {today}"
    lines = [
        f"Parsed {zip_name} ({shape} export).", "",
        f"CSV ({stats['csv_rows']} rows): {link}",
        "(Shared directly with you on Drive — sign in with your "
        "@thekenkolife.com account to view.)", "",
        "This run:",
        f"- {stats['records_in_zip']} record(s) in the zip",
        f"- {stats['newly_processed']} newly transcribed + analysed",
        f"- {stats['from_cache']} already done on an earlier run (not re-charged)",
    ]
    if stats["failed"]:
        lines.append(f"- {stats['failed']} failed (see the run log)")
    if not stats["records_in_zip"]:
        # A recognised zip holding nothing to analyse is worth saying out loud --
        # otherwise this email looks identical to a normal no-op day.
        lines += ["",
                  "WARNING: this zip contained no analysable records (no call "
                  "recordings). The CSV above is unchanged from the last run — "
                  "worth checking the export."]
    if stats["remaining"]:
        lines += ["",
                  f"{stats['remaining']} record(s) hit the run's time budget and "
                  f"are still to do — tomorrow's run picks them up automatically."]
    lines += ["", "— Ran via cloud routine"]
    return {"kind": "nutrition_zip_report", "to": to, "cc": [],
            "subject": subject, "body": "\n".join(lines), "attachments": []}


def deliver(payload, cloud):
    if cloud:
        # The cloud sandbox allows outbound HTTPS only, not raw SMTP sockets
        # (see sales_nightly_report.py) -- the calling agent sends this via
        # its Gmail tool.
        print("CLOUD_EMAIL_JSON:" + json.dumps(payload, ensure_ascii=False))
        print(f"[cloud] Queued run email for {payload['to']} — agent must send it "
              f"via the Gmail tool using the CLOUD_EMAIL_JSON line above.")
        return
    from email.message import EmailMessage
    import sales_nightly_report as snr  # reuse the one SMTP path in this repo

    msg = EmailMessage()
    msg["Subject"] = payload["subject"]
    msg["From"] = os.environ.get("SMTP_USER", payload["to"])
    msg["To"] = payload["to"]
    msg.set_content(payload["body"])
    snr._smtp_send(msg)
    print(f"Run email sent to {payload['to']}.")


# --------------------------------------------------------------------------

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ensure-folder", action="store_true",
                    help="create/find the Drive folder, print its link, and exit")
    ap.add_argument("--folder-id", default=None, help="override the folder lookup")
    ap.add_argument("--zip", default=None,
                    help="parse this local zip instead of pulling one from Drive")
    ap.add_argument("--out", default=str(OUT_CSV))
    ap.add_argument("--to", default=DEFAULT_TO)
    ap.add_argument("--limit", type=int, default=None)
    ap.add_argument("--overwrite", action="store_true",
                    help="re-analyse records already in the extract cache")
    ap.add_argument("--max-minutes", type=float, default=20.0,
                    help="stop processing records after this long (default 20)")
    ap.add_argument("--cloud", action="store_true",
                    help="emit the email as CLOUD_EMAIL_JSON instead of sending via SMTP")
    ap.add_argument("--dry-run", action="store_true",
                    help="parse and write the CSV, but upload nothing and email nobody")
    ap.add_argument("--cache-sync-drive", action="store_true",
                    help="pull/push the transcript + extract caches via Drive")
    args = ap.parse_args()

    service = None
    if not (args.zip and args.dry_run):
        service = _service()

    if args.ensure_folder:
        fid, link = ensure_folder(service, create=True, share_with=[args.to])
        print(f"Drive folder {FOLDER_NAME!r}\n  id:   {fid}\n  link: {link}")
        print("Drop export zips in here; the daily routine picks up the newest one.")
        return 0

    if args.cache_sync_drive:
        drive_cache.enable()
        drive_cache.pull(HERE, *CACHE_FILES)

    tmpdir = None
    if args.zip:
        zip_path, zip_name = Path(args.zip), Path(args.zip).name
        folder_id = None
    else:
        folder_id = args.folder_id or ensure_folder(service, create=False)[0]
        meta = newest_zip(service, folder_id)
        if not meta:
            # Not an error: an ordinary quiet day with nothing dropped in.
            print(f"No zip in {FOLDER_NAME!r} — nothing to do.")
            return 0
        tmpdir = tempfile.TemporaryDirectory()
        zip_name = meta["name"]
        size = int(meta.get("size") or 0)
        print(f"Newest zip: {zip_name} ({size:,} bytes, created {meta['createdTime']})")
        zip_path = download(service, meta["id"], Path(tmpdir.name) / zip_name)

    shape = detect_shape(zip_path)
    print(f"Detected shape: {shape}")
    stats = process(zip_path, shape, args.out, limit=args.limit,
                    max_minutes=args.max_minutes, overwrite=args.overwrite)
    print(f"\n{stats}")
    if not stats["records_in_zip"]:
        print(f"WARNING: {zip_name} held no analysable records.")

    if args.cache_sync_drive:
        drive_cache.push(HERE, *CACHE_FILES)

    if args.dry_run:
        print(f"[dry-run] Wrote {args.out}; uploaded nothing, emailed nobody.")
        return 0

    today = dt.date.today().isoformat()
    link = upload_csv(service, folder_id or ensure_folder(service, create=False)[0],
                      args.out, f"consultation_analysis — {today}.csv",
                      share_with=[args.to])
    print(f"Uploaded CSV: {link}")
    deliver(build_email(args.to, zip_name, link, stats, shape), args.cloud)
    if tmpdir:
        tmpdir.cleanup()
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception:
        traceback.print_exc()
        sys.exit(1)
