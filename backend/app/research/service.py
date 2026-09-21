from __future__ import annotations

import json
from typing import Any

from .. import database as db
from .field_mappings import source_owns_field
from .models import CompletenessStatus, IdentityStatus, SourceResult, SourceResultStatus
from .persistence import add_audit_event, utcnow


PROPOSAL_ELIGIBLE_STATUSES = {
    SourceResultStatus.SUCCESS_COMPLETE,
    SourceResultStatus.SUCCESS_WITH_FINDINGS,
}


def create_tasks_for_run(research_run_id: int, bidder_ids: list[int], source_keys: list[str]) -> int:
    created = 0
    with db.connect() as conn:
        for bidder_id in bidder_ids:
            for source_key in source_keys:
                cur = conn.execute(
                    """
                    INSERT OR IGNORE INTO research_tasks(
                        research_run_id, bidder_id, source_key, status, created_at
                    ) VALUES (?, ?, ?, 'NOT_CHECKED', ?)
                    """,
                    (research_run_id, bidder_id, source_key, utcnow()),
                )
                created += cur.rowcount
        add_audit_event(
            conn,
            "research_run_tasks_created",
            f"Created {created} research tasks for run {research_run_id}.",
            research_run_id=research_run_id,
            details={"bidder_count": len(bidder_ids), "source_count": len(source_keys)},
        )
    return created


def list_tasks(research_run_id: int | None = None) -> list[dict[str, Any]]:
    with db.connect() as conn:
        if research_run_id is None:
            rows = conn.execute("SELECT * FROM research_tasks ORDER BY id DESC").fetchall()
        else:
            rows = conn.execute(
                "SELECT * FROM research_tasks WHERE research_run_id = ? ORDER BY id",
                (research_run_id,),
            ).fetchall()
        return [dict(row) for row in rows]


def _pending_proposals(conn, *, bidder_id: int, source_key: str, field_name: str) -> list[Any]:
    return conn.execute(
        """
        SELECT * FROM proposed_changes
        WHERE bidder_id=? AND source_key=? AND field_name=? AND status='pending'
        ORDER BY id
        """,
        (bidder_id, source_key, field_name),
    ).fetchall()


def _supersede_proposal(conn, proposal_id: int, *, now: str, note: str) -> None:
    conn.execute(
        """
        UPDATE proposed_changes
        SET status='superseded', reviewed_at=?, review_note=?
        WHERE id=? AND status='pending'
        """,
        (now, note, proposal_id),
    )


def persist_source_result(task_id: int, result: SourceResult) -> dict[str, Any]:
    with db.connect() as conn:
        task = conn.execute("SELECT * FROM research_tasks WHERE id = ?", (task_id,)).fetchone()
        if not task:
            raise ValueError(f"Research task {task_id} does not exist.")
        if int(task["bidder_id"]) != result.contractor_id:
            raise ValueError("Source result contractor does not match research task bidder.")
        if str(task["source_key"]) != result.source_key:
            raise ValueError("Source result source does not match research task source.")

        bidder_row = conn.execute("SELECT row_json FROM bidders WHERE id = ?", (result.contractor_id,)).fetchone()
        if not bidder_row:
            raise ValueError(f"Bidder {result.contractor_id} does not exist.")
        master = json.loads(bidder_row["row_json"])

        now = utcnow()
        conn.execute(
            """
            UPDATE research_tasks
            SET status = ?, attempt_count = attempt_count + 1,
                started_at = COALESCE(started_at, ?), completed_at = ?, last_error = NULL
            WHERE id = ?
            """,
            (result.status.value, now, now, task_id),
        )
        check = conn.execute(
            """
            INSERT INTO source_checks(
                research_task_id, checked_at, result_status, identity_status,
                completeness_status, identity_confidence, acquisition_method,
                http_status, adapter_version, parser_version, warnings_json
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                task_id,
                now,
                result.status.value,
                result.identity_status.value,
                result.completeness_status.value,
                result.identity_confidence,
                result.acquisition_method,
                result.http_status,
                result.adapter_version,
                result.parser_version,
                json.dumps(result.warnings, ensure_ascii=False),
            ),
        )
        source_check_id = int(check.lastrowid)

        primary_artifact = result.artifacts[0] if result.artifacts else None
        snapshot = conn.execute(
            """
            INSERT INTO evidence_snapshots(
                source_check_id, bidder_id, source_key, research_run_id, retrieved_at,
                searched_name, searched_address, source_record_id, source_url,
                acquisition_method, http_status, raw_artifact_path, raw_artifact_sha256,
                normalized_json, identity_confidence, identity_status, completeness_status,
                result_status, parser_version, adapter_version, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                source_check_id,
                result.contractor_id,
                result.source_key,
                int(task["research_run_id"]),
                now,
                result.searched_name,
                result.searched_address,
                result.source_record_id,
                result.source_url,
                result.acquisition_method,
                result.http_status,
                primary_artifact.relative_path if primary_artifact else None,
                primary_artifact.sha256 if primary_artifact else None,
                json.dumps(
                    {
                        "payload": result.normalized_payload,
                        "artifacts": [artifact.model_dump() for artifact in result.artifacts],
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                result.identity_confidence,
                result.identity_status.value,
                result.completeness_status.value,
                result.status.value,
                result.parser_version,
                result.adapter_version,
                now,
            ),
        )
        snapshot_id = int(snapshot.lastrowid)

        proposal_ids: list[int] = []
        reconfirmed_proposal_ids: list[int] = []
        superseded_proposal_ids: list[int] = []
        for evidence in result.evidence:
            conn.execute(
                """
                INSERT INTO evidence_records(
                    evidence_snapshot_id, field_name, observed_value, source_record_id,
                    source_url, details_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (
                    snapshot_id,
                    evidence.field_name,
                    evidence.observed_value,
                    evidence.source_record_id,
                    evidence.source_url,
                    json.dumps(evidence.details, ensure_ascii=False, sort_keys=True),
                ),
            )

            observed = "" if evidence.observed_value is None else str(evidence.observed_value).strip()
            current = "" if master.get(evidence.field_name) is None else str(master.get(evidence.field_name, "")).strip()
            comparable = (
                bool(observed)
                and source_owns_field(result.source_key, evidence.field_name)
                and result.status in PROPOSAL_ELIGIBLE_STATUSES
                and result.identity_status == IdentityStatus.CONFIRMED
                and result.completeness_status == CompletenessStatus.COMPLETE
            )
            if not comparable:
                continue

            pending = _pending_proposals(
                conn,
                bidder_id=result.contractor_id,
                source_key=result.source_key,
                field_name=evidence.field_name,
            )
            duplicate_pending_id: int | None = None
            for prior in pending:
                prior_id = int(prior["id"])
                prior_current = "" if prior["current_value"] is None else str(prior["current_value"]).strip()
                prior_proposed = "" if prior["proposed_value"] is None else str(prior["proposed_value"]).strip()
                if observed != current and prior_current == current and prior_proposed == observed:
                    duplicate_pending_id = prior_id
                    if prior_id not in reconfirmed_proposal_ids:
                        reconfirmed_proposal_ids.append(prior_id)
                    continue
                _supersede_proposal(
                    conn,
                    prior_id,
                    now=now,
                    note=f"Superseded by newer complete confirmed evidence snapshot {snapshot_id}.",
                )
                if prior_id not in superseded_proposal_ids:
                    superseded_proposal_ids.append(prior_id)

            if observed == current or duplicate_pending_id is not None:
                continue

            proposal = conn.execute(
                """
                INSERT INTO proposed_changes(
                    bidder_id, evidence_snapshot_id, source_key, field_name,
                    current_value, proposed_value, status, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, 'pending', ?)
                """,
                (
                    result.contractor_id,
                    snapshot_id,
                    result.source_key,
                    evidence.field_name,
                    current,
                    observed,
                    now,
                ),
            )
            proposal_ids.append(int(proposal.lastrowid))

        add_audit_event(
            conn,
            "evidence_snapshot_created",
            f"Stored evidence snapshot {snapshot_id} from {result.source_key}.",
            bidder_id=result.contractor_id,
            research_run_id=int(task["research_run_id"]),
            source_key=result.source_key,
            entity_type="evidence_snapshot",
            entity_id=snapshot_id,
            details={
                "result_status": result.status.value,
                "identity_status": result.identity_status.value,
                "completeness_status": result.completeness_status.value,
                "proposal_ids": proposal_ids,
                "reconfirmed_proposal_ids": reconfirmed_proposal_ids,
                "superseded_proposal_ids": superseded_proposal_ids,
            },
        )
        return {
            "source_check_id": source_check_id,
            "snapshot_id": snapshot_id,
            "proposal_ids": proposal_ids,
            "reconfirmed_proposal_ids": reconfirmed_proposal_ids,
            "superseded_proposal_ids": superseded_proposal_ids,
        }


def record_identity_judgment(
    *,
    bidder_id: int,
    source_key: str,
    source_record_id: str,
    judgment: str,
    confidence: float | None = None,
    decided_by: str | None = None,
    notes: str | None = None,
) -> int:
    normalized = judgment.upper()
    if normalized not in {"SAME_ENTITY", "DIFFERENT_ENTITY", "UNDECIDED"}:
        raise ValueError("Identity judgment must be SAME_ENTITY, DIFFERENT_ENTITY, or UNDECIDED.")
    with db.connect() as conn:
        conn.execute(
            """
            INSERT INTO identity_judgments(
                bidder_id, source_key, source_record_id, judgment, confidence,
                decided_by, decided_at, notes
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(bidder_id, source_key, source_record_id) DO UPDATE SET
                judgment=excluded.judgment,
                confidence=excluded.confidence,
                decided_by=excluded.decided_by,
                decided_at=excluded.decided_at,
                notes=excluded.notes
            """,
            (bidder_id, source_key, source_record_id, normalized, confidence, decided_by, utcnow(), notes),
        )
        row = conn.execute(
            "SELECT id FROM identity_judgments WHERE bidder_id=? AND source_key=? AND source_record_id=?",
            (bidder_id, source_key, source_record_id),
        ).fetchone()
        judgment_id = int(row["id"])
        add_audit_event(
            conn,
            "identity_judgment_recorded",
            f"Identity judgment recorded as {normalized}.",
            actor=decided_by,
            bidder_id=bidder_id,
            source_key=source_key,
            entity_type="identity_judgment",
            entity_id=judgment_id,
            details={"source_record_id": source_record_id, "confidence": confidence},
        )
        return judgment_id


def review_change(change_id: int, *, decision: str, actor: str | None = None, note: str | None = None) -> dict[str, Any]:
    decision = decision.lower()
    if decision not in {"approved", "dismissed"}:
        raise ValueError("Decision must be approved or dismissed.")

    with db.connect() as conn:
        change = conn.execute("SELECT * FROM proposed_changes WHERE id = ?", (change_id,)).fetchone()
        if not change:
            raise ValueError(f"Proposed change {change_id} does not exist.")
        if change["status"] != "pending":
            raise ValueError("Proposed change has already been reviewed.")

        now = utcnow()
        bidder = None
        row_data: dict[str, Any] | None = None
        if decision == "approved":
            bidder = conn.execute("SELECT row_json FROM bidders WHERE id = ?", (change["bidder_id"],)).fetchone()
            if not bidder:
                raise ValueError("Bidder no longer exists.")
            row_data = json.loads(bidder["row_json"])
            live_current = "" if row_data.get(change["field_name"]) is None else str(row_data.get(change["field_name"], "")).strip()
            expected_current = "" if change["current_value"] is None else str(change["current_value"]).strip()
            if live_current != expected_current:
                stale_note = (
                    f"Superseded because the approved master changed from the proposal baseline "
                    f"{expected_current!r} to {live_current!r} before approval."
                )
                conn.execute(
                    """
                    UPDATE proposed_changes
                    SET status='superseded', reviewed_at=?, reviewed_by=?, review_note=?
                    WHERE id=?
                    """,
                    (now, actor, stale_note, change_id),
                )
                add_audit_event(
                    conn,
                    "proposed_change_superseded",
                    f"Proposed change {change_id} was superseded because the master changed before approval.",
                    actor=actor,
                    bidder_id=int(change["bidder_id"]),
                    source_key=str(change["source_key"]),
                    entity_type="proposed_change",
                    entity_id=change_id,
                    details={
                        "expected_current_value": expected_current,
                        "live_current_value": live_current,
                        "requested_decision": decision,
                    },
                )
                return {"change_id": change_id, "decision": "superseded", "revision_id": None, "stale": True}

        conn.execute(
            """
            UPDATE proposed_changes
            SET status=?, reviewed_at=?, reviewed_by=?, review_note=?
            WHERE id=?
            """,
            (decision, now, actor, note, change_id),
        )

        revision_id = None
        if decision == "approved":
            assert row_data is not None
            row_data[change["field_name"]] = change["proposed_value"] or ""
            conn.execute(
                "UPDATE bidders SET row_json = ? WHERE id = ?",
                (json.dumps(row_data, ensure_ascii=False), change["bidder_id"]),
            )
            snapshot = conn.execute(
                "SELECT research_run_id FROM evidence_snapshots WHERE id = ?",
                (change["evidence_snapshot_id"],),
            ).fetchone()
            revision = conn.execute(
                """
                INSERT INTO master_revisions(
                    bidder_id, field_name, previous_value, new_value,
                    evidence_snapshot_id, source_key, research_run_id,
                    approved_by, approved_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    change["bidder_id"],
                    change["field_name"],
                    change["current_value"],
                    change["proposed_value"],
                    change["evidence_snapshot_id"],
                    change["source_key"],
                    int(snapshot["research_run_id"]) if snapshot else None,
                    actor,
                    now,
                ),
            )
            revision_id = int(revision.lastrowid)

        add_audit_event(
            conn,
            "proposed_change_reviewed",
            f"Proposed change {change_id} was {decision}.",
            actor=actor,
            bidder_id=int(change["bidder_id"]),
            source_key=str(change["source_key"]),
            entity_type="proposed_change",
            entity_id=change_id,
            details={"decision": decision, "revision_id": revision_id, "note": note},
        )
        return {"change_id": change_id, "decision": decision, "revision_id": revision_id, "stale": False}
