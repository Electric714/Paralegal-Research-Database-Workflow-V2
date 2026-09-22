from __future__ import annotations

from collections import Counter
from typing import Any

from .. import database as db
from .models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from .service import list_tasks, persist_source_result
from .source_registry import create_source, implemented_source_keys
from .sources.base import ContractorContext


def _contractor_context(bidder_id: int) -> ContractorContext:
    bidder = db.get_bidder(bidder_id)
    if not bidder:
        raise ValueError(f"Bidder {bidder_id} does not exist.")
    return ContractorContext(
        internal_id=bidder_id,
        external_id=str(bidder.get("id", "")),
        contractor_name=str(bidder.get("contractor_name", "")),
        related_companies=str(bidder.get("related_companies", "")),
        address_1=str(bidder.get("address_1", "")),
        city=str(bidder.get("city", "")),
        state=str(bidder.get("state", "")),
        zip=str(bidder.get("zip", "")),
        additional_address=str(bidder.get("additional_address", "")),
        additional_address_city=str(bidder.get("additional_address_city", "")),
        additional_address_state=str(bidder.get("additional_address_state", "")),
        additional_address_zip=str(bidder.get("additional_address_zip", "")),
        dfi=str(bidder.get("dfi", "")),
    )


def _unexpected_failure(
    task: dict[str, Any],
    contractor: ContractorContext,
    exc: Exception,
    *,
    phase: str = "execution",
) -> SourceResult:
    return SourceResult(
        source_key=str(task["source_key"]),
        contractor_id=int(task["bidder_id"]),
        status=SourceResultStatus.PARSER_FAILURE,
        identity_status=IdentityStatus.NOT_EVALUATED,
        completeness_status=CompletenessStatus.UNKNOWN,
        searched_name=contractor.contractor_name,
        searched_address=contractor.address_1,
        warnings=[f"Unexpected adapter {phase} failure: {type(exc).__name__}: {exc}"],
        acquisition_method=f"adapter_{phase}_error",
    )


def _set_run_status(run_id: int, status: str, message: str, *, completed: bool = False) -> None:
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE research_runs
            SET status=?, message=?, completed_at=CASE WHEN ? THEN ? ELSE completed_at END
            WHERE id=?
            """,
            (status, message, 1 if completed else 0, db.utcnow(), run_id),
        )


def execute_research_run(run_id: int) -> dict[str, Any]:
    db.get_run(run_id)
    tasks = list_tasks(run_id)
    pending = [task for task in tasks if str(task["status"]) == SourceResultStatus.NOT_CHECKED.value]
    executable = [task for task in pending if task["source_key"] in implemented_source_keys()]
    skipped = [task for task in pending if task["source_key"] not in implemented_source_keys()]
    already_processed = [task for task in tasks if str(task["status"]) != SourceResultStatus.NOT_CHECKED.value]

    if not executable:
        if already_processed:
            run = db.get_run(run_id)
            return {
                "run": run,
                "executed": 0,
                "skipped": len(skipped),
                "already_processed": len(already_processed),
                "proposal_count": 0,
                "status_counts": {},
            }
        message = "No selected sources have implemented adapters yet."
        _set_run_status(run_id, "planned", message)
        return {
            "run": db.get_run(run_id),
            "executed": 0,
            "skipped": len(skipped),
            "already_processed": 0,
            "proposal_count": 0,
            "status_counts": {},
        }

    _set_run_status(run_id, "running", f"Running {len(executable)} implemented research tasks.")
    db.add_diagnostic(
        "INFO",
        "Research run execution started",
        stage="research",
        details={
            "run_id": run_id,
            "task_count": len(executable),
            "skipped_unimplemented": len(skipped),
            "already_processed": len(already_processed),
        },
    )

    adapters: dict[str, Any] = {}
    prepare_errors: dict[str, Exception] = {}
    status_counts: Counter[str] = Counter()
    proposal_count = 0

    for source_key in sorted({str(task["source_key"]) for task in executable}):
        try:
            adapter = create_source(source_key)
            if adapter is None:
                raise RuntimeError(f"Registered source {source_key!r} could not be created.")
            adapter.prepare()
            adapters[source_key] = adapter
        except Exception as exc:
            prepare_errors[source_key] = exc
            db.add_diagnostic(
                "ERROR",
                "Source adapter preparation failed unexpectedly",
                source_key=source_key,
                stage="research",
                details={"run_id": run_id, "error": repr(exc)},
            )

    for task in executable:
        task_id = int(task["id"])
        bidder_id = int(task["bidder_id"])
        source_key = str(task["source_key"])
        contractor = _contractor_context(bidder_id)

        if source_key in prepare_errors:
            result = _unexpected_failure(task, contractor, prepare_errors[source_key], phase="prepare")
        else:
            adapter = adapters[source_key]
            try:
                result = adapter.search(contractor)
            except Exception as exc:
                result = _unexpected_failure(task, contractor, exc)
                db.add_diagnostic(
                    "ERROR",
                    "Source adapter failed unexpectedly",
                    source_key=source_key,
                    bidder_name=contractor.contractor_name,
                    stage="research",
                    details={"run_id": run_id, "task_id": task_id, "error": repr(exc)},
                )

        persisted = persist_source_result(task_id, result)
        status_counts[result.status.value] += 1
        proposal_count += len(persisted["proposal_ids"])

        if result.status in {
            SourceResultStatus.AUTH_REQUIRED,
            SourceResultStatus.BLOCKED,
            SourceResultStatus.HTTP_ERROR,
            SourceResultStatus.SOURCE_UNAVAILABLE,
            SourceResultStatus.DATASET_MALFORMED,
            SourceResultStatus.LAYOUT_CHANGED,
            SourceResultStatus.PARSER_FAILURE,
            SourceResultStatus.TIMEOUT,
        }:
            db.add_diagnostic(
                "WARNING",
                f"{source_key} task completed with {result.status.value}",
                source_key=source_key,
                bidder_name=contractor.contractor_name,
                stage="research",
                details={
                    "run_id": run_id,
                    "task_id": task_id,
                    "warnings": result.warnings,
                },
            )

    issue_statuses = {
        SourceResultStatus.AUTH_REQUIRED.value,
        SourceResultStatus.BLOCKED.value,
        SourceResultStatus.HTTP_ERROR.value,
        SourceResultStatus.SOURCE_UNAVAILABLE.value,
        SourceResultStatus.DATASET_MALFORMED.value,
        SourceResultStatus.LAYOUT_CHANGED.value,
        SourceResultStatus.PARSER_FAILURE.value,
        SourceResultStatus.TIMEOUT.value,
        SourceResultStatus.PARTIAL_RESULTS.value,
        SourceResultStatus.AMBIGUOUS_MATCH.value,
        SourceResultStatus.MANUAL_REVIEW_REQUIRED.value,
    }
    issue_count = sum(count for status, count in status_counts.items() if status in issue_statuses)
    if skipped or issue_count:
        final_status = "partial"
    else:
        final_status = "completed"

    status_summary = ", ".join(f"{key}={value}" for key, value in sorted(status_counts.items())) or "no executed tasks"
    message = (
        f"Executed {sum(status_counts.values())} task(s); {proposal_count} proposed change(s); "
        f"{len(skipped)} unimplemented task(s) skipped. {status_summary}"
    )
    _set_run_status(run_id, final_status, message, completed=True)
    db.add_diagnostic(
        "INFO" if final_status == "completed" else "WARNING",
        "Research run execution finished",
        stage="research",
        details={
            "run_id": run_id,
            "status": final_status,
            "status_counts": dict(status_counts),
            "proposal_count": proposal_count,
            "skipped_unimplemented": len(skipped),
            "already_processed": len(already_processed),
        },
    )
    return {
        "run": db.get_run(run_id),
        "executed": sum(status_counts.values()),
        "skipped": len(skipped),
        "already_processed": len(already_processed),
        "proposal_count": proposal_count,
        "status_counts": dict(status_counts),
    }
