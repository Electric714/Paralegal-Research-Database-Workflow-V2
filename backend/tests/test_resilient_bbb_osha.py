from __future__ import annotations

from datetime import date

import httpx

from app.research.models import SourceResultStatus
from app.research.source_registry import SOURCE_ADAPTERS
from app.research.sources.base import ContractorContext
from app.research.sources.bbb_resilient import ResilientBbbBusinessProfileSource
from app.research.sources.osha_resilient import ResilientOshaEstablishmentSource
from app.research.sources.public_browser import BrowserFetchResult


OSHA_NO_RESULTS = """
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

BBB_PROFILE = "https://www.bbb.org/us/wi/madison/profile/general-contractor/example-builders-llc-0694-1000000000"
BBB_CHILD = "https://www.bbb.org/sitemap-business-profiles-1.xml"
BBB_INDEX_XML = (
    "<?xml version='1.0'?><sitemapindex>"
    f"<sitemap><loc>{BBB_CHILD}</loc></sitemap>"
    "</sitemapindex>"
)
BBB_CHILD_XML = (
    "<?xml version='1.0'?><urlset>"
    f"<url><loc>{BBB_PROFILE}</loc></url>"
    "</urlset>"
)
BBB_PROFILE_HTML = """
<html><body>
<script type="application/ld+json">
{"@type":"LocalBusiness","name":"Example Builders LLC","telephone":"(608) 555-0100",
 "address":{"@type":"PostalAddress","streetAddress":"123 Main St","addressLocality":"Madison",
 "addressRegion":"WI","postalCode":"53703"}}
</script>
<h1>Business Profile</h1><h2>Example Builders LLC</h2>
<p>123 Main St, Madison, WI 53703</p><p>BBB Rating: A+</p>
</body></html>
"""
BBB_COMPLAINT_HTML = """
<html><body><h1>Complaints</h1><h2>Customer Complaints Summary</h2>
<p>1 total complaint in the last 3 years. 1 complaint closed in the last 12 months.</p>
<h2>If you've experienced an issue</h2></body></html>
"""


class FakeBrowserSession:
    instances: list["FakeBrowserSession"] = []

    def __init__(self, *, allowed_hosts, warmup_url=None, timeout_ms=45_000):
        self.allowed_hosts = set(allowed_hosts)
        self.warmup_url = warmup_url
        self.timeout_ms = timeout_ms
        self.document_urls: list[str] = []
        self.resource_urls: list[str] = []
        self.closed = False
        self.__class__.instances.append(self)

    def get_document(self, url: str) -> BrowserFetchResult:
        self.document_urls.append(url)
        if "osha.gov/ords/imis/establishment.search" in url:
            return BrowserFetchResult(OSHA_NO_RESULTS, url, 200, "text/html")
        if url == BBB_PROFILE:
            return BrowserFetchResult(BBB_PROFILE_HTML, url, 200, "text/html")
        if url == BBB_PROFILE + "/complaints":
            return BrowserFetchResult(BBB_COMPLAINT_HTML, url, 200, "text/html")
        raise AssertionError(f"Unexpected browser document URL: {url}")

    def get_resource(self, url: str) -> BrowserFetchResult:
        self.resource_urls.append(url)
        if url.endswith("sitemap-business-profiles-index.xml"):
            return BrowserFetchResult(BBB_INDEX_XML, url, 200, "application/xml")
        if url == BBB_CHILD:
            return BrowserFetchResult(BBB_CHILD_XML, url, 200, "application/xml")
        raise AssertionError(f"Unexpected browser resource URL: {url}")

    def close(self) -> None:
        self.closed = True


def test_source_registry_uses_resilient_public_site_adapters():
    assert SOURCE_ADAPTERS["osha"] is ResilientOshaEstablishmentSource
    assert SOURCE_ADAPTERS["bbb"] is ResilientBbbBusinessProfileSource


def test_osha_http_403_falls_back_to_browser_without_becoming_blocked():
    FakeBrowserSession.instances.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Forbidden", request=request)

    source = ResilientOshaEstablishmentSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        today=date(1972, 1, 1),
        browser_session_factory=FakeBrowserSession,
    )
    result = source.search(
        ContractorContext(
            internal_id=1,
            external_id="1",
            contractor_name="Acme Roofing",
            address_1="",
            city="",
            state="",
            zip="",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert len(FakeBrowserSession.instances) == 1
    assert len(FakeBrowserSession.instances[0].document_urls) == 1
    assert FakeBrowserSession.instances[0].closed is True


def test_bbb_http_403_falls_back_to_browser_for_sitemaps_profile_and_complaints():
    FakeBrowserSession.instances.clear()

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(403, text="Access denied", request=request)

    source = ResilientBbbBusinessProfileSource(
        transport=httpx.MockTransport(handler),
        state_ranges={"WI": ((1, 1),)},
        boundary_margin=0,
        browser_session_factory=FakeBrowserSession,
    )
    result = source.search(
        ContractorContext(
            internal_id=1,
            external_id="100",
            contractor_name="Example Builders LLC",
            address_1="123 Main St",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.evidence[0].observed_value == "Y"
    assert len(FakeBrowserSession.instances) == 1
    session = FakeBrowserSession.instances[0]
    assert any(url.endswith("sitemap-business-profiles-index.xml") for url in session.resource_urls)
    assert BBB_CHILD in session.resource_urls
    assert BBB_PROFILE in session.document_urls
    assert BBB_PROFILE + "/complaints" in session.document_urls
    assert session.closed is True
