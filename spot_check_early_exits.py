#!/usr/bin/env python3
"""
Spot-check tool for improvement #1 (early_exit_reason reliability).

Every call the model tags as an "early exit" (customer disengaged / location
undeliverable, through no fault of the rep) is excluded from qa_score and the
rep's averages — no human ever reviews that judgment call. A rep who
under-pitches could get the same free pass as a rep who genuinely got
stonewalled, if the model is too charitable.

This reads the existing results.json (no API calls, no cost) and lists every
excluded call that ran well past MIN_DURATION_SEC before being excluded —
sorted longest-first, since the longer a call ran before being called a
"no-fault" early exit, the more suspicious it is (a customer who really
wasn't interested usually shows it fast; a call that ran 2-3 minutes and
still got excluded is worth a human listen).

Usage:
    python3 spot_check_early_exits.py [--min-duration 90] [--top 20]
"""
import json
import argparse
from pathlib import Path

ap = argparse.ArgumentParser()
ap.add_argument("--min-duration", type=int, default=90,
                help="only flag excluded calls that ran at least this long (seconds)")
ap.add_argument("--top", type=int, default=25, help="max calls to list")
args = ap.parse_args()

rj = Path(__file__).parent / "results.json"
if not rj.exists():
    raise SystemExit("No results.json found — run the pipeline first.")

data = json.loads(rj.read_text())
calls = data.get("calls", [])

# status == "short" covers both genuine sub-30s calls (analyze_short_call) and
# longer calls the model flagged with early_exit_reason (analyze). Filtering
# on duration_sec >= min_duration isolates the second group — the ones where
# a real analysis ran and the model chose to excuse the rep anyway.
flagged = [c for c in calls
           if c.get("status") == "short"
           and (c.get("duration_sec") or 0) >= args.min_duration]
flagged.sort(key=lambda c: -(c.get("duration_sec") or 0))

if not flagged:
    print(f"No excluded calls >= {args.min_duration}s found in {len(calls)} total calls. "
          f"Nothing to spot-check.")
else:
    print(f"{len(flagged)} excluded call(s) ran >= {args.min_duration}s before being "
          f"marked no-fault (of {len(calls)} total calls). Listening to a sample of "
          f"these is the fastest way to tell if the model is being too charitable:\n")
    for c in flagged[:args.top]:
        mins = (c.get("duration_sec") or 0) // 60
        secs = (c.get("duration_sec") or 0) % 60
        print(f"  {c.get('date','?'):<11} {c.get('rep','?'):<16} "
              f"{mins}m{secs:02d}s  drop_reason={c.get('drop_reason','?')!r}  "
              f"call_id={c.get('call_id','?')}")
        note = c.get("drop_note", "")
        if note:
            print(f"      note: {note}")
    if len(flagged) > args.top:
        print(f"\n  ...and {len(flagged) - args.top} more not shown (--top {args.top}).")
