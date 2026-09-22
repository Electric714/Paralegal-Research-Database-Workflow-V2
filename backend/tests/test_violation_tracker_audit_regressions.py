from __future__ import annotations

import httpx

from app.research.models import CompletenessStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.violation_tracker import ViolationTrackerSource, _same_identity


def _contractor() -> ContractorContext:
    return ContractorContext(
        internal_id=91,
        external_id="B-91",
        contractor_name="Acme Construction LLC",
        address_1="100 Main St",
        city="Madison",
        state="WI",
        zip="53703",
    )


def _result_html(*, page_link: str = "") -> str:
    return f"""
    <html><body>
      <h5>2 Violation Tracker results found</h5>
      <table>
        <tr>
          <th>Company</th><th>Current Parent</th><th>Current Parent Industry</th>
          <th>Primary Offense Type</th><th>Year</th><th>Agency</th><th>Penalty Amount</th>
        </tr>
        <tr>
          <td><a href="/violation-tracker/wi-acme-duplicate">Acme Construction LLC</a></td>
          <td></td><td>construction</td><td>environmental violation</td>
          <td>2025</td><td>WI-DNR</td>
          <td><a href="/violation-tracker/wi-acme-duplicate">$25,000</a></td>
        </tr>
      </table>
      {page_link}
    </body></html>
    """


def test_internal_whitespace_is_not_removed_when_confirming_identity():
    # Removing every space can collapse genuinely different legal names.
    assert _same_identity("AB Construction LLC", "A B Construction LLC") is False


def test_repeated_pagination_rows_cannot_fake_complete_result_set():
    first_page = _result_html(
        page_link=(
            '<a href="/?company_op=%3D&amp;company=Acme+Construction+LLC&amp;page=2">2</a>'
        )
    )
    repeated_second_page = _result_html()

    def handler(request: httpx.Request) -> httpx.Response:
        page = request.url.params.get("page", "1")
        html = first_page if page == "1" else repeated_second_page
        return httpx.Response(200, text=html, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    result = ViolationTrackerSource(client=client).search(_contractor())

    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert result.normalized_payload["query_outcomes"][0]["error_status"] == "PAGINATION_INCOMPLETE"
    assert result.normalized_payload["query_outcomes"][0]["pages_fetched"] == 2


def test_source_remains_evidence_only_even_when_adapter_is_ready():
    source = ViolationTrackerSource()
    health = source.health_check()

    assert health["implemented"] is True
    assert health["api_key_required"] is False
    assert health["master_field_ownership"] is False
