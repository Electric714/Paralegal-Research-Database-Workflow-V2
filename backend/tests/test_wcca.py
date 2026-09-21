from __future__ import annotations

import pytest

from app import database as db
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.service import list_tasks, persist_source_result
from app.research.sources.base import ContractorContext
from app.research.sources.wcca import WccaOperatorAssistedSource, build_operator_result, build_search_plan


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def contractor(**updates) -> ContractorContext:
    values = {
        "internal_id": 1,
        "external_id": "42",
        "contractor_name": "ACME ELECTRIC, LLC",
        "related_companies": "ACME SERVICES LLC; OLD ACME CO|ACME NORTH\nACME ELECTRIC, LLC",
        "address_1": "123 Main St",
        "city": "Madison",
        "state": "WI",
        "zip": "53703",
        "additional_address": "456 State St",
        "additional_address_city": "Madison",
        "additional_address_state": "WI",
        "additional_address_zip": "53703",
    }
    values.update(updates)
    return ContractorContext(**values)


def test_search_plan_preserves_commas_inside_legal_name_and_deduplicates_aliases():
    plan = build_search_plan(contractor())
    assert plan["search_names"] == [
        "ACME ELECTRIC, LLC",
        "ACME SERVICES LLC",
        "OLD ACME CO",
        "ACME NORTH",
    ]
    assert len(plan["locations"]) == 2


def test_adapter_stops_for_operator_instead_of_scraping_public_wcca():
    result = WccaOperatorAssistedSource().search(contractor())
    assert result.status == SourceResultStatus.MANUAL_REVIEW_REQUIRED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert result.acquisition_method == "operator_assisted_public_wcca"
    assert result.normalized_payload["search_plan"]["search_names"][0] == "ACME ELECTRIC, LLC"


def test_no_match_with_missing_alias_is_partial_not_clean_negative():
    result = build_operator_result(
        contractor(),
        searched_names=["ACME ELECTRIC, LLC", "ACME SERVICES LLC"],
        outcome="no_match",
        operator_confirmed_complete=True,
    )
    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert any("not confirmed as searched" in warning for warning in result.warnings)


def test_complete_no_match_is_clean_negative_but_only_comparison_evidence():
    c = contractor()
    plan = build_search_plan(c)
    result = build_operator_result(
        c,
        searched_names=plan["search_names"],
        outcome="no_match",
        operator_confirmed_complete=True,
    )
    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.is_clean_negative is True
    assert result.evidence[0].field_name == "circuit_court"
    assert result.evidence[0].observed_value == "N"
    assert result.evidence[0].details["comparison_only"] is True


def test_confirmed_case_is_complete_positive_when_all_aliases_checked():
    c = contractor()
    plan = build_search_plan(c)
    result = build_operator_result(
        c,
        searched_names=plan["search_names"],
        outcome="findings",
        operator_confirmed_complete=True,
        identity_confirmed=True,
        cases=[
            {
                "case_number": "2026CV000123",
                "county": "Dane",
                "matched_party_name": "ACME ELECTRIC, LLC",
                "case_type": "Civil",
                "case_status": "Open",
            }
        ],
    )
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.source_record_id == "2026CV000123"
    assert result.evidence[0].observed_value == "Y"


def test_wcca_evidence_cannot_create_master_proposal_until_semantics_confirmed(isolated_db):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "related_companies", "address_1", "city", "state", "zip", "circuit_court", "ccap_show150"],
        [{
            "id": "42",
            "contractor_name": "ACME ELECTRIC, LLC",
            "related_companies": "ACME SERVICES LLC",
            "address_1": "123 Main St",
            "city": "Madison",
            "state": "WI",
            "zip": "53703",
            "circuit_court": "N",
            "ccap_show150": "",
        }],
        str(isolated_db / "master.csv"),
    )
    bidder_id = db.active_bidder_ids()[0]
    bidder = db.get_bidder(bidder_id)
    c = ContractorContext(
        internal_id=bidder_id,
        external_id=str(bidder["id"]),
        contractor_name=str(bidder["contractor_name"]),
        related_companies=str(bidder["related_companies"]),
        address_1=str(bidder["address_1"]),
        city=str(bidder["city"]),
        state=str(bidder["state"]),
        zip=str(bidder["zip"]),
    )
    plan = build_search_plan(c)
    result = build_operator_result(
        c,
        searched_names=plan["search_names"],
        outcome="findings",
        operator_confirmed_complete=True,
        identity_confirmed=True,
        cases=[{"case_number": "2026CV000123", "county": "Dane", "matched_party_name": "ACME ELECTRIC, LLC"}],
    )
    run = db.create_run([bidder_id], ["wcca"], 1)
    task_id = list_tasks(run["id"])[0]["id"]
    persisted = persist_source_result(task_id, result)

    assert persisted["proposal_ids"] == []
    assert db.get_bidder(bidder_id)["circuit_court"] == "N"
    with db.connect() as conn:
        evidence = conn.execute(
            "SELECT field_name, observed_value FROM evidence_records WHERE evidence_snapshot_id=?",
            (persisted["snapshot_id"],),
        ).fetchall()
    assert [(row["field_name"], row["observed_value"]) for row in evidence] == [("circuit_court", "Y")]
