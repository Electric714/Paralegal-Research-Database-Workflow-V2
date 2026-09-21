from __future__ import annotations

import json
from pathlib import Path

import pytest

from app import database as db
from app.import_validation import validate_bidder_rows
from app.research.matching import score_candidate
from app.research.models import (
    CompletenessStatus,
    EvidenceRecord,
    IdentityStatus,
    SourceResult,
    SourceResultStatus,
)
from app.research.service import list_tasks, persist_source_result, review_change
from app.research.sources.base import ContractorContext, ResearchSource


FIXTURE_DIR = Path(__file__).parent / "fixtures"


class FixtureOshaSource(ResearchSource):
    source_key = "osha"
    display_name = "OSHA fixture"

    def search(self, contractor: ContractorContext) -> SourceResult:
        payload = json.loads((FIXTURE_DIR / "mock_osha_record.json").read_text(encoding="utf-8"))
        return self.validate_result(
            SourceResult(
                source_key=self.source_key,
                contractor_id=contractor.internal_id,
                status=SourceResultStatus.SUCCESS_WITH_FINDINGS,
                identity_status=IdentityStatus.CONFIRMED,
                completeness_status=CompletenessStatus.COMPLETE,
                identity_confidence=0.99,
                searched_name=contractor.contractor_name,
                searched_address=contractor.address_1,
                source_record_id=payload["source_record_id"],
                acquisition_method="fixture",
                normalized_payload=payload,
                evidence=[EvidenceRecord(field_name="osha", observed_value=payload["osha_flag"])],
            )
        )


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def test_import_validation_rejects_duplicate_ids_and_bad_boolean():
    columns = ["id", "contractor_name", "state", "osha"]
    rows = [
        {"id": "1", "contractor_name": "Acme LLC", "state": "WI", "osha": "N"},
        {"id": "1", "contractor_name": "Acme 2 LLC", "state": "WIS", "osha": "MAYBE"},
    ]
    report = validate_bidder_rows(columns, rows)
    assert not report.valid
    assert any("duplicate id" in message for message in report.errors)
    assert any("two-letter state code" in message for message in report.errors)
    assert any("must be Y, N, or blank" in message for message in report.errors)


def test_candidate_matching_scores_obvious_same_company_high():
    score = score_candidate(
        master_name="ACME Electric LLC",
        candidate_name="ACME ELECTRIC, L.L.C.",
        master_address="123 Main St",
        candidate_address="123 MAIN STREET",
        master_city="Madison",
        candidate_city="MADISON",
        master_state="WI",
        candidate_state="WI",
    )
    assert score.status == "HIGH"
    assert score.score >= 0.92


def test_clean_negative_requires_complete_source_result():
    incomplete = SourceResult(
        source_key="osha",
        contractor_id=1,
        status=SourceResultStatus.SUCCESS_NO_MATCH,
        completeness_status=CompletenessStatus.PARTIAL,
        searched_name="Acme",
    )
    complete = incomplete.model_copy(update={"completeness_status": CompletenessStatus.COMPLETE})
    assert incomplete.is_clean_negative is False
    assert complete.is_clean_negative is True


def test_fixture_adapter_and_approval_pipeline(isolated_db):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "address_1", "city", "state", "osha"],
        [{
            "id": "1220",
            "contractor_name": "ACME Electric LLC",
            "address_1": "123 Main St",
            "city": "Madison",
            "state": "WI",
            "osha": "N",
        }],
        str(isolated_db / "master.csv"),
    )
    bidder_id = db.active_bidder_ids()[0]
    run = db.create_run(None, ["osha"], 1)
    tasks = list_tasks(run["id"])
    assert len(tasks) == 1
    assert tasks[0]["status"] == "NOT_CHECKED"

    bidder = db.get_bidder(bidder_id)
    source = FixtureOshaSource()
    result = source.search(
        ContractorContext(
            internal_id=bidder_id,
            external_id=str(bidder["id"]),
            contractor_name=str(bidder["contractor_name"]),
            address_1=str(bidder["address_1"]),
            city=str(bidder["city"]),
            state=str(bidder["state"]),
        )
    )
    persisted = persist_source_result(tasks[0]["id"], result)
    assert persisted["snapshot_id"] > 0
    assert len(persisted["proposal_ids"]) == 1

    # Evidence may propose a change, but the approved master is still untouched.
    assert db.get_bidder(bidder_id)["osha"] == "N"

    decision = review_change(persisted["proposal_ids"][0], decision="approved", actor="tester")
    assert decision["revision_id"] is not None
    assert db.get_bidder(bidder_id)["osha"] == "Y"

    with db.connect() as conn:
        revisions = conn.execute("SELECT * FROM master_revisions WHERE bidder_id = ?", (bidder_id,)).fetchall()
        audits = conn.execute("SELECT * FROM audit_events ORDER BY id").fetchall()
    assert len(revisions) == 1
    assert revisions[0]["previous_value"] == "N"
    assert revisions[0]["new_value"] == "Y"
    assert any(row["event_type"] == "evidence_snapshot_created" for row in audits)
    assert any(row["event_type"] == "proposed_change_reviewed" for row in audits)


def test_ambiguous_or_partial_evidence_never_auto_proposes(isolated_db):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "osha"],
        [{"id": "7", "contractor_name": "Similar Name LLC", "osha": "N"}],
        str(isolated_db / "master.csv"),
    )
    bidder_id = db.active_bidder_ids()[0]
    run = db.create_run(None, ["osha"], 1)
    task_id = list_tasks(run["id"])[0]["id"]

    result = SourceResult(
        source_key="osha",
        contractor_id=bidder_id,
        status=SourceResultStatus.AMBIGUOUS_MATCH,
        identity_status=IdentityStatus.REVIEW_REQUIRED,
        completeness_status=CompletenessStatus.PARTIAL,
        searched_name="Similar Name LLC",
        evidence=[EvidenceRecord(field_name="osha", observed_value="Y")],
    )
    persisted = persist_source_result(task_id, result)
    assert persisted["proposal_ids"] == []
    assert db.get_bidder(bidder_id)["osha"] == "N"


def test_reimport_keeps_historical_bidder_for_evidence(isolated_db):
    db.replace_master_database(
        "first.csv",
        ["id", "contractor_name", "osha"],
        [{"id": "1", "contractor_name": "Old Co", "osha": "N"}],
        str(isolated_db / "first.csv"),
    )
    old_id = db.active_bidder_ids()[0]
    run = db.create_run(None, ["osha"], 1)
    task_id = list_tasks(run["id"])[0]["id"]
    persist_source_result(
        task_id,
        SourceResult(
            source_key="osha",
            contractor_id=old_id,
            status=SourceResultStatus.SUCCESS_COMPLETE,
            identity_status=IdentityStatus.CONFIRMED,
            completeness_status=CompletenessStatus.COMPLETE,
            searched_name="Old Co",
        ),
    )

    db.replace_master_database(
        "second.csv",
        ["id", "contractor_name", "osha"],
        [{"id": "2", "contractor_name": "New Co", "osha": "N"}],
        str(isolated_db / "second.csv"),
    )

    assert db.count_bidders() == 1
    assert db.get_bidder(old_id)["_active"] is False
    with db.connect() as conn:
        evidence_count = conn.execute(
            "SELECT COUNT(*) FROM evidence_snapshots WHERE bidder_id = ?", (old_id,)
        ).fetchone()[0]
    assert evidence_count == 1
