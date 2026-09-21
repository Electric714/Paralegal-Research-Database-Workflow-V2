from __future__ import annotations

from collections import Counter, defaultdict
from typing import Any

from .. import database as db
from .field_mappings import source_owns_field
from .models import SourceResultStatus


BLOCKED_STATUSES = {
    SourceResultStatus.BLOCKED.value,
    SourceResultStatus.AUTH_REQUIRED.value,
    SourceResultStatus.SESSION_EXPIRED.value,
}
PARTIAL_STATUSES = {
    SourceResultStatus.PARTIAL_RESULTS.value,
    SourceResultStatus.PAGINATION_INCOMPLETE.value,
}
FAILED_STATUSES = {
    SourceResultStatus.TIMEOUT.value,
    SourceResultStatus.HTTP_ERROR.value,
    SourceResultStatus.PARSER_FAILURE.value,
    SourceResultStatus.LAYOUT_CHANGED.value,
    SourceResultStatus.DATASET_MALFORMED.value,
    SourceResultStatus.SOURCE_UNAVAILABLE.value,
}
AMBIGUOUS_STATUSES = {
    SourceResultStatus.AMBIGUOUS_MATCH.value,
    SourceResultStatus.MANUAL_REVIEW_REQUIRED.value,
}
COMPLETE_STATUSES = {
    SourceResultStatus.SUCCESS_COMPLETE.value,
    SourceResultStatus.SUCCESS_NO_MATCH.value,
    SourceResultStatus.SUCCESS_WITH_FINDINGS.value,
}


def _bucket(status: str, identity_status: str | None, completeness_status: str | None) -> str:
    if status == SourceResultStatus.NOT_CHECKED.value:
        return "not_checked"
    if status in BLOCKED_STATUSES:
        return "blocked"
    if status in FAILED_STATUSES:
        return "failed"
    if status in PARTIAL_STATUSES or completeness_status == "PARTIAL":
        return "partial"
    if status in AMBIGUOUS_STATUSES or identity_status == "REVIEW_REQUIRED":
        return "ambiguous"
    if status == SourceResultStatus.SUCCESS_NO_MATCH.value and completeness_status == "COMPLETE":
        return "no_match"
    if status in COMPLETE_STATUSES and completeness_status == "COMPLETE":
        return "completed"
    return "failed"


def _empty_counts() -> dict[str, int]:
    return {
        "completed": 0,
        "no_match": 0,
        "ambiguous": 0,
        "partial": 0,
        "blocked": 0,
        "failed": 0,
        "not_checked": 0,
    }


def get_run_summary(run_id: int) -> dict[str, Any]:
    run = db.get_run(run_id)
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT rt.id AS task_id, rt.bidder_id, rt.source_key, rt.status,
                   rt.attempt_count, rt.started_at, rt.completed_at, rt.last_error,
                   b.external_id, b.contractor_name,
                   sc.identity_status, sc.completeness_status, sc.identity_confidence,
                   sc.checked_at, sc.warnings_json,
                   es.id AS snapshot_id, es.source_url, es.source_record_id,
                   es.retrieved_at
            FROM research_tasks rt
            JOIN bidders b ON b.id = rt.bidder_id
            LEFT JOIN source_checks sc ON sc.id = (
                SELECT sc2.id FROM source_checks sc2
                WHERE sc2.research_task_id = rt.id
                ORDER BY sc2.id DESC LIMIT 1
            )
            LEFT JOIN evidence_snapshots es ON es.source_check_id = sc.id
            WHERE rt.research_run_id = ?
            ORDER BY b.contractor_name, rt.source_key
            """,
            (run_id,),
        ).fetchall()
        proposal_rows = conn.execute(
            """
            SELECT pc.id, pc.bidder_id, pc.source_key, pc.field_name, pc.current_value,
                   pc.proposed_value, pc.status, pc.evidence_snapshot_id
            FROM proposed_changes pc
            JOIN evidence_snapshots es ON es.id = pc.evidence_snapshot_id
            WHERE es.research_run_id = ?
            ORDER BY pc.id
            """,
            (run_id,),
        ).fetchall()

    proposals_by_task_key: dict[tuple[int, str], list[dict[str, Any]]] = defaultdict(list)
    integrity_issues: list[str] = []
    for row in proposal_rows:
        item = dict(row)
        if not source_owns_field(str(item["source_key"]), str(item["field_name"])):
            integrity_issues.append(
                f"Proposal {item['id']} targets {item['field_name']}, which is not owned by {item['source_key']}."
            )
        proposals_by_task_key[(int(item["bidder_id"]), str(item["source_key"]))].append(item)

    counts = _empty_counts()
    source_counts: dict[str, dict[str, Any]] = {}
    bidder_rows: dict[int, dict[str, Any]] = {}
    raw_status_counts: Counter[str] = Counter()

    for row in rows:
        item = dict(row)
        status = str(item["status"])
        raw_status_counts[status] += 1
        bucket = _bucket(status, item.get("identity_status"), item.get("completeness_status"))
        counts[bucket] += 1

        source_key = str(item["source_key"])
        if source_key not in source_counts:
            source_counts[source_key] = {"source_key": source_key, "expected": 0, **_empty_counts(), "change_count": 0}
        source_counts[source_key]["expected"] += 1
        source_counts[source_key][bucket] += 1

        task_proposals = proposals_by_task_key.get((int(item["bidder_id"]), source_key), [])
        source_counts[source_key]["change_count"] += len(task_proposals)

        bidder_id = int(item["bidder_id"])
        bidder = bidder_rows.setdefault(
            bidder_id,
            {
                "bidder_id": bidder_id,
                "external_id": item.get("external_id"),
                "contractor_name": item["contractor_name"],
                "sources": [],
            },
        )
        bidder["sources"].append(
            {
                "task_id": int(item["task_id"]),
                "source_key": source_key,
                "status": status,
                "summary_status": bucket,
                "identity_status": item.get("identity_status"),
                "completeness_status": item.get("completeness_status"),
                "identity_confidence": item.get("identity_confidence"),
                "attempt_count": int(item["attempt_count"] or 0),
                "checked_at": item.get("checked_at"),
                "source_url": item.get("source_url"),
                "source_record_id": item.get("source_record_id"),
                "snapshot_id": item.get("snapshot_id"),
                "changes": task_proposals,
            }
        )

    expected = int(run["bidder_count"]) * int(run["source_count"])
    actual = len(rows)
    if actual != expected:
        integrity_issues.append(f"Run expects {expected} bidder-source tasks but {actual} are persisted.")
    accounted = sum(counts.values())
    if accounted != actual:
        integrity_issues.append(f"Summary accounts for {accounted} of {actual} persisted tasks.")

    safe_complete = counts["completed"] + counts["no_match"]
    attention = counts["ambiguous"] + counts["partial"] + counts["blocked"] + counts["failed"] + counts["not_checked"]
    pending_changes = sum(1 for row in proposal_rows if row["status"] == "pending")
    return {
        "run": run,
        "expected_tasks": expected,
        "persisted_tasks": actual,
        "accounted_tasks": accounted,
        "safe_complete_tasks": safe_complete,
        "attention_tasks": attention,
        "counts": counts,
        "raw_status_counts": dict(raw_status_counts),
        "change_count": len(proposal_rows),
        "pending_change_count": pending_changes,
        "integrity_ok": not integrity_issues,
        "integrity_issues": integrity_issues,
        "sources": [source_counts[key] for key in sorted(source_counts)],
        "bidders": list(bidder_rows.values()),
    }
