from __future__ import annotations

import httpx
import pytest

from app import database as db
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.source_registry import implemented_source_keys
from app.research.sources.base import ContractorContext
from app.research.sources.wdfi import WdfiCorporateRecordsSource, parse_search_results
from app.sources import SOURCES


SEARCH_TEMPLATE = """
<html><body>
<div>Corporate Records | 1 record for advanced search.</div>
<table>
<tr><th>ID</th><th>Entity Name / Type</th><th>Registered Effective Date</th><th>Status / Status Date</th></tr>
<tr>
<td>A123456</td>
<td><a href="Details.aspx?entityID=A123456&amp;hash=123">ALPHA ELECTRIC LLC</a><br>12 - Domestic Limited Liability Company</td>
<td>04/01/2018</td>
<td>{status}<br>{status_date}</td>
</tr>
</table>
</body></html>
"""

DETAIL_TEMPLATE = """
<html><body>
<h1>ALPHA ELECTRIC LLC</h1>
<table>
<tr><td>Entity ID</td><td>A123456</td></tr>
<tr><td>Registered<br>Effective Date</td><td>04/01/2018</td></tr>
<tr><td>Status</td><td>{status}</td></tr>
<tr><td>Status Date</td><td>{status_date}</td></tr>
<tr><td>Entity Type</td><td>Domestic Limited Liability Company</td></tr>
<tr><td>Registered Agent<br>Office</td><td>AGENT PERSON<br>9 STATE ST<br>MADISON , WI 53703</td></tr>
<tr><td>Principal Office</td><td>{address}<br>{city} , {state} {zip_code}</td></tr>
</table>
</body></html>
"""

NO_MATCH = """
<html><body>
<div>Corporate Records | 0 records for advanced search.</div>
<p>Sorry, your search returned no records.</p>
</body></html>
"""


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", data_dir / "imports")
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def make_source(*, status="Organized", status_date="04/01/2018", address="123 MAIN ST", city="MADISON", state="WI", zip_code="53703"):
    search_html = SEARCH_TEMPLATE.format(status=status, status_date=status_date)
    detail_html = DETAIL_TEMPLATE.format(
        status=status,
        status_date=status_date,
        address=address,
        city=city,
        state=state,
        zip_code=zip_code,
    )

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.lower().endswith("/results.aspx"):
            return httpx.Response(200, text=search_html, request=request)
        if request.url.path.lower().endswith("/details.aspx"):
            return httpx.Response(200, text=detail_html, request=request)
        return httpx.Response(404, request=request)

    return WdfiCorporateRecordsSource(client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True))


def context(**overrides) -> ContractorContext:
    values = {
        "internal_id": 1,
        "external_id": "1",
        "contractor_name": "Alpha Electric LLC",
        "address_1": "123 Main St",
        "city": "Madison",
        "state": "WI",
        "zip": "53703",
        "dfi": "N",
    }
    values.update(overrides)
    return ContractorContext(**values)


def test_wdfi_is_registered_and_ready():
    assert "wdfi" in implemented_source_keys()
    item = next(source for source in SOURCES if source["key"] == "wdfi")
    assert item["status"] == "ready"
    assert "apps.dfi.wi.gov" in item["url"]


def test_search_parser_requires_declared_result_count_to_match():
    parsed = parse_search_results(SEARCH_TEMPLATE.format(status="Organized", status_date="04/01/2018"))
    assert parsed.complete is True
    assert parsed.declared_count == 1
    assert parsed.records[0].entity_id == "A123456"
    assert parsed.records[0].name == "ALPHA ELECTRIC LLC"


def test_exact_name_and_location_confirm_active_wi_entity(isolated_db):
    result = make_source().search(context())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.source_record_id == "A123456"
    assert result.evidence[0].field_name == "dfi"
    assert result.evidence[0].observed_value == "Y"
    assert result.normalized_payload["selected_record"]["city"] == "MADISON"


def test_live_address_with_separate_city_comma_state_and_zip_lines(isolated_db):
    # Live DFI pages separate these spans with newlines, unlike the old fixture.
    source = make_source(city='MADISON\n,\nWI\n53703\nUnited States of America', state='', zip_code='')
    result = source.search(context())
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    record = result.normalized_payload['selected_record']
    assert record['address_1'] == '123 MAIN ST'
    assert (record['city'], record['state'], record['zip_code']) == ('MADISON', 'WI', '53703')


def test_legacy_adverse_status_format_is_preserved_when_semantically_same(isolated_db):
    source = make_source(status="Delinquent", status_date="10/01/2024")
    result = source.search(context(dfi="Delinquent as of 10/1/24"))

    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.evidence[0].observed_value == "Delinquent as of 10/1/24"
    assert result.evidence[0].details["raw_status"] == "Delinquent"
    assert result.evidence[0].details["status_date"] == "10/01/2024"


def test_exact_name_with_conflicting_location_requires_human_review(isolated_db):
    source = make_source(address="900 OTHER RD", city="GREEN BAY", zip_code="54301")
    result = source.search(context())

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence == []
    assert result.normalized_payload["top_candidates"][0]["source_record_id"] == "A123456"


def test_complete_no_match_never_invents_a_dfi_negative(isolated_db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, text=NO_MATCH, request=request)

    source = WdfiCorporateRecordsSource(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = source.search(context(dfi="Y"))

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence == []


def test_non_wisconsin_match_is_evidence_only_for_legacy_dfi_field(isolated_db):
    source = make_source(address="123 MAIN ST", city="CHICAGO", state="IL", zip_code="60601")
    result = source.search(context(city="Chicago", state="IL", zip="60601", dfi="Active in IL but not in DFI"))

    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.evidence == []
    assert any("primary state is not Wisconsin" in warning for warning in result.warnings)
    assert result.normalized_payload["selected_record"]["status"] == "Organized"


def test_blocked_wdfi_response_stays_incomplete(isolated_db):
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="blocked", request=request)

    source = WdfiCorporateRecordsSource(client=httpx.Client(transport=httpx.MockTransport(handler)))
    result = source.search(context())

    assert result.status == SourceResultStatus.BLOCKED
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.evidence == []
