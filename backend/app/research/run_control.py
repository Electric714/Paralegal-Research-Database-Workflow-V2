from __future__ import annotations

from typing import Any

from .. import database as db

PAUSE_REQUESTED = "pause_requested"
STOP_REQUESTED = "stop_requested"
PAUSED = "paused"
CANCELLED = "cancelled"

_CONTROL_REQUESTS = {PAUSE_REQUESTED, STOP_REQUESTED}
_TERMINAL = {"completed", "partial", CANCELLED}


def _update_run(run_id: int, *, status: str, message: str, completed: bool = False) -> dict[str, Any]:
    # Validate first so callers consistently receive ValueError for a bad id.
    db.get_run(run_id)
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE research_runs
            SET status=?, message=?, completed_at=CASE WHEN ? THEN ? ELSE completed_at END
            WHERE id=?
            """,
            (status, message, 1 if completed else 0, db.utcnow(), run_id),
        )
    return db.get_run(run_id)


def requested_action(run_id: int) -> str | None:
    status = str(db.get_run(run_id)["status"])
    return status if status in _CONTROL_REQUESTS else None


def request_pause(run_id: int, *, actor: str | None = None) -> dict[str, Any]:
    run = db.get_run(run_id)
    status = str(run["status"])
    if status in {PAUSED, PAUSE_REQUESTED}:
        return run
    if status in _TERMINAL or status == STOP_REQUESTED:
        raise ValueError(f"Research run {run_id} cannot be paused while status is {status}.")

    item = _update_run(
        run_id,
        status=PAUSE_REQUESTED,
        message="Pause requested. The run will pause after the current source task returns.",
    )
    db.add_diagnostic(
        "INFO",
        "Research run pause requested",
        stage="research",
        details={"run_id": run_id, "actor": actor},
    )
    return item


def request_stop(run_id: int, *, actor: str | None = None) -> dict[str, Any]:
    run = db.get_run(run_id)
    status = str(run["status"])
    if status == CANCELLED:
        return run
    if status in {"completed", "partial"}:
        raise ValueError(f"Research run {run_id} is already finished with status {status}.")

    # A paused run has no active worker. It can be cancelled immediately.
    if status == PAUSED:
        item = _update_run(
            run_id,
            status=CANCELLED,
            message="Research run stopped by the user while paused. Unchecked tasks were not executed.",
            completed=True,
        )
    else:
        item = _update_run(
            run_id,
            status=STOP_REQUESTED,
            message="Stop requested. The run will stop after the current source task returns.",
        )

    db.add_diagnostic(
        "WARNING",
        "Research run stop requested",
        stage="research",
        details={"run_id": run_id, "actor": actor, "previous_status": status},
    )
    return item


def prepare_resume(run_id: int, *, actor: str | None = None) -> dict[str, Any]:
    run = db.get_run(run_id)
    status = str(run["status"])
    if status != PAUSED:
        raise ValueError(f"Research run {run_id} can only resume from paused status, not {status}.")

    item = _update_run(
        run_id,
        status="planned",
        message="Resume requested. Remaining unchecked tasks are queued to continue.",
    )
    db.add_diagnostic(
        "INFO",
        "Research run resume requested",
        stage="research",
        details={"run_id": run_id, "actor": actor},
    )
    return item
