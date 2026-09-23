from __future__ import annotations

import json
from typing import Any

from .. import database as db
from .executor import execute_research_run
from .models import SourceResultStatus
from .service import record_identity_judgment


def _snapshot_payload(raw: str) -> dict[str, Any]:
    try:
        outer = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    payload = outer.get("payload", {})
    return payload if isinstance(payload, dict) else {}


def _candidate_ids(payload: dict[str, Any]) -> set[str]:
    candidates = payload.get("top_candidates", [])
    if not isinstance(candidates, list):
        return set()
    return {
        str(candidate.get("source_record_id") or "").strip()
        for candidate in candidates
        if isinstance(candidate, dict) and str(candidate.get("source_record_id") or "").strip()
    }


def list_identity_review_items(limit: int = 200) -> list[dict[str, Any]]:
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT es.*, b.contractor_name, sc.warnings_json, sc.research_task_id
            FROM evidence_snapshots es
            JOIN bidders b ON b.id = es.bidder_id
            JOIN source_checks sc ON sc.id = es.source_check_id
            WHERE es.identity_status = 'REVIEW_REQUIRED'
              AND b._active = 1
            ORDER BY es.id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()

        result: list[dict[str, Any]] = []
        for row in rows:
            payload = _snapshot_payload(str(row["normalized_json"]))
            candidates = payload.get("top_candidates", [])
            if not isinstance(candidates, list):
                continue

            # Evidence snapshots are immutable, but the active review queue must not
            # keep asking about candidates that a newer COMPLETE check no longer
            # considers plausible. This is especially important after matcher
            # hardening: old false-positive SAM candidates stay in evidence history,
            # while a later clean no-match removes them from the live review queue.
            newer_complete = conn.execute(
                """
                SELECT id, normalized_json
                FROM evidence_snapshots
                WHERE bidder_id=? AND source_key=?
                  AND completeness_status='COMPLETE' AND id>?
                ORDER BY id DESC
                LIMIT 1
                """,
                (int(row["bidder_id"]), str(row["source_key"]), int(row["id"])),
            ).fetchone()
            current_candidate_ids: set[str] | None = None
            if newer_complete:
                current_candidate_ids = _candidate_ids(
                    _snapshot_payload(str(newer_complete["normalized_json"]))
                )

            unresolved: list[dict[str, Any]] = []
            for candidate in candidates:
                if not isinstance(candidate, dict):
                    continue
                source_record_id = str(candidate.get("source_record_id") or "").strip()
                if not source_record_id:
                    continue
                if current_candidate_ids is not None and source_record_id not in current_candidate_ids:
                    continue
                judgment = conn.execute(
                    """
                    SELECT judgment, decided_by, decided_at, notes
                    FROM identity_judgments
                    WHERE bidder_id=? AND source_key=? AND source_record_id=?
                    """,
                    (int(row["bidder_id"]), str(row["source_key"]), source_record_id),
                ).fetchone()
                if judgment and str(judgment["judgment"]) != "UNDECIDED":
                    continue
                item = dict(candidate)
                item["judgment"] = dict(judgment) if judgment else None
                unresolved.append(item)

            if not unresolved:
                continue
            try:
                warnings = json.loads(str(row["warnings_json"] or "[]"))
            except json.JSONDecodeError:
                warnings = []
            result.append(
                {
                    "snapshot_id": int(row["id"]),
                    "research_run_id": int(row["research_run_id"]),
                    "research_task_id": int(row["research_task_id"]),
                    "bidder_id": int(row["bidder_id"]),
                    "contractor_name": str(row["contractor_name"]),
                    "source_key": str(row["source_key"]),
                    "retrieved_at": str(row["retrieved_at"]),
                    "result_status": str(row["result_status"]),
                    "completeness_status": str(row["completeness_status"]),
                    "warnings": warnings,
                    "candidates": unresolved,
                }
            )
        return result


def resolve_identity_review(
    *,
    snapshot_id: int,
    source_record_id: str,
    judgment: str,
    actor: str | None = None,
    note: str | None = None,
) -> dict[str, Any]:
    normalized = judgment.upper()
    if normalized not in {"SAME_ENTITY", "DIFFERENT_ENTITY", "UNDECIDED"}:
        raise ValueError("Judgment must be SAME_ENTITY, DIFFERENT_ENTITY, or UNDECIDED.")

    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT es.id, es.bidder_id, es.source_key, es.research_run_id,
                   sc.research_task_id, es.normalized_json
            FROM evidence_snapshots es
            JOIN source_checks sc ON sc.id = es.source_check_id
            WHERE es.id=? AND es.identity_status='REVIEW_REQUIRED'
            """,
            (snapshot_id,),
        ).fetchone()
        if not row:
            raise ValueError("Identity-review snapshot does not exist or no longer requires identity review.")
        payload = _snapshot_payload(str(row["normalized_json"]))
        candidates = payload.get("top_candidates", [])
        candidate_ids = {
            str(item.get("source_record_id") or "")
            for item in candidates
            if isinstance(item, dict)
        }
        if source_record_id not in candidate_ids:
            raise ValueError("The selected source record is not a candidate in this evidence snapshot.")
        bidder_id = int(row["bidder_id"])
        source_key = str(row["source_key"])
        run_id = int(row["research_run_id"])
        task_id = int(row["research_task_id"])

    judgment_id = record_identity_judgment(
        bidder_id=bidder_id,
        source_key=source_key,
        source_record_id=source_record_id,
        judgment=normalized,
        decided_by=actor,
        notes=note,
    )

    execution = None
    if normalized in {"SAME_ENTITY", "DIFFERENT_ENTITY"}:
        # The immutable ambiguous snapshot remains in history. Re-run the same task
        # so the new human identity judgment produces a new evidence snapshot under
        # the normal source pipeline instead of mutating the old evidence.
        with db.connect() as conn:
            conn.execute(
                """
                UPDATE research_tasks
                SET status=?, completed_at=NULL, last_error=NULL
                WHERE id=?
                """,
                (SourceResultStatus.NOT_CHECKED.value, task_id),
            )
            conn.execute(
                """
                UPDATE research_runs
                SET status='planned', completed_at=NULL,
                    message='Identity judgment recorded; affected source task queued for re-evaluation.'
                WHERE id=?
                """,
                (run_id,),
            )
        execution = execute_research_run(run_id)

    return {
        "judgment_id": judgment_id,
        "snapshot_id": snapshot_id,
        "source_record_id": source_record_id,
        "judgment": normalized,
        "execution": execution,
    }
