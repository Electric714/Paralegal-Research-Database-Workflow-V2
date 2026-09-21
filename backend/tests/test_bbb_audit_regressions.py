from __future__ import annotations

import httpx
import pytest

from app import database as db
from app.research.models import IdentityStatus, SourceResultStatus
from app.research.service import record_identity_judgment
from app.research.sources.base import ContractorContext
from app.research.sources.bbb import BBB_SITEMAP_INDEX, BbbBusinessProfileSource


PROFILE = "https://www.bbb.org/us/wi/madison/profile/general-contractor/example-builders-llc-0694-1000000000"
CHILD = "https://www.bbb.org/sitemap-business-profiles-1.xml"
PROFILE_HTML = """
<html><body>
<script type="application/ld+json">
{"@type":"LocalBusiness","name":"Example Builders LLC",
 "address":{"@type":"PostalAddress","streetAddress":"123 Main St","addressLocality":"Madison",
 "addressRegion":"WI","postalCode":"53703"}}
</script>
</body></html>
"""
COMPLAINT_HTML = """
<html><body><h2>Customer Complaints Summary</h2>
<p>2 complaints in the last 3 years. 1 complaint closed in the last 12 months.</p>
<h2>If you've experienced an issue</h2></body></html>
"""


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    data_dir = tmp_path / "data"
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", data_dir / "imports")
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def test_legacy_slug_judgment_confirms_when_live_location_would_not_auto_confirm(isolated_db):
    db.replace_master_database(
        "master.csv",
        ["id", "contractor_name", "address_1", "city", "state", "zip"],
        [{
            "id": "100",
            "contractor_name": "Example Builders LLC",
            "address_1": "555 Different St",
            "city": "Milwaukee",
            "state": "WI",
            "zip": "53202",
        }],
        str(isolated_db / "master.csv"),
    )
    bidder_id = db.active_bidder_ids()[0]
    record_identity_judgment(
        bidder_id=bidder_id,
        source_key="bbb",
        source_record_id="example-builders-llc-0694-1000000000",
        judgment="SAME_ENTITY",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == BBB_SITEMAP_INDEX:
            return httpx.Response(200, text=f"<sitemapindex><sitemap><loc>{CHILD}</loc></sitemap></sitemapindex>")
        if url == CHILD:
            return httpx.Response(200, text=f"<urlset><url><loc>{PROFILE}</loc></url></urlset>")
        if url == PROFILE:
            return httpx.Response(200, text=PROFILE_HTML)
        if url == PROFILE + "/complaints":
            return httpx.Response(200, text=COMPLAINT_HTML)
        return httpx.Response(404)

    result = BbbBusinessProfileSource(
        transport=httpx.MockTransport(handler),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
    ).search(
        ContractorContext(
            internal_id=bidder_id,
            external_id="100",
            contractor_name="Example Builders LLC",
            address_1="555 Different St",
            city="Milwaukee",
            state="WI",
            zip="53202",
        )
    )

    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.source_record_id == "0694-1000000000"
