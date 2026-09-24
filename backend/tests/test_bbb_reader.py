from __future__ import annotations

import httpx
import pytest

from app import database as db
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.bbb_reader import (
    parse_complaint_summary,
    parse_search_profiles,
)
from app.research.sources.bbb_resilient import ResilientBbbBusinessProfileSource


PROFILE = "https://www.bbb.org/us/fl/miami/profile/roofing-contractors/a-1-duran-roofing-inc-0633-27000818"
SEARCH_MARKDOWN = f"""
Title: Search results for A-1 Duran Roofing near Miami, FL | Better Business Bureau

# Showing: **1** results for **A-1 Duran Roofing** near Miami, FL

### [_A_-_1_ _Duran_ _Roofing_, Inc.]({PROFILE}/addressId/260058)
Roofing Contractors
BBB Rating: A+
(305) 470-9570
8095 NW 64th St,
Miami, FL 33166-2747
"""
ZERO_COMPLAINTS = """
Title: A-1 Duran Roofing, Inc. | BBB Complaints | Better Business Bureau

Business Profile
A-1 Duran Roofing, Inc.
# Complaints
This business has 0 complaints
## If you've experienced an issue
"""
POSITIVE_COMPLAINTS = """
# Complaints
Customer Complaints Summary
3 total complaints in the last 3 years.
1 complaint closed in the last 12 months.
"""


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", data_dir / "imports")
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def test_reader_search_parser_extracts_exact_bbb_card_identity():
    profiles, total = parse_search_profiles(SEARCH_MARKDOWN)
    assert total == 1
    assert len(profiles) == 1
    profile = profiles[0]
    assert profile.profile_url == PROFILE
    assert profile.name == "A-1 Duran Roofing, Inc."
    assert profile.address == "8095 NW 64th St"
    assert profile.city == "Miami"
    assert profile.state == "FL"
    assert profile.zip_code == "33166-2747"


def test_reader_complaint_parser_handles_zero_and_positive_counts():
    assert parse_complaint_summary(ZERO_COMPLAINTS) == (0, None)
    assert parse_complaint_summary(POSITIVE_COMPLAINTS) == (3, 1)


def test_bbb_production_reader_path_avoids_local_bbb_block(isolated_db):
    requested: list[str] = []

    def reader_handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        requested.append(url)
        assert request.url.host == "r.jina.ai"
        if "/https://www.bbb.org/search" in url:
            return httpx.Response(200, text=SEARCH_MARKDOWN, request=request)
        if "/https://www.bbb.org/us/fl/miami/profile/roofing-contractors/a-1-duran-roofing-inc-0633-27000818/complaints" in url:
            return httpx.Response(200, text=ZERO_COMPLAINTS, request=request)
        return httpx.Response(404, text="not found", request=request)

    def blocked_local_handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"reader-first BBB path unexpectedly hit local BBB HTTP: {request.url}")

    source = ResilientBbbBusinessProfileSource(
        transport=httpx.MockTransport(blocked_local_handler),
        reader_transport=httpx.MockTransport(reader_handler),
        reader_first=True,
        browser_first=False,
    )
    result = source.search(
        ContractorContext(
            internal_id=1,
            external_id="100",
            contractor_name="A-1 DURAN ROOFING INC",
            address_1="8095 NW 64th St",
            city="Miami",
            state="FL",
            zip="33166-2747",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_COMPLETE
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.evidence[0].observed_value == "N"
    assert result.evidence[0].source_url == PROFILE + "/complaints"
    assert result.normalized_payload["fetched_via"] == "jina_reader"
    assert result.acquisition_method == "bbb_public_pages_reader_then_browser"
    assert len(requested) == 2
    assert all("sitemap-business-profiles" not in url for url in requested)
