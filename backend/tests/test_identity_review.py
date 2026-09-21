from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import pytest

from app import database as db
from app.research.executor import execute_research_run
from app.research.identity_review import list_identity_review_items, resolve_identity_review
from app.research.models import SourceResultStatus
from app.research.service import list_tasks
from app.research.sources.sam_exclusions import store_uploaded_extract


FIXTURE = Path(__file__).parent / "fixtures" / "sam_exclusions_v2_sample.csv"


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    backend_root = tmp_path / "backend"
    data_dir = backend_root / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "BASE_DIR", backend_root)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def _load_current_sam_extract():
    today = datetime.now(timezone.utc).date()
    filename = "SAM_Exclusions_Public_Extract_V2_" + today.strftime("%y%j") + ".CSV"
    return store_uploaded_extract(
        FIXTURE.read_bytes(),
        filename,
        cache_dir=db.DATA_DIR / "source_cache" / "sam",
    )


def test_ambiguous_sam_match_can_be_resolved_without_mutating_old_evidence(isolated_db):
    _load_current_sam_extract()
    db.replace_master_database(
        "master.csv",
        [
            "id",
            "contractor_name",
            "related_companies",
            "address_1",
            "city",
            "state",
            "zip",
            "state_federal_debarment",
        ],
        [{
            "id": "7",
            "contractor_name": "Common Builders LLC",
            "related_companies": "",
            "address_1": "",
            "city": "Madison",
            "state": "WI",
            "zip": "",
            "state_federal_debarment": "N",
        }],
        str(isolated_db / "master.csv"),
    )

    run = db.create_run(None, ["sam"], 1)
    first = execute_research_run(run["id"])
    assert first["executed"] == 1
    assert first["status_counts"] == {SourceResultStatus.AMBIGUOUS_MATCH.value: 1}
    assert db.list_review_proposals() == []

    reviews = list_identity_review_items()
    assert len(reviews) == 1
    review = reviews[0]
    assert review["contractor_name"] == "Common Builders LLC"
    assert {candidate["source_record_id"] for candidate in review["candidates"]} >= {
        "100000002",
        "100000003",
    }

    ambiguous_snapshot_id = review["snapshot_id"]
    with db.connect() as conn:
        before = dict(conn.execute(
            "SELECT * FROM evidence_snapshots WHERE id=?", (ambiguous_snapshot_id,)
        ).fetchone())

    resolved = resolve_identity_review(
        snapshot_id=ambiguous_snapshot_id,
        source_record_id="100000002",
        judgment="SAME_ENTITY",
        actor="tester",
        note="Confirmed against contractor identity.",
    )
    assert resolved["judgment"] == "SAME_ENTITY"
    assert resolved["execution"]["executed"] == 1
    assert resolved["execution"]["proposal_count"] == 1

    proposals = db.list_review_proposals()
    assert len(proposals) == 1
    assert proposals[0]["field_name"] == "state_federal_debarment"
    assert proposals[0]["proposed_value"] == "Y"

    # The original ambiguous evidence remains immutable and unchanged. Resolution
    # produces a new evidence snapshot under the normal research pipeline.
    with db.connect() as conn:
        after = dict(conn.execute(
            "SELECT * FROM evidence_snapshots WHERE id=?", (ambiguous_snapshot_id,)
        ).fetchone())
        snapshots = conn.execute(
            "SELECT id, identity_status, result_status FROM evidence_snapshots ORDER BY id"
        ).fetchall()
        judgment = conn.execute(
            """
            SELECT judgment FROM identity_judgments
            WHERE bidder_id=? AND source_key='sam' AND source_record_id='100000002'
            """,
            (db.active_bidder_ids()[0],),
        ).fetchone()

    assert before == after
    assert len(snapshots) == 2
    assert snapshots[0]["identity_status"] == "REVIEW_REQUIRED"
    assert snapshots[1]["identity_status"] == "CONFIRMED"
    assert judgment["judgment"] == "SAME_ENTITY"

    # Confirming one SAM record does not silently reject other plausible records.
    # The chosen record disappears from identity review; any other unresolved record
    # remains visible for an explicit human decision.
    remaining = list_identity_review_items()
    remaining_ids = {
        candidate["source_record_id"]
        for item in remaining
        for candidate in item["candidates"]
    }
    assert "100000002" not in remaining_ids
    assert "100000003" in remaining_ids

    tasks = list_tasks(run["id"])
    assert tasks[0]["status"] == SourceResultStatus.SUCCESS_WITH_FINDINGS.value


def test_rejected_sam_candidate_is_remembered_and_not_reasked(isolated_db):
    _load_current_sam_extract()
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "city", "state", "state_federal_debarment"],
        [{
            "id": "7",
            "contractor_name": "Common Builders LLC",
            "city": "Madison",
            "state": "WI",
            "state_federal_debarment": "N",
        }],
        str(isolated_db / "master.csv"),
    )

    run = db.create_run(None, ["sam"], 1)
    execute_research_run(run["id"])
    review = list_identity_review_items()[0]

    result = resolve_identity_review(
        snapshot_id=review["snapshot_id"],
        source_record_id="100000002",
        judgment="DIFFERENT_ENTITY",
        actor="tester",
    )
    assert result["judgment"] == "DIFFERENT_ENTITY"

    with db.connect() as conn:
        judgment = conn.execute(
            """
            SELECT judgment FROM identity_judgments
            WHERE bidder_id=? AND source_key='sam' AND source_record_id='100000002'
            """,
            (db.active_bidder_ids()[0],),
        ).fetchone()
    assert judgment["judgment"] == "DIFFERENT_ENTITY"

    remaining = list_identity_review_items()
    if remaining:
        remaining_ids = {
            candidate["source_record_id"]
            for item in remaining
            for candidate in item["candidates"]
        }
        assert "100000002" not in remaining_ids
