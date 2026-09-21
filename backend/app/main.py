from __future__ import annotations

import csv
import io
import json
import re
import uuid
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, Query, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from . import database as db
from .import_validation import ValidationReport, validate_bidder_rows
from .research.field_mappings import SOURCE_FIELD_MAPPINGS
from .research.service import list_tasks, record_identity_judgment, review_change
from .sources import SOURCES, SOURCE_KEYS

APP_NAME = "Paralegal Research Desk"
MAX_CSV_BYTES = 25 * 1024 * 1024
CORE_COLUMNS = {"id", "contractor_name"}
EXPECTED_COLUMNS = [
    "id", "contractor_name", "related_companies", "address_1", "city", "state", "zip",
    "additional_address", "additional_address_city", "additional_address_state", "additional_address_zip",
    "dfi", "wc", "wc_date", "osha_severe_violations", "years", "osha", "state_federal_debarment",
    "mndol_ineligibility", "public_works_projects_budget_time_quality_complaint", "federal_court",
    "circuit_court", "ccap_show150", "environmental_violations", "prevailing_wage_violations", "dwd",
    "dwd_substance_abuse_plan", "better_business_bureau_complaints", "misc_violations", "tax_liability",
]

app = FastAPI(title=APP_NAME, version="0.2.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def startup() -> None:
    db.init_db()


class RunRequest(BaseModel):
    source_keys: list[str]
    bidder_ids: list[int] | None = None


class IdentityJudgmentRequest(BaseModel):
    bidder_id: int
    source_key: str
    source_record_id: str
    judgment: str
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    decided_by: str | None = None
    notes: str | None = None


class ReviewDecisionRequest(BaseModel):
    decision: str
    actor: str | None = None
    note: str | None = None


def _decode_csv(data: bytes) -> str:
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    raise HTTPException(422, "CSV encoding is not supported. Save the file as UTF-8 or Windows-1252.")


def _parse_csv(data: bytes) -> tuple[list[str], list[dict[str, str]]]:
    if not data:
        raise HTTPException(422, "The uploaded CSV is empty.")
    if len(data) > MAX_CSV_BYTES:
        raise HTTPException(413, "The uploaded CSV exceeds the 25 MB proof-of-concept limit.")
    text = _decode_csv(data)
    reader = csv.DictReader(io.StringIO(text, newline=""))
    if not reader.fieldnames:
        raise HTTPException(422, "The CSV does not contain a header row.")
    columns = [str(col or "").strip() for col in reader.fieldnames]
    if any(not col for col in columns):
        raise HTTPException(422, "The CSV contains a blank column name.")
    if len(set(columns)) != len(columns):
        raise HTTPException(422, "The CSV contains duplicate column names.")
    missing_core = sorted(CORE_COLUMNS - set(columns))
    if missing_core:
        raise HTTPException(422, f"Missing required column(s): {', '.join(missing_core)}")

    rows: list[dict[str, str]] = []
    for index, raw in enumerate(reader, start=2):
        if None in raw:
            raise HTTPException(422, f"Row {index} contains more values than the header defines.")
        row = {column: (raw.get(column) or "").strip() for column in columns}
        if not any(row.values()):
            continue
        rows.append(row)
    if not rows:
        raise HTTPException(422, "The CSV contains no bidder records.")
    return columns, rows


def _validate_import(columns: list[str], rows: list[dict[str, str]]) -> ValidationReport:
    report = validate_bidder_rows(columns, rows)
    missing_expected = [column for column in EXPECTED_COLUMNS if column not in columns]
    if missing_expected:
        report.errors.insert(
            0,
            "Missing expected bidder field(s): " + ", ".join(missing_expected),
        )
    return report


@app.get("/api/health")
def health():
    return {"name": APP_NAME, "status": "ok", "version": "0.2.0"}


@app.get("/api/schema")
def schema():
    return {
        "expected_columns": EXPECTED_COLUMNS,
        "column_count": len(EXPECTED_COLUMNS),
        "core_columns": sorted(CORE_COLUMNS),
        "source_field_mappings": {
            key: {"owned_fields": sorted(mapping.owned_fields), "notes": mapping.notes}
            for key, mapping in SOURCE_FIELD_MAPPINGS.items()
        },
    }


@app.get("/api/dashboard")
def dashboard():
    active = db.active_import()
    reviews = db.list_review_proposals()
    runs = db.list_runs(1)
    return {
        "bidder_count": db.count_bidders(),
        "source_count": len(SOURCES),
        "implemented_source_count": sum(1 for source in SOURCES if source["status"] == "ready"),
        "pending_review_count": sum(1 for item in reviews if item["status"] == "pending"),
        "active_import": active,
        "last_run": runs[0] if runs else None,
    }


@app.get("/api/sources")
def sources():
    return {"items": SOURCES}


@app.post("/api/import/preview")
async def preview_import(file: UploadFile = File(...)):
    data = await file.read()
    columns, rows = _parse_csv(data)
    report = _validate_import(columns, rows)
    return {
        "filename": file.filename or "upload.csv",
        "row_count": len(rows),
        "columns": columns,
        "missing_expected_columns": [col for col in EXPECTED_COLUMNS if col not in columns],
        "extra_columns": [col for col in columns if col not in EXPECTED_COLUMNS],
        "validation_errors": report.errors,
        "validation_warnings": report.warnings,
        "valid_for_import": report.valid,
        "preview": rows[:5],
    }


@app.post("/api/import")
async def import_master(file: UploadFile = File(...)):
    data = await file.read()
    columns, rows = _parse_csv(data)
    report = _validate_import(columns, rows)
    if not report.valid:
        raise HTTPException(422, {"message": "Bidder CSV validation failed.", "errors": report.errors})

    db.ensure_dirs()
    original = Path(file.filename or "master.csv").name
    safe_name = re.sub(r"[^A-Za-z0-9._-]+", "_", original)
    snapshot_name = f"{uuid.uuid4().hex}_{safe_name}"
    snapshot = db.IMPORT_DIR / snapshot_name
    snapshot.write_bytes(data)
    import_id = db.replace_master_database(original, columns, rows, str(snapshot))
    db.add_diagnostic(
        "INFO",
        "Master bidder database imported",
        stage="import",
        details={
            "import_id": import_id,
            "filename": original,
            "rows": len(rows),
            "columns": columns,
            "validation_warnings": report.warnings,
        },
    )
    return {
        "import_id": import_id,
        "filename": original,
        "row_count": len(rows),
        "columns": columns,
        "validation_warnings": report.warnings,
    }


@app.get("/api/import/current")
def current_import():
    return {"item": db.active_import()}


@app.get("/api/bidders")
def bidders(search: str = "", limit: int = Query(100, ge=1, le=500), offset: int = Query(0, ge=0)):
    items, total = db.list_bidders(search=search, limit=limit, offset=offset)
    return {"items": items, "total": total, "limit": limit, "offset": offset}


@app.get("/api/bidders/{bidder_id}")
def bidder(bidder_id: int):
    item = db.get_bidder(bidder_id)
    if not item:
        raise HTTPException(404, "Bidder not found")
    return {"item": item}


@app.get("/api/export")
def export_master():
    active = db.active_import()
    if not active:
        raise HTTPException(404, "No master bidder database has been imported.")
    rows = db.all_bidder_rows()
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=active["columns"], extrasaction="ignore", lineterminator="\n")
    writer.writeheader()
    for row in rows:
        writer.writerow({column: row.get(column, "") for column in active["columns"]})
    filename = f"approved_bidder_database_export_{active['id']}.csv"
    return StreamingResponse(
        iter([output.getvalue()]),
        media_type="text/csv",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/runs")
def create_run(payload: RunRequest):
    if not payload.source_keys:
        raise HTTPException(422, "Select at least one research source.")
    unknown = sorted(set(payload.source_keys) - SOURCE_KEYS)
    if unknown:
        raise HTTPException(422, f"Unknown source key(s): {', '.join(unknown)}")
    if payload.bidder_ids:
        valid = [
            bidder_id
            for bidder_id in payload.bidder_ids
            if (item := db.get_bidder(bidder_id)) and item.get("_active")
        ]
        if len(valid) != len(payload.bidder_ids):
            raise HTTPException(422, "One or more selected bidders do not exist in the active master database.")
        bidder_count = len(valid)
    else:
        bidder_count = db.count_bidders()
    if bidder_count == 0:
        raise HTTPException(422, "Import the bidder database before creating a research run.")
    return {"item": db.create_run(payload.bidder_ids, payload.source_keys, bidder_count)}


@app.get("/api/runs")
def runs():
    return {"items": db.list_runs()}


@app.get("/api/tasks")
def tasks(research_run_id: int | None = None):
    return {"items": list_tasks(research_run_id)}


@app.post("/api/identity-judgments")
def identity_judgment(payload: IdentityJudgmentRequest):
    if payload.source_key not in SOURCE_KEYS:
        raise HTTPException(422, "Unknown source key.")
    try:
        judgment_id = record_identity_judgment(**payload.model_dump())
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    return {"id": judgment_id}


@app.get("/api/review")
def review_queue():
    return {"items": db.list_review_proposals()}


@app.post("/api/review/{change_id}")
def review_decision(change_id: int, payload: ReviewDecisionRequest):
    try:
        return {"item": review_change(change_id, **payload.model_dump())}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/diagnostics")
def diagnostics(limit: int = Query(500, ge=1, le=5000)):
    return {"items": db.list_diagnostics(limit)}


@app.delete("/api/diagnostics")
def clear_diagnostics():
    db.clear_diagnostics()
    return {"ok": True}


@app.get("/api/diagnostics/export")
def export_diagnostics():
    payload = {"application": APP_NAME, "diagnostics": db.list_diagnostics(5000)}
    text = json.dumps(payload, indent=2, ensure_ascii=False)
    return StreamingResponse(
        iter([text]),
        media_type="application/json",
        headers={"Content-Disposition": 'attachment; filename="paralegal-research-diagnostics.json"'},
    )


# In normal local use the launcher builds the React application first. Serving it
# here means the user runs one local server, not separate backend/frontend windows.
FRONTEND_DIST = Path(__file__).resolve().parents[2] / "frontend" / "dist"
if FRONTEND_DIST.exists():
    app.mount("/", StaticFiles(directory=str(FRONTEND_DIST), html=True), name="frontend")
