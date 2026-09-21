from __future__ import annotations

from app import database as db
from app.research.models import CompletenessStatus, EvidenceRecord, IdentityStatus, SourceResult, SourceResultStatus
from app.research.run_summary import get_run_summary
from app.research.service import list_tasks, persist_source_result


def _import_bidders(tmp_path):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "osha"],
        [
            {"id": "1", "contractor_name": "Clean Co", "osha": "N"},
            {"id": "2", "contractor_name": "Changed Co", "osha": "N"},
            {"id": "3", "contractor_name": "Blocked Co", "osha": ""},
        ],
        str(tmp_path / "master.csv"),
    )


def test_run_summary_reconciles_persisted_tasks_and_changes(isolated_db):
    _import_bidders(isolated_db)
    run = db.create_run(None, ["osha"], 3)
    tasks = list_tasks(run["id"])

    persist_source_result(
        tasks[0]["id"],
        SourceResult(
            source_key="osha",
            contractor_id=tasks[0]["bidder_id"],
            status=SourceResultStatus.SUCCESS_NO_MATCH,
            identity_status=IdentityStatus.NOT_EVALUATED,
            completeness_status=CompletenessStatus.COMPLETE,
            searched_name="Clean Co",
        ),
    )
    persist_source_result(
        tasks[1]["id"],
        SourceResult(
            source_key="osha",
            contractor_id=tasks[1]["bidder_id"],
            status=SourceResultStatus.SUCCESS_WITH_FINDINGS,
            identity_status=IdentityStatus.CONFIRMED,
            completeness_status=CompletenessStatus.COMPLETE,
            searched_name="Changed Co",
            evidence=[EvidenceRecord(field_name="osha", observed_value="Y")],
        ),
    )
    persist_source_result(
        tasks[2]["id"],
        SourceResult(
            source_key="osha",
            contractor_id=tasks[2]["bidder_id"],
            status=SourceResultStatus.BLOCKED,
            identity_status=IdentityStatus.NOT_EVALUATED,
            completeness_status=CompletenessStatus.UNKNOWN,
            searched_name="Blocked Co",
        ),
    )

    summary = get_run_summary(run["id"])
    assert summary["expected_tasks"] == 3
    assert summary["persisted_tasks"] == 3
    assert summary["accounted_tasks"] == 3
    assert summary["counts"]["no_match"] == 1
    assert summary["counts"]["completed"] == 1
    assert summary["counts"]["blocked"] == 1
    assert summary["change_count"] == 1
    assert summary["pending_change_count"] == 1
    assert summary["integrity_ok"] is True


def test_incomplete_or_ambiguous_results_never_count_as_clean_negative(isolated_db):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "osha"],
        [
            {"id": "1", "contractor_name": "Partial Co", "osha": ""},
            {"id": "2", "contractor_name": "Ambiguous Co", "osha": ""},
        ],
        str(isolated_db / "master.csv"),
    )
    run = db.create_run(None, ["osha"], 2)
    tasks = list_tasks(run["id"])
    persist_source_result(
        tasks[0]["id"],
        SourceResult(
            source_key="osha",
            contractor_id=tasks[0]["bidder_id"],
            status=SourceResultStatus.PARTIAL_RESULTS,
            identity_status=IdentityStatus.NOT_EVALUATED,
            completeness_status=CompletenessStatus.PARTIAL,
            searched_name="Partial Co",
        ),
    )
    persist_source_result(
        tasks[1]["id"],
        SourceResult(
            source_key="osha",
            contractor_id=tasks[1]["bidder_id"],
            status=SourceResultStatus.AMBIGUOUS_MATCH,
            identity_status=IdentityStatus.REVIEW_REQUIRED,
            completeness_status=CompletenessStatus.PARTIAL,
            searched_name="Ambiguous Co",
        ),
    )
    summary = get_run_summary(run["id"])
    assert summary["counts"]["no_match"] == 0
    assert summary["safe_complete_tasks"] == 0
    assert summary["attention_tasks"] == 2
