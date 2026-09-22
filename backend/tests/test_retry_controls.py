from __future__ import annotations

import json

import pytest

from app import database as db
from app.research import retry_controls
from app.research.models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from app.research.service import list_tasks, persist_source_result, review_change
from app.research.sources.base import ContractorContext, ResearchSource


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "osha", "better_business_bureau_complaints"],
        [{
            "id": "1",
            "contractor_name": "Retry Electric LLC",
            "osha": "N",
            "better_business_bureau_complaints": "0",
        }],
        str(tmp_path / "master.csv"),
    )
    return db.active_bidder_ids()[0]


def result(
    bidder_id: int,
    source_key: str,
    status: SourceResultStatus,
    *,
    field_name: str | None = None,
    observed_value: str | None = None,
    complete: bool = True,
) -> SourceResult:
    evidence = []
    if field_name is not None:
        evidence.append(EvidenceRecord(field_name=field_name, observed_value=observed_value))
    return SourceResult(
        source_key=source_key,
        contractor_id=bidder_id,
        status=status,
        identity_status=IdentityStatus.CONFIRMED if complete else IdentityStatus.NOT_EVALUATED,
        completeness_status=CompletenessStatus.COMPLETE if complete else CompletenessStatus.PARTIAL,
        searched_name="Retry Electric LLC",
        evidence=evidence,
        acquisition_method="fixture",
    )


class SuccessfulOshaRetry(ResearchSource):
    source_key = "osha"
    display_name = "OSHA retry fixture"

    def search(self, contractor: ContractorContext) -> SourceResult:
        return result(
            contractor.internal_id,
            "osha",
            SourceResultStatus.SUCCESS_WITH_FINDINGS,
            field_name="osha",
            observed_value="Y",
        )


def test_retry_appends_attempt_and_preserves_prior_evidence(isolated_db, monkeypatch):
    bidder_id = isolated_db
    run = db.create_run([bidder_id], ["osha"], 1)
    task_id = list_tasks(run["id"])[0]["id"]
    first = persist_source_result(
        task_id,
        result(bidder_id, "osha", SourceResultStatus.TIMEOUT, complete=False),
    )

    monkeypatch.setattr(retry_controls, "implemented_source_keys", lambda: {"osha"})
    monkeypatch.setattr(retry_controls, "create_source", lambda source_key: SuccessfulOshaRetry())

    retried = retry_controls.retry_task(task_id, actor="tester")
    assert retried["previous_status"] == SourceResultStatus.TIMEOUT.value
    assert retried["result_status"] == SourceResultStatus.SUCCESS_WITH_FINDINGS.value
    assert retried["task"]["attempt_count"] == 2
    assert retried["snapshot_id"] != first["snapshot_id"]
    assert len(retried["proposal_ids"]) == 1
    assert db.get_bidder(bidder_id)["osha"] == "N"

    with db.connect() as conn:
        snapshots = conn.execute(
            "SELECT id, result_status FROM evidence_snapshots WHERE bidder_id=? AND source_key='osha' ORDER BY id",
            (bidder_id,),
        ).fetchall()
    assert [row["result_status"] for row in snapshots] == [
        SourceResultStatus.TIMEOUT.value,
        SourceResultStatus.SUCCESS_WITH_FINDINGS.value,
    ]

    with pytest.raises(ValueError, match="not retryable"):
        retry_controls.retry_task(task_id, actor="tester")


def test_repeated_same_finding_reconfirms_without_duplicate_proposal(isolated_db):
    bidder_id = isolated_db
    run1 = db.create_run([bidder_id], ["osha"], 1)
    task1 = list_tasks(run1["id"])[0]["id"]
    first = persist_source_result(
        task1,
        result(
            bidder_id,
            "osha",
            SourceResultStatus.SUCCESS_WITH_FINDINGS,
            field_name="osha",
            observed_value="Y",
        ),
    )
    assert len(first["proposal_ids"]) == 1

    run2 = db.create_run([bidder_id], ["osha"], 1)
    task2 = list_tasks(run2["id"])[0]["id"]
    second = persist_source_result(
        task2,
        result(
            bidder_id,
            "osha",
            SourceResultStatus.SUCCESS_WITH_FINDINGS,
            field_name="osha",
            observed_value="Y",
        ),
    )
    assert second["proposal_ids"] == []
    assert second["reconfirmed_proposal_ids"] == first["proposal_ids"]

    with db.connect() as conn:
        pending = conn.execute(
            "SELECT id FROM proposed_changes WHERE bidder_id=? AND source_key='osha' AND status='pending'",
            (bidder_id,),
        ).fetchall()
        snapshots = conn.execute(
            "SELECT id FROM evidence_snapshots WHERE bidder_id=? AND source_key='osha'",
            (bidder_id,),
        ).fetchall()
    assert len(pending) == 1
    assert len(snapshots) == 2


def test_new_complete_evidence_supersedes_conflicting_pending_proposal(isolated_db):
    bidder_id = isolated_db
    run1 = db.create_run([bidder_id], ["osha"], 1)
    task1 = list_tasks(run1["id"])[0]["id"]
    first = persist_source_result(
        task1,
        result(
            bidder_id,
            "osha",
            SourceResultStatus.SUCCESS_WITH_FINDINGS,
            field_name="osha",
            observed_value="Y",
        ),
    )
    proposal_id = first["proposal_ids"][0]

    run2 = db.create_run([bidder_id], ["osha"], 1)
    task2 = list_tasks(run2["id"])[0]["id"]
    second = persist_source_result(
        task2,
        result(
            bidder_id,
            "osha",
            SourceResultStatus.SUCCESS_WITH_FINDINGS,
            field_name="osha",
            observed_value="N",
        ),
    )
    assert proposal_id in second["superseded_proposal_ids"]
    assert second["proposal_ids"] == []

    with db.connect() as conn:
        proposal = conn.execute("SELECT status FROM proposed_changes WHERE id=?", (proposal_id,)).fetchone()
    assert proposal["status"] == "superseded"


def test_stale_approval_cannot_overwrite_changed_master(isolated_db):
    bidder_id = isolated_db
    run = db.create_run([bidder_id], ["osha"], 1)
    task_id = list_tasks(run["id"])[0]["id"]
    persisted = persist_source_result(
        task_id,
        result(
            bidder_id,
            "osha",
            SourceResultStatus.SUCCESS_WITH_FINDINGS,
            field_name="osha",
            observed_value="Y",
        ),
    )
    proposal_id = persisted["proposal_ids"][0]

    with db.connect() as conn:
        row = conn.execute("SELECT row_json FROM bidders WHERE id=?", (bidder_id,)).fetchone()
        payload = json.loads(row["row_json"])
        payload["osha"] = "CHANGED"
        conn.execute("UPDATE bidders SET row_json=? WHERE id=?", (json.dumps(payload), bidder_id))

    decision = review_change(proposal_id, decision="approved", actor="tester")
    assert decision["decision"] == "superseded"
    assert decision["stale"] is True
    assert decision["revision_id"] is None
    assert db.get_bidder(bidder_id)["osha"] == "CHANGED"


def test_retry_run_only_retries_transient_or_incomplete_tasks(isolated_db, monkeypatch):
    bidder_id = isolated_db
    run = db.create_run([bidder_id], ["osha", "bbb"], 1)
    tasks = {task["source_key"]: task for task in list_tasks(run["id"])}
    persist_source_result(
        tasks["osha"]["id"],
        result(bidder_id, "osha", SourceResultStatus.TIMEOUT, complete=False),
    )
    persist_source_result(
        tasks["bbb"]["id"],
        result(bidder_id, "bbb", SourceResultStatus.PARSER_FAILURE, complete=False),
    )

    monkeypatch.setattr(retry_controls, "implemented_source_keys", lambda: {"osha", "bbb"})
    monkeypatch.setattr(retry_controls, "create_source", lambda source_key: SuccessfulOshaRetry() if source_key == "osha" else None)

    outcome = retry_controls.retry_run_problems(run["id"], actor="tester")
    assert outcome["retried"] == 1
    updated = {task["source_key"]: task for task in list_tasks(run["id"])}
    assert updated["osha"]["attempt_count"] == 2
    assert updated["bbb"]["attempt_count"] == 1
    assert updated["bbb"]["status"] == SourceResultStatus.PARSER_FAILURE.value


def test_rerun_creates_new_run_using_current_selected_scope(isolated_db, monkeypatch):
    bidder_id = isolated_db
    original = db.create_run([bidder_id], ["osha"], 1)

    def fake_execute(run_id: int):
        return {
            "run": db.get_run(run_id),
            "executed": 0,
            "skipped": 0,
            "already_processed": 0,
            "proposal_count": 0,
            "status_counts": {},
        }

    monkeypatch.setattr(retry_controls, "execute_research_run", fake_execute)
    rerun = retry_controls.rerun_research_run(original["id"], actor="tester")
    assert rerun["rerun_of_run_id"] == original["id"]
    assert rerun["item"]["id"] != original["id"]
    assert rerun["item"]["bidder_scope"]["type"] == "selected"
    assert rerun["item"]["bidder_scope"]["bidder_ids"] == [bidder_id]
    new_tasks = list_tasks(rerun["item"]["id"])
    assert len(new_tasks) == 1
    assert new_tasks[0]["bidder_id"] == bidder_id
    assert new_tasks[0]["source_key"] == "osha"
