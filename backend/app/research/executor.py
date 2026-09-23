from __future__ import annotations

from collections import Counter
from typing import Any

from .. import database as db
from .models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from .run_control import cancellation_requested, finalize_cancel
from .service import list_tasks, persist_source_result
from .source_registry import create_source, implemented_source_keys
from .sources.base import ContractorContext


SOURCE_WIDE_FAILURE_STATUSES = frozenset(
    {
        SourceResultStatus.AUTH_REQUIRED,
        SourceResultStatus.BLOCKED,
        SourceResultStatus.SESSION_EXPIRED,
        SourceResultStatus.SOURCE_UNAVAILABLE,
        SourceResultStatus.DATASET_MALFORMED,
        SourceResultStatus.LAYOUT_CHANGED,
        SourceResultStatus.PARSER_FAILURE,
    }
)

DIAGNOSTIC_PROBLEM_STATUSES = frozenset(
    {
        SourceResultStatus.AUTH_REQUIRED,
        SourceResultStatus.BLOCKED,
        SourceResultStatus.HTTP_ERROR,
        SourceResultStatus.SOURCE_UNAVAILABLE,
        SourceResultStatus.DATASET_MALFORMED,
        SourceResultStatus.LAYOUT_CHANGED,
        SourceResultStatus.PARSER_FAILURE,
        SourceResultStatus.SESSION_EXPIRED,
        SourceResultStatus.TIMEOUT,
    }
)


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


def _mark_remaining_source_tasks(
    run_id: int,
    source_key: str,
    status: SourceResultStatus,
    warnings: list[str],
) -> int:
    reason = "; ".join(warnings[:3]) if warnings else status.value
    message = f"Not attempted because {source_key} was halted after a source-wide {status.value} result: {reason}"
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE research_tasks
            SET status=?, completed_at=?, last_error=?
            WHERE research_run_id=? AND source_key=? AND status=?
            """,
            (
                status.value,
                db.utcnow(),
                message,
                run_id,
                source_key,
                SourceResultStatus.NOT_CHECKED.value,
            ),
        )
        return int(cur.rowcount)


def _record_problem(
    summaries: dict[tuple[str, str], dict[str, Any]],
    *,
    source_key: str,
    status: SourceResultStatus,
    count: int = 1,
    bidder_name: str | None = None,
    warnings: list[str] | None = None,
    halted_count: int = 0,
) -> None:
    key = (source_key, status.value)
    item = summaries.setdefault(
        key,
        {
            "source_key": source_key,
            "status": status.value,
            "count": 0,
            "sample_bidders": [],
            "warnings": [],
            "halted_count": 0,
        },
    )
    item["count"] += count
    item["halted_count"] += halted_count
    if bidder_name and bidder_name not in item["sample_bidders"] and len(item["sample_bidders"]) < 3:
        item["sample_bidders"].append(bidder_name)
    for warning in warnings or []:
        if warning and warning not in item["warnings"] and len(item["warnings"]) < 5:
            item["warnings"].append(warning)


def _flush_problem_diagnostics(run_id: int, summaries: dict[tuple[str, str], dict[str, Any]]) -> None:
    for item in summaries.values():
        db.add_diagnostic(
            "WARNING",
            f"{item['source_key']} produced {item['status']} for {item['count']} task(s)",
            source_key=item["source_key"],
            stage="research",
            details={
                "run_id": run_id,
                "task_count": item["count"],
                "sample_bidders": item["sample_bidders"],
                "warnings": item["warnings"],
                "source_halted": bool(item["halted_count"]),
                "short_circuited_tasks": item["halted_count"],
            },
        )


def _cancel_execution(
    run_id: int,
    *,
    executed_count: int,
    skipped_unimplemented: int,
    already_processed: int,
    proposal_count: int,
    status_counts: Counter[str],
    short_circuited_count: int,
    problem_summaries: dict[tuple[str, str], dict[str, Any]],
) -> dict[str, Any]:
    remaining = sum(
        1
        for task in list_tasks(run_id)
        if str(task["status"]) == SourceResultStatus.NOT_CHECKED.value
    )
    _flush_problem_diagnostics(run_id, problem_summaries)
    run = finalize_cancel(run_id, remaining_tasks=remaining)
    return {
        "run": run,
        "executed": executed_count,
        "skipped": skipped_unimplemented,
        "already_processed": already_processed,
        "proposal_count": proposal_count,
        "status_counts": dict(status_counts),
        "short_circuited": short_circuited_count,
        "cancelled": True,
    }


def execute_research_run(run_id: int) -> dict[str, Any]:
    initial_run = db.get_run(run_id)
    initial_status = str(initial_run.get("status"))
    if initial_status == "cancelled":
        return {
            "run": initial_run,
            "executed": 0,
            "skipped": 0,
            "already_processed": 0,
            "proposal_count": 0,
            "status_counts": {},
            "short_circuited": 0,
            "cancelled": True,
        }
    if initial_status == "cancel_requested":
        return _cancel_execution(
            run_id,
            executed_count=0,
            skipped_unimplemented=0,
            already_processed=0,
            proposal_count=0,
            status_counts=Counter(),
            short_circuited_count=0,
            problem_summaries={},
        )

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
                "short_circuited": 0,
                "cancelled": False,
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
            "short_circuited": 0,
            "cancelled": False,
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
    executed_count = 0
    short_circuited_count = 0
    halted_sources: set[str] = set()
    problem_summaries: dict[tuple[str, str], dict[str, Any]] = {}

    for source_key in sorted({str(task["source_key"]) for task in executable}):
        if cancellation_requested(run_id):
            return _cancel_execution(
                run_id,
                executed_count=executed_count,
                skipped_unimplemented=len(skipped),
                already_processed=len(already_processed),
                proposal_count=proposal_count,
                status_counts=status_counts,
                short_circuited_count=short_circuited_count,
                problem_summaries=problem_summaries,
            )
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
        source_key = str(task["source_key"])
        if source_key in halted_sources:
            continue
        if cancellation_requested(run_id):
            return _cancel_execution(
                run_id,
                executed_count=executed_count,
                skipped_unimplemented=len(skipped),
                already_processed=len(already_processed),
                proposal_count=proposal_count,
                status_counts=status_counts,
                short_circuited_count=short_circuited_count,
                problem_summaries=problem_summaries,
            )

        task_id = int(task["id"])
        bidder_id = int(task["bidder_id"])
        contractor = _contractor_context(bidder_id)

        if source_key in prepare_errors:
            result = _unexpected_failure(task, contractor, prepare_errors[source_key], phase="prepare")
        else:
            adapter = adapters[source_key]
            try:
                result = adapter.search(contractor)
            except Exception as exc:  # source bugs must not abort an entire research run
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
        executed_count += 1
        status_counts[result.status.value] += 1
        proposal_count += len(persisted["proposal_ids"])

        if result.status in DIAGNOSTIC_PROBLEM_STATUSES:
            _record_problem(
                problem_summaries,
                source_key=source_key,
                status=result.status,
                bidder_name=contractor.contractor_name,
                warnings=result.warnings,
            )

        if cancellation_requested(run_id):
            return _cancel_execution(
                run_id,
                executed_count=executed_count,
                skipped_unimplemented=len(skipped),
                already_processed=len(already_processed),
                proposal_count=proposal_count,
                status_counts=status_counts,
                short_circuited_count=short_circuited_count,
                problem_summaries=problem_summaries,
            )

        if result.status in SOURCE_WIDE_FAILURE_STATUSES:
            halted = _mark_remaining_source_tasks(
                run_id,
                source_key,
                result.status,
                result.warnings,
            )
            if halted:
                halted_sources.add(source_key)
                short_circuited_count += halted
                status_counts[result.status.value] += halted
                _record_problem(
                    problem_summaries,
                    source_key=source_key,
                    status=result.status,
                    count=halted,
                    warnings=result.warnings,
                    halted_count=halted,
                )

    issue_statuses = {
        SourceResultStatus.AUTH_REQUIRED.value,
        SourceResultStatus.BLOCKED.value,
        SourceResultStatus.HTTP_ERROR.value,
        SourceResultStatus.SOURCE_UNAVAILABLE.value,
        SourceResultStatus.DATASET_MALFORMED.value,
        SourceResultStatus.LAYOUT_CHANGED.value,
        SourceResultStatus.PARSER_FAILURE.value,
        SourceResultStatus.SESSION_EXPIRED.value,
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
        f"Executed {executed_count} adapter request(s); {short_circuited_count} task(s) short-circuited after source-wide failures; "
        f"{proposal_count} proposed change(s); {len(skipped)} unimplemented task(s) skipped. {status_summary}"
    )
    _set_run_status(run_id, final_status, message, completed=True)
    _flush_problem_diagnostics(run_id, problem_summaries)
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
            "adapter_requests": executed_count,
            "short_circuited_tasks": short_circuited_count,
        },
    )
    return {
        "run": db.get_run(run_id),
        "executed": executed_count,
        "skipped": len(skipped),
        "already_processed": len(already_processed),
        "proposal_count": proposal_count,
        "status_counts": dict(status_counts),
        "short_circuited": short_circuited_count,
        "cancelled": False,
    }
