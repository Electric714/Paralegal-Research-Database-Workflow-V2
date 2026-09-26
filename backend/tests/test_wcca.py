from __future__ import annotations

import pytest

from app import database as db
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.service import list_tasks, persist_source_result
from app.research.source_registry import create_source
from app.research.sources.base import ContractorContext
from app.research.sources.wcca import WccaCcapSource, WccaOperatorAssistedSource, build_operator_result, build_search_plan
from app.research.sources.wcca_browser import WccaPublicBrowserSource


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
    assert "no-match is not proof" in plan["instructions"]
    assert plan["scope"] == "approved_bidder_and_explicit_related_company_aliases_only"


def test_registry_uses_live_public_browser_adapter_and_legacy_manual_adapter_still_resolves():
    source = create_source("wcca")
    assert isinstance(source, WccaPublicBrowserSource)
    assert WccaOperatorAssistedSource is WccaCcapSource
    health = source.health_check()
    assert health["status"] == "ready_public_browser"
    assert health["public_site_automation"] is True
    assert health["rest_api_used"] is False
    assert health["captcha_bypass"] is False


def test_legacy_manual_adapter_still_stops_for_operator_instead_of_scraping_public_wcca():
    result = WccaCcapSource().search(contractor())
    assert result.status == SourceResultStatus.MANUAL_REVIEW_REQUIRED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert result.acquisition_method == "operator_assisted_public_wcca"
    assert result.adapter_version == "1.0.0"
    assert result.parser_version == "operator-v3"
    assert result.normalized_payload["search_plan"]["search_names"][0] == "ACME ELECTRIC, LLC"
    assert result.normalized_payload["completion_path"] == "/wcca-workbench.html"


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
    assert not any(item.field_name == "circuit_court" for item in result.evidence)
    assert any("not confirmed as searched" in warning for warning in result.warnings)


def test_complete_public_no_match_never_becomes_circuit_court_n():
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
    assert not any(item.field_name == "circuit_court" for item in result.evidence)
    assert [(item.field_name, item.observed_value) for item in result.evidence] == [
        ("wcca_public_search", "NO_CURRENTLY_DISPLAYED_MATCH")
    ]
    assert result.evidence[0].details["does_not_establish_circuit_court_n"] is True
    assert result.normalized_payload["field_observation"] is None


def test_confirmed_case_is_complete_positive_and_keeps_case_level_evidence():
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
                "filing_date": "2026-01-15",
                "disposition": "Pending",
                "case_url": "https://wcca.wicourts.gov/caseDetail.html?caseNo=2026CV000123",
            }
        ],
    )
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.source_record_id == "2026CV000123"
    assert result.normalized_payload["field_observation"] == "Y"
    assert result.normalized_payload["integrity"]["matched_parties_within_planned_scope"] is True
    assert [(item.field_name, item.observed_value) for item in result.evidence] == [
        ("circuit_court", "Y"),
        ("wcca_case", "2026CV000123"),
    ]
    case_evidence = result.evidence[1]
    assert case_evidence.details["matched_party_name"] == "ACME ELECTRIC, LLC"
    assert case_evidence.details["filing_date"] == "2026-01-15"
    assert case_evidence.details["disposition"] == "Pending"
    assert case_evidence.details["matched_party_within_planned_scope"] is True


def test_non_official_case_url_is_discarded_but_identifiers_remain_usable():
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
                "matched_party_name": "ACME ELECTRIC LLC",
                "case_url": "https://example.com/not-wcca",
            }
        ],
    )
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.normalized_payload["cases"][0]["case_url"] == ""
    assert result.evidence[0].source_url == "https://wcca.wicourts.gov/index.xsl"
    assert any("Non-official WCCA case URL" in warning for warning in result.warnings)


def test_confirmed_positive_outside_approved_search_scope_requires_review():
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
                "case_number": "2026CV000777",
                "matched_party_name": "TOTALLY DIFFERENT COMPANY LLC",
            }
        ],
    )
    assert result.status == SourceResultStatus.MANUAL_REVIEW_REQUIRED
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.normalized_payload["field_observation"] is None
    assert not any(item.field_name == "circuit_court" for item in result.evidence)
    assert any("outside the approved bidder/related-company search scope" in warning for warning in result.warnings)


def test_conflicting_duplicate_case_number_requires_review():
    c = contractor()
    plan = build_search_plan(c)
    result = build_operator_result(
        c,
        searched_names=plan["search_names"],
        outcome="findings",
        operator_confirmed_complete=True,
        identity_confirmed=True,
        cases=[
            {"case_number": "2026CV000123", "matched_party_name": "ACME ELECTRIC, LLC"},
            {"case_number": "2026-CV-000123", "matched_party_name": "ACME SERVICES LLC"},
        ],
    )
    assert result.status == SourceResultStatus.MANUAL_REVIEW_REQUIRED
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.normalized_payload["integrity"]["case_records_consistent"] is False
    assert not any(item.field_name == "circuit_court" for item in result.evidence)
    assert any("Conflicting WCCA entries" in warning for warning in result.warnings)


def test_claimed_positive_requires_case_number_and_matched_party_name():
    c = contractor()
    plan = build_search_plan(c)
    result = build_operator_result(
        c,
        searched_names=plan["search_names"],
        outcome="findings",
        operator_confirmed_complete=True,
        identity_confirmed=True,
        cases=[{"county": "Dane", "matched_party_name": "ACME ELECTRIC, LLC"}],
    )
    assert result.status == SourceResultStatus.MANUAL_REVIEW_REQUIRED
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert not any(item.field_name == "circuit_court" for item in result.evidence)
    assert any("case number and matched party/business name" in warning for warning in result.warnings)


def test_confirmed_positive_can_be_retained_even_when_search_is_partial():
    c = contractor()
    result = build_operator_result(
        c,
        searched_names=["ACME ELECTRIC, LLC"],
        outcome="findings",
        operator_confirmed_complete=False,
        identity_confirmed=True,
        cases=[{"case_number": "2026CV000123", "matched_party_name": "ACME ELECTRIC, LLC"}],
    )
    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert any(item.field_name == "circuit_court" and item.observed_value == "Y" for item in result.evidence)


def test_unplanned_search_name_does_not_make_missing_alias_complete():
    c = contractor()
    result = build_operator_result(
        c,
        searched_names=["ACME ELECTRIC, LLC", "SOME RANDOM NAME"],
        outcome="no_match",
        operator_confirmed_complete=True,
    )
    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert "SOME RANDOM NAME" in result.normalized_payload["unexpected_search_names"]
    assert any("outside the approved bidder/related-company scope" in warning for warning in result.warnings)


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
            "SELECT field_name, observed_value FROM evidence_records WHERE evidence_snapshot_id=? ORDER BY id",
            (persisted["snapshot_id"],),
        ).fetchall()
    assert [(row["field_name"], row["observed_value"]) for row in evidence] == [
        ("circuit_court", "Y"),
        ("wcca_case", "2026CV000123"),
    ]
