from __future__ import annotations

import os
import time
from types import SimpleNamespace

import httpx

from app.research.field_mappings import source_owns_field
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.mn_pca import (
    CACHE_MAX_AGE_SECONDS,
    CSV_EXPORT_URL,
    DATA_VIEW_URL,
    LANDING_URL,
    MinnesotaPcaEnforcementSource,
    MpcaDatasetError,
    parse_mpca_csv,
)
from app.research.sources.public_browser import BrowserBlockedError


CSV = """Company or individual(s),Public date,Violation location,Violation description,Net penalty,Case type
Acme Construction LLC,03/05/2026,Minneapolis,Construction stormwater,$9663,Administrative penalty order
Other Builder Inc,01/10/2025,St Paul,Hazardous waste,$1200,Administrative penalty order
"""

CURRENT_HEADER_VARIANT_CSV = """Company or individual(s) (location),Public date,Violation location,Violation(s),Net penalty,Case type
Acme Construction LLC,03/05/2026,Minneapolis,Construction stormwater,$9663,Administrative penalty order
"""

BROKEN_TABLEAU_SUMMARY_CSV = """YEAR(Enforcement Action Date),AGG(Number of Cases)
2026,147
"""

CAPTCHA_HTML = """<!DOCTYPE html><html><title>Radware Captcha Page</title>
<body>We apologize for the inconvenience. hCaptcha</body></html>"""


def contractor(*, name: str = "Acme Construction, LLC", related: str = "") -> ContractorContext:
    return ContractorContext(
        internal_id=7,
        external_id="B-7",
        contractor_name=name,
        related_companies=related,
        city="Minneapolis",
        state="MN",
    )


def direct_source(
    *,
    csv_body: str = CSV,
    csv_status: int = 200,
    csv_content_type: str = "text/csv",
    min_expected_records: int = 1,
    cache_path: str | None = None,
    allow_cached_fallback: bool = False,
) -> tuple[MinnesotaPcaEnforcementSource, list[str]]:
    requested: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requested.append(str(request.url))
        if request.url.host == "services.pca.state.mn.us":
            raise AssertionError("MPCA adapter must not use the REST API")
        if str(request.url) == LANDING_URL:
            return httpx.Response(200, text="<html>MPCA compliance and enforcement</html>", request=request)
        if str(request.url) == DATA_VIEW_URL:
            return httpx.Response(200, text="<html>Tableau enforcement workbook</html>", request=request)
        if str(request.url) == CSV_EXPORT_URL:
            return httpx.Response(
                csv_status,
                text=csv_body,
                headers={"content-type": csv_content_type},
                request=request,
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    source = MinnesotaPcaEnforcementSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        min_expected_records=min_expected_records,
        allow_browser_fallback=False,
        allow_cached_fallback=allow_cached_fallback,
        cache_path=cache_path,
    )
    return source, requested


class FakeBrowserSession:
    def __init__(self, *, csv_text: str = CSV, blocked: bool = False, **kwargs):
        self.csv_text = csv_text
        self.blocked = blocked
        self.closed = False
        self.document_urls: list[str] = []
        self.resource_urls: list[str] = []

    def get_document(self, url: str):
        self.document_urls.append(url)
        if self.blocked:
            raise BrowserBlockedError("test security challenge")
        return SimpleNamespace(text="<html>ok</html>", final_url=url, status_code=200, content_type="text/html")

    def get_resource(self, url: str):
        self.resource_urls.append(url)
        if self.blocked:
            raise BrowserBlockedError("test security challenge")
        return SimpleNamespace(text=self.csv_text, final_url=url, status_code=200, content_type="text/csv")

    def close(self):
        self.closed = True


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
    assert len(digest) == 64


def test_missing_required_columns_fails_closed():
    try:
        parse_mpca_csv("Name,Something\nAcme,Value\n")
    except MpcaDatasetError:
        pass
    else:
        raise AssertionError("expected MpcaDatasetError")


def test_default_path_uses_tableau_and_never_rest_api():
    source, requested = direct_source()
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.acquisition_method == "official_tableau_direct_csv"
    assert result.normalized_payload["rest_api_used"] is False
    assert LANDING_URL in requested
    assert DATA_VIEW_URL in requested
    assert CSV_EXPORT_URL in requested
    assert all("services.pca.state.mn.us" not in url for url in requested)


def test_direct_tableau_clean_no_match_is_safe_research_negative_only():
    source, _ = direct_source()
    result = source.search(contractor(name="No Such Contractor LLC"))

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.is_clean_negative is True
    assert result.evidence == []
    assert "never proposes" in result.normalized_payload["negative_semantics"]


def test_captcha_on_direct_export_uses_browser_context_tableau_export():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) in {LANDING_URL, DATA_VIEW_URL}:
            return httpx.Response(200, text="<html>ok</html>", request=request)
        if str(request.url) == CSV_EXPORT_URL:
            return httpx.Response(
                200,
                text=CAPTCHA_HTML,
                headers={"content-type": "text/html"},
                request=request,
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    sessions: list[FakeBrowserSession] = []

    def browser_factory(**kwargs):
        session = FakeBrowserSession(csv_text=CSV, **kwargs)
        sessions.append(session)
        return session

    source = MinnesotaPcaEnforcementSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_session_factory=browser_factory,
        min_expected_records=1,
        allow_cached_fallback=False,
    )
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.acquisition_method == "official_tableau_headless_browser_csv"
    assert sessions[0].document_urls == [DATA_VIEW_URL]
    assert sessions[0].resource_urls == [CSV_EXPORT_URL]
    assert result.normalized_payload["rest_api_used"] is False


def test_headless_browser_block_then_persistent_system_browser_succeeds():
    def handler(request: httpx.Request) -> httpx.Response:
        if str(request.url) in {LANDING_URL, DATA_VIEW_URL}:
            return httpx.Response(200, text="<html>ok</html>", request=request)
        if str(request.url) == CSV_EXPORT_URL:
            return httpx.Response(
                200,
                text=CAPTCHA_HTML,
                headers={"content-type": "text/html"},
                request=request,
            )
        raise AssertionError(f"unexpected URL: {request.url}")

    factory_calls: list[dict] = []

    def browser_factory(**kwargs):
        factory_calls.append(kwargs)
        return FakeBrowserSession(csv_text=CSV, blocked=len(factory_calls) == 1, **kwargs)

    source = MinnesotaPcaEnforcementSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        browser_session_factory=browser_factory,
        browser_profile_dir="test-profile",
        min_expected_records=1,
        allow_cached_fallback=False,
    )
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.acquisition_method == "official_tableau_persistent_browser_csv"
    assert len(factory_calls) == 2
    assert factory_calls[0]["headless"] is True
    assert factory_calls[0]["profile_dir"] is None
    assert factory_calls[1]["headless"] is False
    assert factory_calls[1]["profile_dir"] == "test-profile"


def test_approved_alias_can_confirm():
    source, _ = direct_source()
    result = source.search(contractor(name="Parent Holdings LLC", related="Acme Construction LLC"))
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.evidence[0].details["query_basis"] == "approved_alias"


def test_typo_name_requires_review():
    body = CSV.replace("Acme Construction LLC", "Acme Constrction LLC")
    source, _ = direct_source(csv_body=body)
    result = source.search(contractor())

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.is_clean_negative is False
    assert result.evidence[0].details["master_field_proposal_allowed"] is False


def test_legal_name_expansion_requires_review_instead_of_clean_negative():
    body = CSV.replace("Acme Construction LLC", "Acme Construction Services LLC")
    source, _ = direct_source(csv_body=body)
    result = source.search(contractor())

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.is_clean_negative is False
    assert result.evidence[0].details["candidate_party"] == "Acme Construction Services LLC"


def test_http_failure_cannot_become_negative():
    source, _ = direct_source(csv_body="Service unavailable", csv_status=503)
    result = source.search(contractor(name="No Such Contractor LLC"))

    assert result.status == SourceResultStatus.HTTP_ERROR
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_html_instead_of_csv_cannot_become_negative():
    source, _ = direct_source(csv_body="<!doctype html><html>not csv</html>", csv_content_type="text/html")
    result = source.search(contractor(name="No Such Contractor LLC"))

    assert result.status == SourceResultStatus.PARSER_FAILURE
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_summary_sheet_layout_cannot_become_negative():
    source, _ = direct_source(csv_body=BROKEN_TABLEAU_SUMMARY_CSV)
    result = source.search(contractor(name="No Such Contractor LLC"))

    assert result.status == SourceResultStatus.DATASET_MALFORMED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_production_row_floor_rejects_suspiciously_small_extract():
    source, _ = direct_source(min_expected_records=25)
    result = source.search(contractor())

    assert result.status == SourceResultStatus.DATASET_MALFORMED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False
    assert "expected at least 25" in result.warnings[0]


def test_recent_validated_cache_can_surface_finding_but_is_partial(tmp_path):
    cache = tmp_path / "mpca.csv"
    cache.write_text(CSV, encoding="utf-8")
    source, _ = direct_source(
        csv_body="down",
        csv_status=503,
        cache_path=str(cache),
        allow_cached_fallback=True,
    )
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.acquisition_method == "validated_recent_tableau_cache"
    assert result.evidence[0].observed_value == "Y"
    assert result.normalized_payload["dataset_cached"] is True


def test_recent_cache_no_match_is_never_clean_negative(tmp_path):
    cache = tmp_path / "mpca.csv"
    cache.write_text(CSV, encoding="utf-8")
    source, _ = direct_source(
        csv_body="down",
        csv_status=503,
        cache_path=str(cache),
        allow_cached_fallback=True,
    )
    result = source.search(contractor(name="No Such Contractor LLC"))

    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert result.evidence == []
    assert "not a clean negative" in result.warnings[-1]


def test_stale_cache_is_ignored_and_failure_stays_failure(tmp_path):
    cache = tmp_path / "mpca.csv"
    cache.write_text(CSV, encoding="utf-8")
    stale = time.time() - CACHE_MAX_AGE_SECONDS - 60
    os.utime(cache, (stale, stale))

    source, _ = direct_source(
        csv_body="down",
        csv_status=503,
        cache_path=str(cache),
        allow_cached_fallback=True,
    )
    result = source.search(contractor(name="No Such Contractor LLC"))

    assert result.status == SourceResultStatus.HTTP_ERROR
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_field_ownership_is_narrow():
    assert source_owns_field("mn_pca", "environmental_violations") is True
    assert source_owns_field("mn_pca", "misc_violations") is False
    assert source_owns_field("mn_pca", "prevailing_wage_violations") is False
