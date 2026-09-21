from __future__ import annotations

from datetime import date

import httpx

from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.osha import (
    OshaEstablishmentSource,
    inspection_date_windows,
    parse_inspection_detail,
    parse_search_page,
)


SEARCH_HTML = """
<html><body>
<h4>Results By Name</h4>
<p>Results 1 - 2 of 2</p>
<table>
  <tr>
    <th></th><th>#</th><th>Activity</th><th>Date Opened</th><th>RID</th>
    <th>ST</th><th>Type</th><th>Scope</th><th>SIC</th><th>NAICS</th>
    <th>Violations</th><th>Establishment Name</th>
  </tr>
  <tr>
    <td></td><td>1</td>
    <td><a href="/ords/imis/establishment.inspection_detail?id=990617.015">990617.015</a></td>
    <td>08/18/2014</td><td>0523400</td><td>WI</td><td>Fat/Cat</td><td>Partial</td>
    <td></td><td>238220</td><td></td><td>\"C\" Schlicht Plumbing, Inc.</td>
  </tr>
  <tr>
    <td></td><td>2</td>
    <td><a href="/ords/imis/establishment.inspection_detail?id=991849.015">991849.015</a></td>
    <td>09/05/2015</td><td>0523400</td><td>WI</td><td>FollowUp</td><td>Partial</td>
    <td></td><td>238220</td><td>2</td><td>\"C\" Schlicht Plumbing, Inc.</td>
  </tr>
</table>
</body></html>
"""

NO_RESULTS_HTML = """
<html><body>
<h4>Results By Name</h4>
<p>Results 0 - 0 of 0</p>
<table>
  <tr>
    <th></th><th>#</th><th>Activity</th><th>Date Opened</th><th>RID</th>
    <th>ST</th><th>Type</th><th>Scope</th><th>SIC</th><th>NAICS</th>
    <th>Violations</th><th>Establishment Name</th>
  </tr>
</table>
</body></html>
"""

DETAIL_HTML = """
<html><body>
<h3>Inspection: 990617.015 - \"C\" Schlicht Plumbing, Inc.</h3>
<div>Case Status: CLOSED</div>
<div>Site Address:<br>\"C\" Schlicht Plumbing, Inc.<br>820 N. Plankington Ave.<br>Milwaukee, WI 53203</div>
<div>Mailing Address:<br>2807 W Vliet St, Milwaukee, WI 53208</div>
<div>Union Status: NonUnion</div>
<div>NAICS: 238220</div>
</body></html>
"""


def contractor() -> ContractorContext:
    return ContractorContext(
        internal_id=1,
        external_id="1220",
        contractor_name='"C" SCHLICHT PLUMBING INC',
        address_1="2807 W Vliet St",
        city="Milwaukee",
        state="WI",
        zip="53208",
    )


def test_search_page_parser_reads_current_osha_result_table_with_st_header():
    parsed = parse_search_page(SEARCH_HTML)
    assert parsed.table_found is True
    assert parsed.complete is True
    assert parsed.total_results == 2
    assert len(parsed.rows) == 2
    assert parsed.rows[0].activity_number == "990617.015"
    assert parsed.rows[0].state == "WI"
    assert parsed.rows[0].establishment_name == '"C" Schlicht Plumbing, Inc.'
    assert parsed.rows[1].violations == "2"


def test_historical_windows_cover_old_inspections_without_exceeding_ten_years():
    reference = date(2026, 9, 21)
    windows = inspection_date_windows(reference)
    target = date(2014, 8, 18)

    assert windows[0][1] == reference
    assert windows[-1][0] == date(1972, 1, 1)
    assert any(start <= target <= end for start, end in windows)
    assert all(end.year - start.year <= 10 for start, end in windows)
    for current, older in zip(windows, windows[1:]):
        assert (current[0] - older[1]).days == 1


def test_inspection_detail_parser_keeps_address_evidence():
    detail = parse_inspection_detail(DETAIL_HTML)
    assert detail["case_status"] == "CLOSED"
    assert "820 N. Plankington Ave." in detail["site_address"]
    assert "2807 W Vliet St" in detail["mailing_address"]


def test_exact_normalized_osha_match_produces_y_evidence():
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("establishment.search"):
            assert request.url.params["state"] == "WI"
            assert request.url.params["p_case"] == "all"
            return httpx.Response(200, text=SEARCH_HTML, request=request)
        if request.url.path.endswith("establishment.inspection_detail"):
            return httpx.Response(200, text=DETAIL_HTML, request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    source = OshaEstablishmentSource(client=client, today=date(2026, 9, 21))
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].field_name == "osha"
    assert result.evidence[0].observed_value == "Y"
    assert result.source_record_id == "990617.015"
    assert result.normalized_payload["inspection_count"] == 2
    assert result.normalized_payload["inspection_years"] == ["2015", "2014"]
    assert result.normalized_payload["total_listed_violations"] == 2
    assert len(result.normalized_payload["inspection_details"]) == 2
    assert "osha_severe_violations" not in {item.field_name for item in result.evidence}
    assert "years" not in {item.field_name for item in result.evidence}


def test_complete_no_match_never_manufactures_an_n_evidence_record():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NO_RESULTS_HTML, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = OshaEstablishmentSource(client=client, today=date(2026, 9, 21))
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert result.evidence == []
    assert result.is_clean_negative is True


def test_osha_rate_limit_is_reported_as_blocked_not_no_match():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(429, text="Too Many Requests", request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = OshaEstablishmentSource(client=client, today=date(2026, 9, 21))
    result = source.search(contractor())

    assert result.status == SourceResultStatus.BLOCKED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.evidence == []
    assert result.is_clean_negative is False
