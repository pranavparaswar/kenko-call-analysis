# Kenko Call Analysis

Pulls recorded calls from Callyzer, transcribes them with Sarvam, classifies each
call by type, scores it against a weighted checklist, and builds an HTML dashboard.

## What's in this folder

**Core (this is what you run):**
- `pipeline.py` — the whole pipeline + CLI
- `dashboard_template.html` — dashboard source (pipeline injects data into this)
- `dashboard.html` — generated dashboard (open in a browser)
- `employees.csv` — reps and their roles (Sales / Nutrition analyzed; Ops skipped)
- `requirements.txt`, `.env.example`
- `sample_results.json` — 64 demo calls (used by `--demo`)
- `results.json` / `results.csv` — last real run output
- `transcripts_cache.json` — cached transcripts so reruns don't re-transcribe
- `kenko_call_results.xlsx` — spreadsheet view: Calls, Rep Summary, Scoring Reference
  (built from the sample data; a real run refreshes results.json, then see below to re-export)

**docs/** — architecture, plan, integration notes, original READMEs.

**backend_firebase_optional/** — an alternate real-time Firebase backend
(`main.py`, `core.py`, `server.py`, `seed_roles.py`). NOT needed for the pipeline
above. Note `core.py` still uses the older flat-checklist scoring, not the weighted
taxonomy in `pipeline.py`.

## Quick start

```bash
pip install -r requirements.txt

# see the dashboard with demo data, no keys needed:
python3 pipeline.py --demo
open dashboard.html

# real run:
cp .env.example .env          # then edit .env with your keys
export CALLYZER_TOKEN="..."   # or: set them from .env however you prefer
export SARVAM_API_KEY="..."

python3 pipeline.py --check --days 7   # connection test, no Sarvam charges
python3 pipeline.py --days 1           # real run, 1 day (transcribes -> bills Sarvam)
open dashboard.html
```

`--days N` overwrites `results.json`. Back it up first if you want to keep a run:
`cp results.json results_backup.json`.

## Scoring

Each call type has weighted checkpoints (see the **Scoring Reference** sheet in the
xlsx, or `CALL_TYPES` in `pipeline.py`). Weight 3 = core, 2 = important, 1 = hygiene.
`qa_score` = weighted % of checkpoints covered, so the score always matches the
checklist. The model's own rating is kept alongside as `qa_score_model`.

Nutrition call types: **onboarding**, **consultation**, **escalation**.
Consultation checkpoints come from Kenko's app tabs (Meals / Progress / Feedback /
Coach Notes). Onboarding additionally checks for plan **start date** and **food
storage/handling** (refrigerate / microwave) instructions.
