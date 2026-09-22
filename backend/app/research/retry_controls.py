from __future__ import annotations

from collections import Counter
from typing import Any

from .. import database as db
from .executor import _contractor_context, _unexpected_failure, execute_research_run
from .models import SourceResultStatus
from .persistence import add_audit_event, utcnow
from .retry_policy import is_retryable_status
from .run_summary import get_run_summary
from .service import list_tasks, persist_source_result
from .source_registry import create_source, implemented_source_keys


def _task(task_id: int) -> dict[str, Any]:
    with db.connect() as conn:
        row = conn.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
    if not row:
        raise ValueError(f"Research task {task_id} does not exist.")
    return dict(row)


def _refresh_run_from_tasks(run_id: int) -> dict[str, Any]:
    summary = get_run_summary(run_id)
    if summary["attention_tasks"]:
        status = "partial"
    else:
        status = "completed"
    message = (
        f"Research state reconciled: {summary['safe_complete_tasks']} safe-complete, "
        f"{summary['attention_tasks']} attention task(s), "
        f"{summary['pending_change_count']} pending change(s)."
    )
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE research_runs
            SET status=?, message=?, completed_at=?
            WHERE id=?
            """,
            (status, message, utcnow(), run_id),
        )
    return get_run_summary(run_id)


def _execute_retry(task: dict[str, Any], *, actor: str | None = None) -> dict[str, Any]:
    task_id = int(task["id"])
    run_id = int(task["research_run_id"])
    bidder_id = int(task["bidder_id"])
    source_key = str(task["source_key"])
    previous_status = str(task["status"])

    if not is_retryable_status(previous_status):
        raise ValueError(f"Task {task_id} is not retryable from status {previous_status}.")
    if source_key not in implemented_source_keys():
        raise ValueError(f"Source {source_key} does not have an implemented adapter.")

    bidder = db.get_bidder(bidder_id)
    if not bidder or not bidder.get("_active"):
        raise ValueError("This task belongs to a bidder that is no longer active in the approved master database.")

    contractor = _contractor_context(bidder_id)
    adapter = create_source(source_key)
    if adapter is None:
        raise ValueError(f"Source {source_key} does not have an implemented adapter.")

    try:
        adapter.prepare()
        result = adapter.search(contractor)
    except Exception as exc:  # a retry failure is still preserved as evidence/history
        result = _unexpected_failure(task, contractor, exc)
        db.add_diagnostic(
            "ERROR",
            "Source adapter failed unexpectedly during retry",
            source_key=source_key,
            bidder_name=contractor.contractor_name,
            stage="research_retry",
            details={"run_id": run_id, "task_id": task_id, "error": repr(exc)},
        )

    persisted = persist_source_result(task_id, result)
    updated = _task(task_id)
    with db.connect() as conn:
        add_audit_event(
            conn,
            "research_task_retried",
            f"Retried research task {task_id}: {previous_status} -> {result.status.value}.",
            actor=actor,
            bidder_id=bidder_id,
            research_run_id=run_id,
            source_key=source_key,
            entity_type="research_task",
            entity_id=task_id,
            details={
                "previous_status": previous_status,
                "result_status": result.status.value,
                "attempt_count": int(updated["attempt_count"]),
                "snapshot_id": persisted["snapshot_id"],
                "proposal_ids": persisted["proposal_ids"],
                "reconfirmed_proposal_ids": persisted.get("reconfirmed_proposal_ids", []),
                "superseded_proposal_ids": persisted.get("superseded_proposal_ids", []),
            },
        )

    return {
        "task": updated,
        "previous_status": previous_status,
        "result_status": result.status.value,
        "snapshot_id": persisted["snapshot_id"],
        "proposal_ids": persisted["proposal_ids"],
        "reconfirmed_proposal_ids": persisted.get("reconfirmed_proposal_ids", []),
        "superseded_proposal_ids": persisted.get("superseded_proposal_ids", []),
    }


def retry_task(task_id: int, *, actor: str | None = None) -> dict[str, Any]:
    task = _task(task_id)
    result = _execute_retry(task, actor=actor)
    summary = _refresh_run_from_tasks(int(task["research_run_id"]))
    return {**result, "summary": summary}


def retry_run_problems(run_id: int, *, actor: str | None = None) -> dict[str, Any]:
    db.get_run(run_id)
    tasks = list_tasks(run_id)
    retryable = [task for task in tasks if is_retryable_status(str(task["status"]))]

    with db.connect() as conn:
        add_audit_event(
            conn,
            "research_run_retry_requested",
            f"Retry requested for {len(retryable)} retryable task(s) in run {run_id}.",
            actor=actor,
            research_run_id=run_id,
            entity_type="research_run",
            entity_id=run_id,
            details={"task_ids": [int(task["id"]) for task in retryable]},
        )

    status_counts: Counter[str] = Counter()
    proposal_count = 0
    reconfirmed_count = 0
    superseded_count = 0
    results: list[dict[str, Any]] = []
    for task in retryable:
        item = _execute_retry(task, actor=actor)
        results.append(item)
        status_counts[item["result_status"]] += 1
        proposal_count += len(item["proposal_ids"])
        reconfirmed_count += len(item["reconfirmed_proposal_ids"])
        superseded_count += len(item["superseded_proposal_ids"])

    summary = _refresh_run_from_tasks(run_id) if retryable else get_run_summary(run_id)
    return {
        "run": summary["run"],
        "retried": len(retryable),
        "status_counts": dict(status_counts),
        "proposal_count": proposal_count,
        "reconfirmed_proposal_count": reconfirmed_count,
        "superseded_proposal_count": superseded_count,
        "results": results,
        "summary": summary,
    }


def rerun_research_run(run_id: int, *, actor: str | None = None) -> dict[str, Any]:
    original = db.get_run(run_id)
    scope = original.get("bidder_scope") or {}
    source_keys = list(original.get("source_keys") or [])
    if not source_keys:
        raise ValueError("The original run does not contain any research sources.")

    if scope.get("type") == "selected":
        original_ids = [int(value) for value in scope.get("bidder_ids") or []]
        bidder_ids = [
            bidder_id
            for bidder_id in original_ids
            if (bidder := db.get_bidder(bidder_id)) and bidder.get("_active")
        ]
        if not bidder_ids:
            raise ValueError("None of the original selected bidders are active in the current approved master database.")
        bidder_count = len(bidder_ids)
    else:
        bidder_ids = None
        bidder_count = db.count_bidders()
        if bidder_count == 0:
            raise ValueError("Import the bidder database before re-running research.")

    new_run = db.create_run(bidder_ids, source_keys, bidder_count)
    with db.connect() as conn:
        add_audit_event(
            conn,
            "research_run_rerun_created",
            f"Created run {new_run['id']} as a fresh re-run of run {run_id}.",
            actor=actor,
            research_run_id=int(new_run["id"]),
            entity_type="research_run",
            entity_id=int(new_run["id"]),
            details={"rerun_of_run_id": run_id, "source_keys": source_keys, "bidder_count": bidder_count},
        )

    execution = execute_research_run(int(new_run["id"]))
    return {"rerun_of_run_id": run_id, "item": execution["run"], "execution": execution}
