from __future__ import annotations

import json
from typing import Any

from fastapi import APIRouter, BackgroundTasks, HTTPException, Query
from fastapi.responses import StreamingResponse

from . import database as db
from .research.executor import execute_research_run
from .research.run_control import prepare_resume, request_pause, request_stop
from .research.run_summary import (
    AMBIGUOUS_STATUSES, BLOCKED_STATUSES, FAILED_STATUSES, PARTIAL_STATUSES,
    get_run_summary,
)
from .research.service import list_tasks

router = APIRouter()


def _http_control_error(exc: ValueError) -> HTTPException:
    message = str(exc)
    status = 404 if "does not exist" in message else 409
    return HTTPException(status, message)


@router.post("/api/runs/{run_id}/pause")
def pause_run(run_id: int, actor: str | None = Query(default="local-user")):
    try:
        return {"item": request_pause(run_id, actor=actor)}
    except ValueError as exc:
        raise _http_control_error(exc) from exc


@router.post("/api/runs/{run_id}/stop")
def stop_run(run_id: int, actor: str | None = Query(default="local-user")):
    try:
        return {"item": request_stop(run_id, actor=actor)}
    except ValueError as exc:
        raise _http_control_error(exc) from exc


@router.post("/api/runs/{run_id}/resume")
def resume_run(
    run_id: int,
    background_tasks: BackgroundTasks,
    actor: str | None = Query(default="local-user"),
):
    try:
        item = prepare_resume(run_id, actor=actor)
    except ValueError as exc:
        raise _http_control_error(exc) from exc

    # Return immediately so the UI keeps working while the remaining tasks run.
    background_tasks.add_task(execute_research_run, run_id)
    return {"item": item, "scheduled": True}


@router.get("/api/runs/{run_id}/diagnostics/export")
def export_run_diagnostics(run_id: int):
    try:
        run = db.get_run(run_id)
        summary = get_run_summary(run_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc

    tasks = list_tasks(run_id)
    run_diagnostics: list[dict[str, Any]] = []
    for item in db.list_diagnostics(5000):
        details = item.get("details") or {}
        if details.get("run_id") == run_id:
            run_diagnostics.append(item)

    issue_statuses = BLOCKED_STATUSES | FAILED_STATUSES | PARTIAL_STATUSES | AMBIGUOUS_STATUSES
    problematic_tasks = [task for task in tasks if str(task.get("status")) in issue_statuses]
    pending_tasks = [task for task in tasks if str(task.get("status")) == "NOT_CHECKED"]

    payload = {
        "application": "Paralegal Research Desk",
        "export_type": "research_run_live_diagnostics",
        "exported_at": db.utcnow(),
        "run": run,
        "summary": summary,
        "problematic_tasks": problematic_tasks,
        "pending_task_count": len(pending_tasks),
        "diagnostics": run_diagnostics,
    }
    text = json.dumps(payload, indent=2, ensure_ascii=False, default=str)
    return StreamingResponse(
        iter([text]),
        media_type="application/json",
        headers={
            "Content-Disposition": f'attachment; filename="research-run-{run_id}-diagnostics.json"'
        },
    )
