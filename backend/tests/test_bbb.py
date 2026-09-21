from __future__ import annotations

import httpx
import pytest

from app import database as db
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.service import list_tasks, persist_source_result
from app.research.sources.base import ContractorContext
from app.research.sources.bbb import (
    BBB_SITEMAP_INDEX,
    BbbBusinessProfileSource,
    parse_complaint_summary,
    parse_profile_html,
)


PROFILE = "https://www.bbb.org/us/wi/madison/profile/general-contractor/example-builders-llc-0694-1000000000"
CHILD = "https://www.bbb.org/sitemap-business-profiles-1.xml"

PROFILE_HTML = """
<html><body>
<script type="application/ld+json">
{"@type":"LocalBusiness","name":"Example Builders LLC","telephone":"(608) 555-0100",
 "address":{"@type":"PostalAddress","streetAddress":"123 Main St","addressLocality":"Madison",
 "addressRegion":"WI","postalCode":"53703"}}
</script>
<h1>Example Builders LLC</h1>
<p>123 Main St, Madison, WI 53703</p>
<p>BBB Accredited Business</p><p>BBB Rating: A+</p>
</body></html>
"""


def xml_index(*urls: str) -> str:
    entries = "".join(f"<sitemap><loc>{url}</loc></sitemap>" for url in urls)
    return f"<?xml version='1.0'?><sitemapindex>{entries}</sitemapindex>"


def xml_urls(*urls: str) -> str:
    entries = "".join(f"<url><loc>{url}</loc></url>" for url in urls)
    return f"<?xml version='1.0'?><urlset>{entries}</urlset>"


def contractor(*, internal_id: int = 1, name: str = "Example Builders LLC", address: str = "123 Main St", city: str = "Madison", state: str = "WI", zip_code: str = "53703") -> ContractorContext:
    return ContractorContext(
        internal_id=internal_id,
        external_id="100",
        contractor_name=name,
        address_1=address,
        city=city,
        state=state,
        zip=zip_code,
    )


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def transport_for(*, complaints: str = "3 total complaints in the last 3 years. 1 complaint closed in the last 12 months.", profile_html: str = PROFILE_HTML, profile_status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BBB_SITEMAP_INDEX:
            return httpx.Response(200, text=xml_index(CHILD), headers={"content-type": "application/xml"})
        if url == CHILD:
            return httpx.Response(200, text=xml_urls(PROFILE), headers={"content-type": "application/xml"})
        if url == PROFILE:
            return httpx.Response(profile_status, text=profile_html, headers={"content-type": "text/html"})
        if url == PROFILE + "/complaints":
            return httpx.Response(200, text=f"<html><body><h1>Complaints</h1><p>{complaints}</p></body></html>")
        return httpx.Response(404, text="not found")
    return httpx.MockTransport(handler)


def test_profile_and_complaint_parsers_capture_only_summary_identity_data():
    profile = parse_profile_html(PROFILE_HTML, PROFILE)
    assert profile is not None
    assert profile.name == "Example Builders LLC"
    assert profile.address == "123 Main St"
    assert profile.city == "Madison"
    assert profile.state == "WI"
    assert profile.zip_code == "53703"
    assert profile.accredited is True
    assert profile.rating == "A+"

    total, closed = parse_complaint_summary(
        "<html><body>605 total complaints in the last 3 years. 192 complaints closed in the last 12 months.</body></html>"
    )
    assert total == 605
    assert closed == 192


def test_exact_profile_with_complaints_proposes_y(isolated_db):
    source = BbbBusinessProfileSource(
        transport=transport_for(),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    )
    result = source.search(contractor())
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].field_name == "better_business_bureau_complaints"
    assert result.evidence[0].observed_value == "Y"
    assert result.evidence[0].details["total_complaints_3y"] == 3
    assert "Initial Complaint" not in str(result.normalized_payload)


def test_exact_profile_with_zero_complaints_can_propose_n(isolated_db):
    source = BbbBusinessProfileSource(
        transport=transport_for(complaints="0 complaints in the last 3 years. 0 complaints closed in the last 12 months."),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    )
    result = source.search(contractor())
    assert result.status == SourceResultStatus.SUCCESS_COMPLETE
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].observed_value == "N"


def test_no_sitemap_candidate_is_not_a_clean_negative(isolated_db):
    other = "https://www.bbb.org/us/wi/madison/profile/general-contractor/totally-different-company-0694-2222222222"

    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) == BBB_SITEMAP_INDEX:
            return httpx.Response(200, text=xml_index(CHILD))
        if str(request.url) == CHILD:
            return httpx.Response(200, text=xml_urls(other))
        return httpx.Response(404)

    source = BbbBusinessProfileSource(
        transport=httpx.MockTransport(handler),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    )
    result = source.search(contractor())
    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert result.evidence == []


def test_plausible_wrong_location_requires_identity_review(isolated_db):
    profile_html = PROFILE_HTML.replace("123 Main St", "999 Main St").replace("Madison", "Milwaukee").replace("53703", "53202")
    profile_url = PROFILE.replace("/madison/", "/milwaukee/")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BBB_SITEMAP_INDEX:
            return httpx.Response(200, text=xml_index(CHILD))
        if url == CHILD:
            return httpx.Response(200, text=xml_urls(profile_url))
        if url == profile_url:
            return httpx.Response(200, text=profile_html)
        return httpx.Response(404)

    source = BbbBusinessProfileSource(
        transport=httpx.MockTransport(handler),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    )
    result = source.search(contractor())
    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.normalized_payload["top_candidates"]
    assert result.evidence == []


def test_profile_access_block_never_becomes_no_match(isolated_db):
    source = BbbBusinessProfileSource(
        transport=transport_for(profile_status=403),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    )
    result = source.search(contractor())
    assert result.status == SourceResultStatus.BLOCKED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.evidence == []


def test_verified_profile_url_is_reused_on_next_run(isolated_db):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "address_1", "city", "state", "zip", "better_business_bureau_complaints"],
        [{
            "id": "100",
            "contractor_name": "Example Builders LLC",
            "address_1": "123 Main St",
            "city": "Madison",
            "state": "WI",
            "zip": "53703",
            "better_business_bureau_complaints": "",
        }],
        str(isolated_db / "master.csv"),
    )
    bidder_id = db.active_bidder_ids()[0]
    context = contractor(internal_id=bidder_id)
    first = BbbBusinessProfileSource(
        transport=transport_for(),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    ).search(context)
    run = db.create_run([bidder_id], ["bbb"], 1)
    task_id = list_tasks(run["id"])[0]["id"]
    persist_source_result(task_id, first)

    requested: list[str] = []
    def cached_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        if url == PROFILE:
            return httpx.Response(200, text=PROFILE_HTML)
        if url == PROFILE + "/complaints":
            return httpx.Response(200, text="<html><body>3 total complaints in the last 3 years.</body></html>")
        return httpx.Response(500, text="sitemap should not be needed")

    second = BbbBusinessProfileSource(
        transport=httpx.MockTransport(cached_handler),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    ).search(context)
    assert second.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert second.normalized_payload["cached_profile_used"] is True
    assert requested == [PROFILE, PROFILE + "/complaints"]
