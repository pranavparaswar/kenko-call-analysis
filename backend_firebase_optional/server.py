"""
Kenko Call Analysis — single REST API service.

This is the ONE thing the app integrates with. Your app calls these endpoints;
it never touches the pipeline internals.

  GET  /health                      -> {"ok": true}
  GET  /config                      -> the call-type taxonomy (roles, types, checkpoints)
  POST /pull        {"days": 7}      -> starts a pull+analyze in the background -> {"run_id"}
  GET  /runs/{id}                   -> {"status", "message"}  (queued|running|done|error)
  GET  /calls?role=&call_type=&status=&limit=  -> [ analyzed call objects ]

Run:
  pip install fastapi uvicorn requests sarvamai
  export CALLYZER_TOKEN=...   SARVAM_API_KEY=...
  uvicorn server:app --host 0.0.0.0 --port 8080
(or use the Dockerfile). See INTEGRATION.md for the full contract.
"""
import os
import sys
import json
import uuid
import threading
from pathlib import Path

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# import the proven engine (pipeline.py sits one level up)
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import pipeline  # noqa: E402

RESULTS = Path(pipeline.CONFIG["OUT_DIR"]) / "results.json"

app = FastAPI(title="Kenko Call Analysis API", version="1.0")
app.add_middleware(
    CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])

_runs = {}          # run_id -> {"status", "message"}
_lock = threading.Lock()


@app.get("/health")
def health():
    keys_ok = not (pipeline.CONFIG["CALLYZER_TOKEN"].startswith("PASTE")
                   or pipeline.CONFIG["SARVAM_API_KEY"].startswith("PASTE"))
    return {"ok": True, "keys_configured": keys_ok}


@app.get("/config")
def config():
    """The call-type taxonomy the app uses to render checklists dynamically."""
    return {"taxonomy": pipeline.CALL_TYPES, "roles": list(pipeline.CALL_TYPES.keys())}


@app.post("/pull")
def pull(body: dict = None):
    days = int((body or {}).get("days", 7))
    run_id = uuid.uuid4().hex[:12]
    with _lock:
        _runs[run_id] = {"status": "queued", "message": f"pull last {days} days"}

    def worker():
        with _lock:
            _runs[run_id] = {"status": "running", "message": f"processing last {days} days"}
        try:
            pipeline.run(days)
            with _lock:
                _runs[run_id] = {"status": "done", "message": "completed"}
        except Exception as e:
            with _lock:
                _runs[run_id] = {"status": "error", "message": str(e)[:400]}

    threading.Thread(target=worker, daemon=True).start()
    return {"run_id": run_id, "status": "queued"}


@app.get("/runs/{run_id}")
def run_status(run_id: str):
    with _lock:
        return _runs.get(run_id, {"status": "unknown", "message": "no such run"})


@app.get("/calls")
def calls(role: str = Query(None), call_type: str = Query(None),
          status: str = Query(None), limit: int = Query(500)):
    if not RESULTS.exists():
        return JSONResponse([], status_code=200)
    data = json.loads(RESULTS.read_text())
    rows = data.get("calls", [])
    if role:
        rows = [c for c in rows if c.get("role") == role]
    if call_type:
        rows = [c for c in rows if c.get("call_type") == call_type]
    if status:
        rows = [c for c in rows if c.get("status", "done") == status]
    rows = sorted(rows, key=lambda c: c.get("date", ""), reverse=True)
    return rows[:limit]


@app.get("/")
def root():
    return {"service": "Kenko Call Analysis API",
            "endpoints": ["/health", "/config", "/pull", "/runs/{id}", "/calls"]}
