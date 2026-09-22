from __future__ import annotations

import httpx

from app.research.field_mappings import source_owns_field
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.violation_tracker import (
    ViolationTrackerSource,
    parse_results_page,
)


NO_RESULTS_HTML = """
<html><body>
<div id="contentResult">
  <p>No Violation Tracker results found.</p>
</div>
<footer data-version="20260702"></footer>
</body></html>
"""


def result_html(
    *,
    company: str,
    parent: str,
    detail_slug: str = "wi-acme",
    offense: str = "environmental violation",
    year: str = "2025",
    agency: str = "WI-DNR",
    penalty: str = "$ 25,000",
    count: int = 1,
    pagination: str = "",
) -> str:
    return f"""
    <html><body>
    <h5>{count} Violation Tracker results found</h5>
    <table>
      <tr>
        <th>Company</th><th>Current Parent</th><th>Current Parent Industry</th>
        <th>Primary Offense Type</th><th>Year</th><th>Agency</th><th>Penalty Amount</th>
      </tr>
      <tr>
        <td><a href="/violation-tracker/{detail_slug}">{company}</a></td>
        <td><a href="/parent/acme-parent">{parent}</a></td>
        <td>construction and engineering</td>
        <td>{offense}</td><td>{year}</td><td>{agency}</td>
        <td><a href="/violation-tracker/{detail_slug}">{penalty}</a></td>
      </tr>
    </table>
    {pagination}
    <footer data-version="20260702"></footer>
    </body></html>
    """


def contractor(*, related: str = "") -> ContractorContext:
    return ContractorContext(
        internal_id=7,
        external_id="B-7",
        contractor_name="Acme Construction LLC",
        related_companies=related,
        address_1="100 Main St",
        city="Madison",
        state="WI",
        zip="53703",
    )


def test_parser_reads_public_results_table_and_record_links():
    parsed = parse_results_page(
        result_html(company="Acme Construction, LLC", parent="Acme Holdings")
    )

    assert parsed.table_found is True
    assert parsed.result_count == 1
    assert parsed.no_results is False
    assert parsed.data_version == "20260702"
    assert len(parsed.rows) == 1
    assert parsed.rows[0].company == "Acme Construction, LLC"
    assert parsed.rows[0].primary_offense == "environmental violation"
    assert parsed.rows[0].penalty_amount == 25000
    assert parsed.rows[0].detail_url.endswith("/violation-tracker/wi-acme")


def test_all_approved_names_can_cleanly_return_no_match():
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(request.url.params["company"])
        assert request.url.params["company_op"] == "="
        return httpx.Response(200, text=NO_RESULTS_HTML, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    source = ViolationTrackerSource(client=client)
    result = source.search(contractor(related="Acme Services LLC; Acme DBA LLC"))

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert result.evidence == []
    assert result.is_clean_negative is True
    assert requested == ["Acme Construction LLC", "Acme Services LLC", "Acme DBA LLC"]
    assert len(result.normalized_payload["approved_names_searched"]) == 3


def test_approved_alias_direct_match_is_a_confirmed_finding():
    def handler(request: httpx.Request) -> httpx.Response:
        name = request.url.params["company"]
        if name == "Acme Construction LLC":
            return httpx.Response(200, text=NO_RESULTS_HTML, request=request)
        assert name == "Acme Mechanical LLC"
        return httpx.Response(
            200,
            text=result_html(company="Acme Mechanical, LLC", parent="Acme Holdings"),
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(
        contractor(related="Acme Mechanical LLC")
    )

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].field_name == "violation_tracker"
    assert result.evidence[0].details["query_basis"] == "approved_alias"
    assert result.evidence[0].details["match_basis"] == "penalized_company"
    assert result.normalized_payload["classification"] == "MATCH"


def test_current_parent_only_results_are_ambiguous_not_bidder_violations():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=result_html(company="Acme Subsidiary LLC", parent="Acme Construction LLC"),
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(contractor())

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.normalized_payload["direct_record_count"] == 0
    assert result.normalized_payload["parent_only_record_count"] == 1
    assert result.evidence[0].observed_value == "Parent-only candidate records: 1"
    assert result.is_clean_negative is False


def test_unrelated_rows_fail_closed_when_exact_filter_appears_ignored():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            text=result_html(company="Completely Different Corp", parent="Different Holdings"),
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(contractor())

    assert result.status == SourceResultStatus.PARSER_FAILURE
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False
    assert any("filter may have been ignored" in warning for warning in result.warnings)


def test_one_successful_name_plus_one_failed_alias_is_partial_not_no_match():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.params["company"] == "Acme Construction LLC":
            return httpx.Response(200, text=NO_RESULTS_HTML, request=request)
        return httpx.Response(503, text="Service unavailable", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(
        contractor(related="Acme Alternate LLC")
    )

    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert result.normalized_payload["classification"] == "PARTIAL_OR_FAILED"


def test_rate_limit_is_blocked_not_no_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(contractor())

    assert result.status == SourceResultStatus.BLOCKED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_interactive_challenge_is_blocked_without_bypass_attempt():
    challenge = "<html><head><title>Just a moment...</title></head><body>Verify you are human</body></html>"

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=challenge, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(contractor())

    assert result.status == SourceResultStatus.BLOCKED
    assert result.is_clean_negative is False
    assert any("no bypass was attempted" in warning for warning in result.warnings)


def test_pagination_is_followed_before_declaring_complete_match():
    first_page = result_html(
        company="Acme Subsidiary LLC",
        parent="Acme Construction LLC",
        detail_slug="acme-subsidiary",
        count=2,
        pagination=(
            '<a href="/?company_op=%3D&amp;company=Acme+Construction+LLC&amp;page=2">2</a>'
        ),
    )
    second_page = result_html(
        company="Acme Construction LLC",
        parent="Acme Holdings",
        detail_slug="acme-direct",
        count=2,
    )
    seen_pages: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        seen_pages.append(page)
        return httpx.Response(
            200,
            text=first_page if page == "1" else second_page,
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(contractor())

    assert seen_pages == ["1", "2"]
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.normalized_payload["direct_record_count"] == 1
    assert result.normalized_payload["parent_only_record_count"] == 1
    assert result.normalized_payload["query_outcomes"][0]["pages_fetched"] == 2


def test_violation_tracker_remains_evidence_only_for_master_fields():
    assert source_owns_field("violation_tracker", "misc_violations") is False
    assert source_owns_field("violation_tracker", "environmental_violations") is False
    assert source_owns_field("violation_tracker", "prevailing_wage_violations") is False
    assert source_owns_field("violation_tracker", "violation_tracker") is False
