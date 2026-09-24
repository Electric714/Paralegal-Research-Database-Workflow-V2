from __future__ import annotations

import httpx

from app.research.field_mappings import source_owns_field
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.mn_pca import CSV_EXPORT_URL, MinnesotaPcaEnforcementSource, MpcaDatasetError, parse_mpca_csv


CSV = """Company or individual(s),Public date,Violation location,Violation description,Net penalty,Case type
Acme Construction LLC,03/05/2026,Minneapolis,Construction stormwater,$9663,Administrative penalty order
Other Builder Inc,01/10/2025,St Paul,Hazardous waste,$1200,Administrative penalty order
"""

CURRENT_HEADER_VARIANT_CSV = """Company or individual(s) (location),Public date,Violation location,Violation(s),Net penalty,Case type
Acme Construction LLC,03/05/2026,Minneapolis,Construction stormwater,$9663,Administrative penalty order
"""


def contractor(*, name: str = "Acme Construction, LLC", related: str = "") -> ContractorContext:
    return ContractorContext(internal_id=7, external_id="B-7", contractor_name=name, related_companies=related, city="Minneapolis", state="MN")


def source_for(body: str = CSV, status: int = 200, content_type: str = "text/csv") -> MinnesotaPcaEnforcementSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == CSV_EXPORT_URL
        return httpx.Response(status, text=body, headers={"content-type": content_type}, request=request)
    return MinnesotaPcaEnforcementSource(client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def test_parser_validates_and_reads_official_export_shape():
    records, digest = parse_mpca_csv(CSV)
    assert len(records) == 2
    assert records[0].party == "Acme Construction LLC"
    assert records[0].violation == "Construction stormwater"
    assert records[0].penalty == "$9663"
    assert len(digest) == 64


def test_parser_accepts_current_mpca_header_variants():
    records, digest = parse_mpca_csv(CURRENT_HEADER_VARIANT_CSV)
    assert len(records) == 1
    assert records[0].party == "Acme Construction LLC"
    assert records[0].violation == "Construction stormwater"
    assert records[0].penalty == "$9663"
    assert len(digest) == 64


def test_missing_required_columns_fails_closed():
    try:
        parse_mpca_csv("Name,Something\nAcme,Value\n")
    except MpcaDatasetError:
        pass
    else:
        raise AssertionError("expected MpcaDatasetError")


def test_exact_normalized_name_is_confirmed_and_proposes_environmental_violation():
    result = source_for().search(contractor())
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].field_name == "environmental_violations"
    assert result.evidence[0].observed_value == "Y"
    assert result.evidence[0].details["master_field_proposal_allowed"] is True
    assert result.normalized_payload["confirmed_record_count"] == 1


def test_approved_alias_can_confirm():
    result = source_for().search(contractor(name="Parent Holdings LLC", related="Acme Construction LLC"))
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.evidence[0].details["query_basis"] == "approved_alias"


def test_complete_dataset_no_match_is_clean_negative_but_never_proposes_n():
    result = source_for().search(contractor(name="No Such Contractor LLC"))
    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.is_clean_negative is True
    assert result.evidence == []
    assert "never proposes" in result.normalized_payload["negative_semantics"]


def test_typo_name_requires_review():
    body = CSV.replace("Acme Construction LLC", "Acme Constrction LLC")
    result = source_for(body).search(contractor())
    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.is_clean_negative is False
    assert result.evidence[0].details["master_field_proposal_allowed"] is False


def test_legal_name_expansion_requires_review_instead_of_clean_negative():
    body = CSV.replace("Acme Construction LLC", "Acme Construction Services LLC")
    result = source_for(body).search(contractor())
    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.is_clean_negative is False
    assert result.evidence[0].details["candidate_party"] == "Acme Construction Services LLC"
    assert result.evidence[0].details["master_field_proposal_allowed"] is False


def test_http_failure_cannot_become_negative():
    result = source_for("Service unavailable", status=503).search(contractor())
    assert result.status == SourceResultStatus.HTTP_ERROR
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_html_instead_of_csv_fails_closed():
    result = source_for("<!doctype html><html></html>", content_type="text/html").search(contractor())
    assert result.status == SourceResultStatus.PARSER_FAILURE
    assert result.is_clean_negative is False


def test_field_ownership_is_narrow():
    assert source_owns_field("mn_pca", "environmental_violations") is True
    assert source_owns_field("mn_pca", "misc_violations") is False
    assert source_owns_field("mn_pca", "prevailing_wage_violations") is False
