"""Persist the pipeline's caches (results.json / transcripts_cache.json) to Google Drive.

WHY THIS EXISTS
---------------
Each cloud routine run gets a brand-new sandbox with a fresh `git clone`. Both
cache files are gitignored (they hold customer transcripts and phone numbers,
which must not land in git history), so nothing survives from one scheduled run
to the next. Verified directly on 2026-10-19: the 5 PM run transcribed all 91 of
that day's recordings and wrote a results.json checkpoint; the 7 PM run, two
hours later, found `results.json: No such file or directory`.

Without a shared cache every run re-fetches, re-downloads, re-transcribes and
re-analyzes the whole day from midnight -- paying Sarvam and Claude again for
work already done -- and since a backgrounded run is killed at ~30 minutes, it
usually throws that work away before finishing.

Syncing through the same Shared Drive the pipeline already uses for dashboards
gives every run a common, resumable cache while keeping PII out of the repo.
The service account and drive id are the ones already in the environment
(GDRIVE_SERVICE_ACCOUNT_JSON / GDRIVE_SHARED_DRIVE_ID).

DESIGN NOTES
------------
- Opt-in: off unless `enable()` is called (via `--cache-sync-drive`), so local
  laptop runs never read from or clobber the shared copy.
- Never fatal: any Drive error is reported and swallowed. A Drive hiccup must
  not take down the nightly report -- the worst case is simply the old
  behaviour of redoing work.
- Pushes during long loops are throttled, because transcripts_cache.json runs
  to several MB and the analysis loop checkpoints every 10 calls.
"""

import io
import json
import os
import time
from pathlib import Path

# Remote copies are prefixed so they can't collide with the per-rep dashboard
# HTML files that already live in the same Shared Drive.
REMOTE_PREFIX = "pipeline_cache__"
FILES = ("results.json", "transcripts_cache.json")

_ENABLED = False
_SERVICE = None
_last_push = {}


def enable():
    """Turn syncing on for this process (cloud routine runs only)."""
    global _ENABLED
    _ENABLED = True


def is_enabled():
    return _ENABLED


def _service():
    global _SERVICE
    if _SERVICE is None:
        from google.oauth2 import service_account
        from googleapiclient.discovery import build

        creds_json = os.environ.get("GDRIVE_SERVICE_ACCOUNT_JSON")
        if not creds_json:
            raise RuntimeError("GDRIVE_SERVICE_ACCOUNT_JSON not set in environment")
        creds = service_account.Credentials.from_service_account_info(
            json.loads(creds_json), scopes=["https://www.googleapis.com/auth/drive"])
        _SERVICE = build("drive", "v3", credentials=creds, cache_discovery=False)
    return _SERVICE


def _drive_id():
    did = os.environ.get("GDRIVE_SHARED_DRIVE_ID")
    if not did:
        raise RuntimeError("GDRIVE_SHARED_DRIVE_ID not set in environment")
    return did


def _find(remote_name):
    """File id of `remote_name` in the Shared Drive, or None if absent."""
    res = _service().files().list(
        q=f"name = '{remote_name}' and trashed = false",
        spaces="drive",
        corpora="drive",
        driveId=_drive_id(),
        includeItemsFromAllDrives=True,
        supportsAllDrives=True,
        fields="files(id, name, modifiedTime, size)",
        pageSize=10,
    ).execute()
    files = res.get("files", [])
    return files[0]["id"] if files else None


def pull(out_dir):
    """Download both caches from Drive into `out_dir`, overwriting local copies.

    Call once at the very start of a run, BEFORE results.json is read for the
    skip-already-analyzed set. A missing remote file is normal (first run of a
    new day, or first ever run) and is not an error.
    """
    if not _ENABLED:
        return
    try:
        from googleapiclient.http import MediaIoBaseDownload
    except ImportError as e:
        # Import lives inside the guard on purpose: a missing google client in
        # the sandbox must degrade to "no shared cache", never take down the run.
        print(f"  [drive-cache] google client unavailable ({e}) — skipping pull.")
        return

    for local in FILES:
        remote = REMOTE_PREFIX + local
        try:
            fid = _find(remote)
            if not fid:
                print(f"  [drive-cache] {local}: nothing on Drive yet — starting fresh.")
                continue
            buf = io.BytesIO()
            dl = MediaIoBaseDownload(
                buf, _service().files().get_media(fileId=fid, supportsAllDrives=True))
            done = False
            while not done:
                _, done = dl.next_chunk()
            data = buf.getvalue()
            # Only overwrite once the payload parses -- a truncated or corrupt
            # download must not replace a good local file mid-run.
            json.loads(data)
            Path(out_dir, local).write_bytes(data)
            print(f"  [drive-cache] pulled {local} ({len(data):,} bytes).")
        except Exception as e:
            print(f"  [drive-cache] pull of {local} failed ({e}) — continuing without it.")


def push(out_dir, *names):
    """Upload the named caches (default: both) to Drive, replacing the remote copy."""
    if not _ENABLED:
        return
    try:
        from googleapiclient.http import MediaFileUpload
    except ImportError as e:
        print(f"  [drive-cache] google client unavailable ({e}) — skipping push.")
        return

    for local in (names or FILES):
        p = Path(out_dir, local)
        if not p.exists():
            continue
        remote = REMOTE_PREFIX + local
        try:
            media = MediaFileUpload(str(p), mimetype="application/json", resumable=False)
            fid = _find(remote)
            if fid:
                _service().files().update(
                    fileId=fid, media_body=media, supportsAllDrives=True).execute()
            else:
                _service().files().create(
                    body={"name": remote, "parents": [_drive_id()]},
                    media_body=media, fields="id", supportsAllDrives=True).execute()
            _last_push[local] = time.time()
            print(f"  [drive-cache] pushed {local} ({p.stat().st_size:,} bytes).")
        except Exception as e:
            print(f"  [drive-cache] push of {local} failed ({e}) — continuing.")


def push_throttled(out_dir, name, min_seconds=120):
    """push() at most once per `min_seconds` for this file.

    The analysis loop checkpoints every 10 calls and the transcript cache is
    saved after every single transcription; uploading several MB that often
    would cost more time than the re-analysis it saves.
    """
    if not _ENABLED:
        return
    if time.time() - _last_push.get(name, 0) < min_seconds:
        return
    push(out_dir, name)
