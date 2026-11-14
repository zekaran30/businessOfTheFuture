"""
Millennium Mutual money ledger: web API and static frontend.

Run from the project folder:
    python run.py            (or: uvicorn backend.app:app --reload)
then open http://127.0.0.1:8000
"""
from __future__ import annotations

import io
import math
from datetime import date, datetime
from pathlib import Path

import numpy as np
import pandas as pd
from fastapi import Body, FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles

from . import detector as D
from . import metrics as M
from . import pipeline as P
from .store import DATA, DEFAULT_ASSUMPTIONS, ROOT, Store

app = FastAPI(title="Millennium Mutual: money ledger", version="1.0")
store = Store()
SAMPLE = DATA / "sample_new_quarter"


def jsonable(obj):
    """Recursively convert numpy / pandas values so they serialise cleanly."""
    if isinstance(obj, dict):
        return {str(k): jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set)):
        return [jsonable(v) for v in obj]
    if isinstance(obj, (np.integer,)):
        return int(obj)
    if isinstance(obj, (np.floating, float)):
        v = float(obj)
        return None if (math.isnan(v) or math.isinf(v)) else v
    if isinstance(obj, (np.bool_,)):
        return bool(obj)
    if isinstance(obj, (pd.Timestamp, datetime, date)):
        return obj.strftime("%Y-%m-%d") if not pd.isna(obj) else None
    if obj is pd.NaT or obj is pd.NA:
        return None
    return obj


def ok(payload) -> JSONResponse:
    return JSONResponse(jsonable(payload))


def _assumptions(body: dict) -> dict:
    a = {**DEFAULT_ASSUMPTIONS, **(body.get("assumptions") or {})}
    return {k: min(max(float(v), 0.0), 0.9) for k, v in a.items()}


def _batches_summary() -> list:
    out = []
    for b in store.batches:
        out.append({k: b.get(k) for k in ("id", "name", "uploaded_at", "as_of", "quarter")} | {"report": b.get("report")})
    return out


# ------------------------------------------------------------------- pages
@app.get("/", include_in_schema=False)
def index():
    return FileResponse(ROOT / "frontend" / "index.html")


app.mount("/static", StaticFiles(directory=ROOT / "frontend"), name="static")


# -------------------------------------------------------------------- meta
@app.get("/api/meta")
def meta():
    quarters = M.quarters_between(store.data_start, store.asof)
    return ok({
        "asof": store.asof, "data_start": store.data_start, "extraction_date": P.EXTRACTION_DATE,
        "quarters": [{"value": q, "label": M.quarter_label(q, store.asof)} for q in quarters],
        "options": M.filter_options(store.policies, store.claims),
        "dimensions": [{"value": k, "label": v[0], "scope": v[2]} for k, v in M.DIMENSIONS.items()],
        "assumptions": DEFAULT_ASSUMPTIONS, "rules": D.DEFAULT_RULES,
        "counts": {"policies": len(store.policies), "claims": len(store.claims)},
        "batches": [{k: b.get(k) for k in ("id", "name", "uploaded_at", "as_of", "quarter")} for b in store.batches],
        "alerts_new": sum(1 for a in store.alerts if a.get("workflow") == "New"),
        "sample_available": (SAMPLE / "policies_2026Q4.csv").exists(),
    })


# ------------------------------------------------------------------ ledger
@app.post("/api/ledger")
def ledger(body: dict = Body(default={})):
    filters = body.get("filters") or {}
    dim = body.get("dim") if body.get("dim") in M.DIMENSIONS else "city_product"
    heat_rows = body.get("heat_rows") if body.get("heat_rows") in M.DIMENSIONS else "city"
    heat_cols = body.get("heat_cols") if body.get("heat_cols") in M.DIMENSIONS else "policy_type"
    if M.DIMENSIONS[heat_rows][2] == "claim" or M.DIMENSIONS[heat_cols][2] == "claim":
        heat_rows, heat_cols = "city", "policy_type"
    with store.lock:
        result = M.ledger(store.policies, store.claims, filters, _assumptions(body), store.asof, store.data_start,
                          dim, heat_rows, heat_cols, D.DEFAULT_RULES["min_earned"])
    return ok(result)


# ------------------------------------------------------------------- check
def _run_check(quarter: str, body: dict) -> dict:
    quarters = M.quarters_between(store.data_start, store.asof)
    if quarter not in quarters:
        raise HTTPException(400, f"No data for {quarter}. Available: {quarters[0]} to {quarters[-1]}.")
    with store.lock:
        return D.run_check(store.policies, store.claims, quarter, _assumptions(body), body.get("rules") or {},
                           store.asof, store.data_start)


@app.post("/api/check")
def check(body: dict = Body(default={})):
    """Run the quarterly check. With "log": true the automated actions run too."""
    quarter = body.get("quarter") or M.quarter_of(store.asof)
    result = _run_check(quarter, body)
    actions = store.log_check(result, trigger=f"Manual check of {result['label']}") if body.get("log") else None
    return ok({"check": result, "actions": actions})


# ----------------------------------------------------------------- uploads
def _read_upload(f: UploadFile | None):
    if f is None or not f.filename:
        return None
    raw = f.file.read()
    if not raw.strip():
        return None
    try:
        return P.read_text_csv(raw)
    except Exception as exc:
        raise HTTPException(400, f"Could not read {f.filename} as CSV: {exc}")


def _ingest(name: str, policies_raw, claims_raw, as_of: str | None, body: dict) -> dict:
    if policies_raw is None and claims_raw is None:
        raise HTTPException(400, "Choose a policies file, a claims file, or both.")
    try:
        batch = store.add_batch(name, policies_raw, claims_raw, as_of or None)
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    quarter = body.get("quarter") or batch["quarter"]
    result = _run_check(quarter, body)
    actions = store.log_check(result, trigger=f"New data: {name}")
    return {"batch": {k: batch.get(k) for k in ("id", "name", "uploaded_at", "as_of", "quarter", "report")},
            "check": result, "actions": actions}


@app.post("/api/upload")
def upload(policies: UploadFile | None = File(default=None), claims: UploadFile | None = File(default=None),
           as_of: str = Form(default=""), quarter: str = Form(default="")):
    """Add a new extract, clean it with the same rules, then run the check and its actions automatically."""
    names = [f.filename for f in (policies, claims) if f is not None and f.filename]
    payload = _ingest(" + ".join(names) or "Upload", _read_upload(policies), _read_upload(claims), as_of,
                      {"quarter": quarter or None})
    return ok(payload)


@app.post("/api/upload-sample")
def upload_sample():
    p, c = SAMPLE / "policies_2026Q4.csv", SAMPLE / "claims_2026Q4.csv"
    if not p.exists():
        raise HTTPException(404, "Sample files not found in data/sample_new_quarter.")
    payload = _ingest("Sample 2026 Q4 extract", P.read_text_csv(p), P.read_text_csv(c), None, {})
    return ok(payload)


@app.get("/api/sample/{which}")
def sample_file(which: str):
    files = {"policies": "policies_2026Q4.csv", "claims": "claims_2026Q4.csv"}
    if which not in files or not (SAMPLE / files[which]).exists():
        raise HTTPException(404, "Unknown sample file.")
    return FileResponse(SAMPLE / files[which], filename=files[which], media_type="text/csv")


@app.post("/api/manual")
def manual(body: dict = Body(...)):
    """Add one policy or one claim typed into the form, then re-run the check for its quarter."""
    kind = body.get("kind")
    if kind not in ("policy", "claim"):
        raise HTTPException(400, "kind must be 'policy' or 'claim'.")
    result = store.add_manual(kind, body.get("row") or {})
    payload = {"entry": result}
    if result["accepted"] and result["quarter"]:
        check = _run_check(result["quarter"], body)
        payload["check"] = check
        payload["actions"] = store.log_check(check, trigger=f"Hand-entered {kind} {result['id']}")
    return ok(payload)


@app.post("/api/reset")
def reset():
    store.reset()
    return ok({"reset": True})


# ------------------------------------------------------------------ alerts
@app.get("/api/alerts")
def alerts():
    order = {"critical": 0, "high": 1, "watch": 2}
    items = sorted(store.alerts, key=lambda a: (a["quarter"], -order[a["severity"]], a.get("shortfall") or 0),
                   reverse=True)
    return ok({"alerts": items, "outbox": store.outbox[:50]})


@app.post("/api/alerts/{alert_id}")
def update_alert(alert_id: str, body: dict = Body(...)):
    workflow = body.get("workflow")
    if workflow not in ("New", "Acknowledged", "Resolved"):
        raise HTTPException(400, "workflow must be New, Acknowledged or Resolved.")
    a = store.set_alert_workflow(alert_id, workflow)
    if a is None:
        raise HTTPException(404, "Alert not found.")
    return ok(a)


@app.get("/api/alerts/{alert_id}/review.csv")
def review_csv(alert_id: str):
    a = next((x for x in store.alerts if x["id"] == alert_id), None)
    if a is None or not a.get("review"):
        raise HTTPException(404, "No review list for this alert.")
    buf = io.StringIO()
    pd.DataFrame(a["review"]).to_csv(buf, index=False)
    name = f"senior-review_{a['segment'].replace(' / ', '-').replace(' ', '-')}_{a['quarter']}.csv"
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{name}"'})


# -------------------------------------------------------------- data notes
@app.get("/api/data-notes")
def data_notes():
    return ok({
        "log": store.base_log, "rejected": store.base_rejected,
        "batches": _batches_summary(),
        "counts": {"policies": len(store.policies), "claims": len(store.claims)},
    })


@app.get("/api/export/flagged-claims.csv")
def export_claims(segment_lens: str = "", quarter: str = ""):
    """Download the claims behind the current view (for audits)."""
    buf = io.StringIO()
    cols = ["claim_id", "policy_id", "city", "policy_type", "source", "claim_type", "loss_date", "claim_amount",
            "paid", "reserve", "status", "adjuster_team"]
    store.claims[cols].to_csv(buf, index=False)
    return StreamingResponse(iter([buf.getvalue()]), media_type="text/csv",
                             headers={"Content-Disposition": 'attachment; filename="claims_clean.csv"'})
