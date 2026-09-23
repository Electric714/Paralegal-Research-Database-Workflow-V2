from __future__ import annotations

from typing import Any

from .. import database as db

ACTIVE_RUN_STATUSES = frozenset({"running", "cancel_requested"})
CANCELLED_STATUS = "cancelled"


def active_run() -> dict[str, Any] | None:
    for run in db.list_runs(20):
        if str(run.get("status")) in ACTIVE_RUN_STATUSES:
            return run
    return None


def cancellation_requested(run_id: int) -> bool:
    status = str(db.get_run(run_id).get("status"))
    return status in {"cancel_requested", CANCELLED_STATUS}


def request_cancel(run_id: int, *, actor: str | None = None) -> dict[str, Any]:
    run = db.get_run(run_id)
    status = str(run.get("status"))
    if status == CANCELLED_STATUS:
        return run
    if status not in {"planned", "running", "cancel_requested"}:
        raise ValueError(f"Research run {run_id} is already {status} and cannot be stopped.")

    if status != "cancel_requested":
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE research_runs
                SET status='cancel_requested',
                    message='Stop requested by operator; finishing the current source request before stopping.'
                WHERE id=?
                """,
                (run_id,),
            )
        db.add_diagnostic(
            "INFO",
            "Research run stop requested",
            stage="research",
            details={"run_id": run_id, "actor": actor or "local-user"},
        )
    return db.get_run(run_id)


def finalize_cancel(run_id: int, *, remaining_tasks: int | None = None) -> dict[str, Any]:
    run = db.get_run(run_id)
    if str(run.get("status")) == CANCELLED_STATUS:
        return run

    remaining_text = ""
    if remaining_tasks is not None:
        remaining_text = f" {remaining_tasks} task(s) were left not checked."
    message = "Research run stopped by operator." + remaining_text
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE research_runs
            SET status=?, message=?, completed_at=?
            WHERE id=?
            """,
            (CANCELLED_STATUS, message, db.utcnow(), run_id),
        )
    db.add_diagnostic(
        "INFO",
        "Research run stopped",
        stage="research",
        details={"run_id": run_id, "remaining_tasks": remaining_tasks},
    )
    return db.get_run(run_id)
