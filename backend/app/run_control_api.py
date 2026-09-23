from __future__ import annotations

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

from .research.run_control import active_run, request_cancel

router = APIRouter(prefix="/api/runs", tags=["run-control"])


class StopRunRequest(BaseModel):
    actor: str | None = None


@router.get("/active")
def get_active_run():
    return {"item": active_run()}


@router.post("/{run_id}/cancel")
def cancel_run(run_id: int, payload: StopRunRequest):
    try:
        return {"item": request_cancel(run_id, actor=payload.actor)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
