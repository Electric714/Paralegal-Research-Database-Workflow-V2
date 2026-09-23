from __future__ import annotations

from collections import Counter
from typing import Any

from .. import database as db
from .models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from .run_control import CANCELLED, PAUSED, PAUSE_REQUESTED, STOP_REQUESTED, requested_action
from .service import list_tasks, persist_source_result
from .source_registry import create_source, implemented_source_keys
from .sources.base import ContractorContext


# These outcomes describe a source/session/dataset problem rather than something
# specific to one bidder. Once one is observed, continuing to hit the same source
# for every remaining bidder only creates an error storm and can worsen rate limits.
SOURCE_WIDE_FAILURE_STATUSES = frozenset(
    {
        SourceResultStatus.AUTH_REQUIRED,
        SourceResultStatus.BLOCKED,
        SourceResultStatus.SESSION_EXPIRED,
        SourceResultStatus.SOURCE_UNAVAILABLE,
        SourceResultStatus.DATASET_MALFORMED,
        SourceResultStatus.LAYOUT_CHANGED,
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

TERMINAL_RUN_STATUSES = frozenset({"completed", "partial", CANCELLED})


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


def _execution_payload(
    run_id: int,
    *,
    status_counts: Counter[str] | None = None,
    skipped: int = 0,
    already_processed: int = 0,
    proposal_count: int = 0,
    short_circuited: int = 0,
) -> dict[str, Any]:
    counts = status_counts or Counter()
    return {
        "run": db.get_run(run_id),
        "executed": sum(counts.values()),
        "skipped": skipped,
        "already_processed": already_processed,
        "proposal_count": proposal_count,
        "status_counts": dict(counts),
        "short_circuited": short_circuited,
    }


def _note_unattempted_source_tasks(
    run_id: int,
    source_key: str,
    status: SourceResultStatus,
    warnings: list[str],
) -> int:
    """Annotate, but do not falsely complete, tasks skipped by the circuit breaker."""
    reason = "; ".join(warnings[:3]) if warnings else status.value
    message = (
        f"Not attempted because {source_key} was halted after a source-wide "
        f"{status.value} result: {reason}"
    )
    with db.connect() as conn:
        cur = conn.execute(
            """
            UPDATE research_tasks
            SET last_error=?
            WHERE research_run_id=? AND source_key=? AND status=?
            """,
            (message, run_id, source_key, SourceResultStatus.NOT_CHECKED.value),
        )
        return int(cur.rowcount)


def _record_problem(
    summaries: dict[tuple[str, str], dict[str, Any]],
    *,
    source_key: str,
    status: SourceResultStatus,
    bidder_name: str | None = None,
    warnings: list[str] | None = None,
    short_circuited: int = 0,
) -> None:
    key = (source_key, status.value)
    item = summaries.setdefault(
        key,
        {
            "source_key": source_key,
            "status": status.value,
            "observed_count": 0,
            "short_circuited": 0,
            "sample_bidders": [],
            "warnings": [],
        },
    )
    item["observed_count"] += 1
    item["short_circuited"] += short_circuited
    if bidder_name and bidder_name not in item["sample_bidders"] and len(item["sample_bidders"]) < 3:
        item["sample_bidders"].append(bidder_name)
    for warning in warnings or []:
        if warning and warning not in item["warnings"] and len(item["warnings"]) < 5:
            item["warnings"].append(warning)


def _add_short_circuit_count(
    summaries: dict[tuple[str, str], dict[str, Any]],
    *,
    source_key: str,
    status: SourceResultStatus,
    count: int,
) -> None:
    key = (source_key, status.value)
    item = summaries.get(key)
    if item is not None:
        item["short_circuited"] += count


def _flush_problem_diagnostics(run_id: int, summaries: dict[tuple[str, str], dict[str, Any]]) -> None:
    for item in summaries.values():
        if item["short_circuited"]:
            message = (
                f"{item['source_key']} halted after {item['status']}; "
                f"{item['short_circuited']} remaining task(s) were not attempted"
            )
        else:
            message = f"{item['source_key']} produced {item['status']} for {item['observed_count']} task(s)"
        db.add_diagnostic(
            "WARNING",
            message,
            source_key=item["source_key"],
            stage="research",
            details={
                "run_id": run_id,
                "status": item["status"],
                "observed_task_count": item["observed_count"],
                "short_circuited_tasks": item["short_circuited"],
                "sample_bidders": item["sample_bidders"],
                "warnings": item["warnings"],
            },
        )
    summaries.clear()


def _honor_control_request(
    run_id: int,
    *,
    status_counts: Counter[str],
    skipped: int,
    already_processed: int,
    proposal_count: int,
    short_circuited: int = 0,
    problem_summaries: dict[tuple[str, str], dict[str, Any]] | None = None,
) -> dict[str, Any] | None:
    action = requested_action(run_id)
    if action is None:
        return None

    if problem_summaries:
        _flush_problem_diagnostics(run_id, problem_summaries)

    executed = sum(status_counts.values())
    if action == PAUSE_REQUESTED:
        status = PAUSED
        completed = False
        message = (
            f"Research paused by the user after {executed} task(s) in this execution pass. "
            "Remaining unchecked tasks can be resumed later."
        )
        diagnostic_message = "Research run paused"
        severity = "INFO"
    elif action == STOP_REQUESTED:
        status = CANCELLED
        completed = True
        message = (
            f"Research stopped by the user after {executed} task(s) in this execution pass. "
            "Remaining unchecked tasks were not executed."
        )
        diagnostic_message = "Research run stopped"
        severity = "WARNING"
    else:
        return None

    _set_run_status(run_id, status, message, completed=completed)
    db.add_diagnostic(
        severity,
        diagnostic_message,
        stage="research",
        details={
            "run_id": run_id,
            "executed_this_pass": executed,
            "short_circuited_this_pass": short_circuited,
            "proposal_count": proposal_count,
            "skipped_unimplemented": skipped,
            "already_processed": already_processed,
        },
    )
    return _execution_payload(
        run_id,
        status_counts=status_counts,
        skipped=skipped,
        already_processed=already_processed,
        proposal_count=proposal_count,
        short_circuited=short_circuited,
    )


def execute_research_run(run_id: int) -> dict[str, Any]:
    run = db.get_run(run_id)

    # Completed/partial runs are terminal snapshots. Repeated calls to /execute must
    # not silently re-run a leftover task. Explicit retry, fresh rerun, identity
    # resolution, and resume flows all transition/create a non-terminal run first.
    if str(run["status"]) in TERMINAL_RUN_STATUSES or str(run["status"]) == PAUSED:
        return _execution_payload(run_id)

    tasks = list_tasks(run_id)
    pending = [task for task in tasks if str(task["status"]) == SourceResultStatus.NOT_CHECKED.value]
    executable = [task for task in pending if task["source_key"] in implemented_source_keys()]
    skipped = [task for task in pending if task["source_key"] not in implemented_source_keys()]
    already_processed = [task for task in tasks if str(task["status"]) != SourceResultStatus.NOT_CHECKED.value]

    status_counts: Counter[str] = Counter()
    proposal_count = 0
    short_circuited = 0
    problem_summaries: dict[tuple[str, str], dict[str, Any]] = {}

    controlled = _honor_control_request(
        run_id,
        status_counts=status_counts,
        skipped=len(skipped),
        already_processed=len(already_processed),
        proposal_count=proposal_count,
        short_circuited=short_circuited,
        problem_summaries=problem_summaries,
    )
    if controlled is not None:
        return controlled

    if not executable:
        if already_processed:
            return _execution_payload(
                run_id,
                skipped=len(skipped),
                already_processed=len(already_processed),
            )
        message = "No selected sources have implemented adapters yet."
        _set_run_status(run_id, "planned", message)
        return _execution_payload(run_id, skipped=len(skipped))

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

    for source_key in sorted({str(task["source_key"]) for task in executable}):
        controlled = _honor_control_request(
            run_id,
            status_counts=status_counts,
            skipped=len(skipped),
            already_processed=len(already_processed),
            proposal_count=proposal_count,
            short_circuited=short_circuited,
            problem_summaries=problem_summaries,
        )
        if controlled is not None:
            return controlled

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

        controlled = _honor_control_request(
            run_id,
            status_counts=status_counts,
            skipped=len(skipped),
            already_processed=len(already_processed),
            proposal_count=proposal_count,
            short_circuited=short_circuited,
            problem_summaries=problem_summaries,
        )
        if controlled is not None:
            return controlled

    halted_sources: set[str] = set()
    for task in executable:
        source_key = str(task["source_key"])
        if source_key in halted_sources:
            continue

        controlled = _honor_control_request(
            run_id,
            status_counts=status_counts,
            skipped=len(skipped),
            already_processed=len(already_processed),
            proposal_count=proposal_count,
            short_circuited=short_circuited,
            problem_summaries=problem_summaries,
        )
        if controlled is not None:
            return controlled

        task_id = int(task["id"])
        bidder_id = int(task["bidder_id"])
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

        # A preparation failure is necessarily source-wide. The explicit status set
        # below covers live source/session/dataset failures observed during search.
        source_wide_failure = source_key in prepare_errors or result.status in SOURCE_WIDE_FAILURE_STATUSES
        if source_wide_failure:
            unattempted = _note_unattempted_source_tasks(
                run_id,
                source_key,
                result.status,
                result.warnings,
            )
            if unattempted:
                halted_sources.add(source_key)
                short_circuited += unattempted
                _add_short_circuit_count(
                    problem_summaries,
                    source_key=source_key,
                    status=result.status,
                    count=unattempted,
                )

        # Pause/stop is cooperative: a source request already in flight is allowed to
        # return and persist its evidence, then the executor exits before starting the
        # next bidder/source task. This avoids corrupting immutable evidence snapshots.
        controlled = _honor_control_request(
            run_id,
            status_counts=status_counts,
            skipped=len(skipped),
            already_processed=len(already_processed),
            proposal_count=proposal_count,
            short_circuited=short_circuited,
            problem_summaries=problem_summaries,
        )
        if controlled is not None:
            return controlled

    controlled = _honor_control_request(
        run_id,
        status_counts=status_counts,
        skipped=len(skipped),
        already_processed=len(already_processed),
        proposal_count=proposal_count,
        short_circuited=short_circuited,
        problem_summaries=problem_summaries,
    )
    if controlled is not None:
        return controlled

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
        SourceResultStatus.SESSION_EXPIRED.value,
        SourceResultStatus.PAGINATION_INCOMPLETE.value,
    }

    # A run can span multiple execution passes after pause/resume. Determine the final
    # state from every persisted task, not only the tasks executed in this pass.
    final_tasks = list_tasks(run_id)
    cumulative_counts: Counter[str] = Counter(str(task["status"]) for task in final_tasks)
    cumulative_issue_count = sum(count for status, count in cumulative_counts.items() if status in issue_statuses)
    cumulative_pending = cumulative_counts.get(SourceResultStatus.NOT_CHECKED.value, 0)
    final_status = "partial" if cumulative_pending or cumulative_issue_count else "completed"

    status_summary = ", ".join(f"{key}={value}" for key, value in sorted(cumulative_counts.items())) or "no tasks"
    message = (
        f"Run has {len(final_tasks) - cumulative_pending}/{len(final_tasks)} task(s) processed; "
        f"{short_circuited} task(s) were not attempted after source-wide failures; "
        f"{proposal_count} proposed change(s) created in this pass. {status_summary}"
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
            "status_counts": dict(cumulative_counts),
            "executed_this_pass": sum(status_counts.values()),
            "short_circuited_this_pass": short_circuited,
            "proposal_count_this_pass": proposal_count,
            "skipped_unimplemented": len(skipped),
            "already_processed": len(already_processed),
            "adapter_requests": executed_count,
            "short_circuited_tasks": short_circuited_count,
        },
    )
    return _execution_payload(
        run_id,
        status_counts=status_counts,
        skipped=len(skipped),
        already_processed=len(already_processed),
        proposal_count=proposal_count,
        short_circuited=short_circuited,
    )
