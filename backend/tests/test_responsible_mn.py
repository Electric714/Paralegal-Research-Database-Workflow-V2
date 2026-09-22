from __future__ import annotations

import httpx

from app.research.field_mappings import source_owns_field
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.responsible_mn import (
    RESPONSIBLE_MN_URL,
    ResponsibleMinnesotaError,
    ResponsibleMinnesotaSource,
    parse_responsible_mn_html,
)


HTML = """<!doctype html>
<html><body>
<table>
  <tr>
    <th>Contractor</th>
    <th>Statutory Provision Rendering Non-Responsible</th>
    <th>Relevant Public Documents/Links</th>
    <th>End Date</th>
  </tr>
  <tr>
    <td>Dionne Construction</td>
    <td>16C.285, subd. 3 (2)</td>
    <td><a href="https://mn.gov/example/dionne">Suspended/Debarred Vendors</a></td>
    <td>8/27/2099</td>
  </tr>
  <tr>
    <td>Stillwater Masonry Restoration Inc. and Todd Andrew Konigson, individually</td>
    <td>16C.285, subd. 3 (6)</td>
    <td><a href="/documents/stillwater.pdf">Public Record</a></td>
    <td>3/4/2099</td>
  </tr>
  <tr>
    <td>Old Contractor LLC</td>
    <td>16C.285, subd. 3 (6)</td>
    <td><a href="/documents/old.pdf">Old Record</a></td>
    <td>1/1/2000</td>
  </tr>
  <tr>
    <td>Frederick Leon Newell, individually</td>
    <td>16C.285, subd. 3 (2) (vii)</td>
    <td><a href="/documents/newell.pdf">Sentencing Order</a></td>
    <td></td>
  </tr>
</table>
</body></html>
"""


def contractor(*, name: str = "Dionne Construction LLC", related: str = "") -> ContractorContext:
    return ContractorContext(
        internal_id=11,
        external_id="B-11",
        contractor_name=name,
        related_companies=related,
        city="Minneapolis",
        state="MN",
    )


def source_for(
    body: str = HTML,
    *,
    status: int = 200,
    content_type: str = "text/html; charset=UTF-8",
) -> ResponsibleMinnesotaSource:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == RESPONSIBLE_MN_URL
        return httpx.Response(
            status,
            text=body,
            headers={"content-type": content_type},
            request=request,
        )

    return ResponsibleMinnesotaSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    )


def test_parser_reads_table_links_dates_and_combined_identity():
    records, digest = parse_responsible_mn_html(HTML)
    assert len(records) == 4
    assert records[0].raw_name == "Dionne Construction"
    assert records[0].statutory_provision == "16C.285, subd. 3 (2)"
    assert records[0].document_url == "https://mn.gov/example/dionne"
    assert records[0].end_date.isoformat() == "2099-08-27"
    assert records[1].match_names == (
        "Stillwater Masonry Restoration Inc.",
        "Todd Andrew Konigson",
    )
    assert records[3].match_names == ("Frederick Leon Newell",)
    assert records[3].end_date is None
    assert len(digest) == 64


def test_missing_expected_table_fails_closed():
    try:
        parse_responsible_mn_html("<html><body><table><tr><th>Name</th></tr></table></body></html>")
    except ResponsibleMinnesotaError as exc:
        assert exc.status == SourceResultStatus.LAYOUT_CHANGED
    else:
        raise AssertionError("expected ResponsibleMinnesotaError")


def test_exact_normalized_current_match_creates_comparison_evidence_only():
    result = source_for().search(contractor())
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].field_name == "mndol_ineligibility"
    assert result.evidence[0].observed_value == "Y"
    assert result.evidence[0].details["end_date_state"] == "CURRENT"
    assert result.evidence[0].details["master_field_proposal_allowed"] is False
    assert result.artifacts[0].sha256


def test_approved_related_company_can_confirm():
    result = source_for().search(
        contractor(name="Parent Holdings LLC", related="Dionne Construction LLC")
    )
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.evidence[0].details["query_basis"] == "approved_alias"


def test_combined_company_and_individual_row_matches_company_without_splitting_normal_and_names():
    result = source_for().search(contractor(name="Stillwater Masonry Restoration, Inc."))
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.evidence[0].details["matched_source_name"] == "Stillwater Masonry Restoration Inc."


def test_expired_listing_is_retained_but_does_not_assert_current_y():
    result = source_for().search(contractor(name="Old Contractor LLC"))
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.evidence[0].observed_value is None
    assert result.evidence[0].details["end_date_state"] == "EXPIRED"
    assert "historical evidence" in result.warnings[0]


def test_no_match_is_partial_and_never_a_clean_negative():
    result = source_for().search(contractor(name="No Such Contractor LLC"))
    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert result.evidence == []
    assert "never means" in result.normalized_payload["negative_semantics"]


def test_similar_name_requires_human_identity_review():
    result = source_for().search(contractor(name="Dionne Constrction LLC"))
    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence[0].observed_value is None
    assert result.evidence[0].details["master_field_proposal_allowed"] is False


def test_http_failure_cannot_become_negative():
    result = source_for("Service unavailable", status=503).search(contractor())
    assert result.status == SourceResultStatus.HTTP_ERROR
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_invalid_end_date_fails_closed():
    result = source_for(HTML.replace("8/27/2099", "not-a-date")).search(contractor())
    assert result.status == SourceResultStatus.DATASET_MALFORMED
    assert result.is_clean_negative is False


def test_responsible_mn_does_not_own_master_field_until_legacy_semantics_are_confirmed():
    assert source_owns_field("responsible_mn", "mndol_ineligibility") is False
    assert source_owns_field("responsible_mn", "state_federal_debarment") is False
