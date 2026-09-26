# Kenko Call Analysis

Weekly pipeline that pulls call recordings from **Callyzer**, transcribes them with
**Sarvam** (batch, diarized), runs an analysis, and produces a **dashboard** you open
in your browser. Handles both **sales** and **nutritionist** calls, each scored
against its own must-cover checklist.

```
Callyzer /call-log/history  ->  download MP3s  ->  Sarvam batch STT (diarized)
     ->  detect role (sales / nutrition)  ->  analysis (summary · sentiment · QA · must-cover)
     ->  results.json / results.csv  ->  dashboard.html
```

## Files
| File | What it is |
|------|-----------|
| `dashboard.html` | **Open this in your browser.** Loaded with sample data so you can see the layout. |
| `pipeline.py` | The pipeline. Run weekly to refresh with real calls. |
| `dashboard_template.html` | Template the pipeline injects data into (don't open directly). |
| `employees.csv` | Created by `--employees`. Tag each person's `role` here. |
| `sample_results.json` | Sample data powering the demo dashboard. |
| `results.json` / `results.csv` | Created after a real run. |

---

# Step-by-step

### 1. Install Python + dependencies
You need Python 3.9+ installed. Then, in a terminal:
```bash
pip install requests sarvamai
```

### 2. Get your two keys
- **Callyzer token:** Callyzer dashboard -> generate API token. It's sent as
  `Authorization: Bearer <token>`.
- **Sarvam key:** platform.sarvam.ai -> API Keys.

### 3. Put the keys in as environment variables (don't hard-code them)
Mac/Linux:
```bash
export CALLYZER_TOKEN="your-callyzer-token"
export SARVAM_API_KEY="your-sarvam-key"
```
Windows (PowerShell):
```powershell
setx CALLYZER_TOKEN "your-callyzer-token"
setx SARVAM_API_KEY "your-sarvam-key"
```

### 4. Tag who is sales and who is nutrition  ← important, but easy
Pull the full employee list from Callyzer into a spreadsheet:
```bash
python pipeline.py --employees
```
This writes **`employees.csv`** with every employee (name, number, code, existing
Callyzer tags) and a blank **`role`** column. Open it, type `sales` or `nutrition`
in the `role` column for each person, and save. That's it — the pipeline reads this
file automatically. Re-running `--employees` later keeps roles you've already set
and just adds any new staff.

(You don't need to touch code. If you prefer, you can instead fill `REP_ROLES` in
`pipeline.py`, or rely on Callyzer's own "Sales"/"Nutrition" tags — both work. Order
of precedence: employees.csv → REP_ROLES → Callyzer tags → model fallback.)

### 5. Edit the two must-cover checklists
In `pipeline.py`, find `CHECKLISTS`. There are two lists — `sales` and `nutrition`.
Each item has:
- `id` — short internal key
- `label` — what shows on the dashboard
- `criteria` — plain-English instruction telling the model what to look for

Add/remove items freely; the dashboard adapts. (Nutrition defaults include progress
review, recording weight/measurements, diet adherence, next check-in, etc.)

### 6. Verify the connection, then do a small test run
First just check the Callyzer token works (no transcription, no Sarvam usage):
```bash
python pipeline.py --check
```
It prints how many calls it can see, how many have recordings, the field names in
the response, and which reps appear — handy for filling in `REP_ROLES`.

Then process just the last day to confirm the whole chain end to end:
```bash
python pipeline.py --days 1
```

### 7. Run the full week + open the dashboard
```bash
python pipeline.py --days 7
```
Then open (or refresh) `dashboard.html`. That's your weekly review.

To preview the dashboard anytime without touching the APIs:
```bash
python pipeline.py --demo
```

---

## What the dashboard shows
- **Sales / Nutrition / All toggle** at the top — filters the entire view.
- **KPIs:** calls analyzed, avg QA score, positive sentiment %, must-cover compliance %.
- **Compliance by rep** — who's covering their required items, who isn't.
- **Sentiment split** and **per-role checklist coverage**.
- **Calls table** — sortable/filterable, role-tagged; click any row for the full
  breakdown (summary, sentiment reason, QA coaching notes, per-item checklist, action items).

## How sales vs nutrition is decided (in order)
1. `employees.csv` role column (run `--employees`, tag each person).
2. `REP_ROLES` map in `pipeline.py` (phone number or exact name).
3. Callyzer `emp_tags` containing "sales" / "nutrition".
4. Model classification from the transcript (fallback for unmapped numbers).

## Notes & things to confirm
- Transcription uses the Sarvam **Batch API** (`saaras:v3`) with diarization —
  handles 10-min calls in one job, 20 files per job, agent/customer separated.
- Callyzer recording URLs may or may not need the bearer token to download — the
  script tries both.
- Analysis uses Sarvam Chat Completions (`sarvam-m`); swap the model in `analyze()`
  if that id is deprecated for your account.
- Callyzer field names (`emp_name`, `client_name`, `call_date`, `emp_tags`, etc.)
  are best-effort mappings in `_row()` / `detect_role()`; send one real response
  and they can be locked in exactly.
- To automate: schedule `python pipeline.py` every Monday (cron / Task Scheduler).

## Later: FlutterFlow
FlutterFlow consumes data, not this HTML page. `results.json` is already the data
source — when you're ready, push it to Firestore (or expose it as a JSON endpoint)
and build the screens in FlutterFlow against the same fields. No rework on the
analysis side.
