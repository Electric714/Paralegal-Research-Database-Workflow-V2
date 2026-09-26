from __future__ import annotations

from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.wcca_browser import (
    PUBLIC_BROWSER_ACQUISITION,
    WccaBrowserUnavailable,
    WccaBusinessSearchResult,
    WccaHumanActionTimeout,
    WccaLayoutChanged,
    WccaPublicBrowserSource,
    WccaSearchHit,
)


def contractor(**updates) -> ContractorContext:
    values = {
        "internal_id": 1,
        "external_id": "42",
        "contractor_name": "ACME ELECTRIC, LLC",
        "related_companies": "ACME SERVICES LLC;OLD ACME CO",
        "address_1": "123 Main St",
        "city": "Madison",
        "state": "WI",
        "zip": "53703",
    }
    values.update(updates)
    return ContractorContext(**values)


def hit(
    *,
    case_number: str = "2026CV000123",
    name: str = "ACME ELECTRIC LLC",
    county: str = "Dane",
) -> WccaSearchHit:
    return WccaSearchHit(
        case_number=case_number,
        filing_date="09-26-2026",
        county=county,
        case_status="Closed",
        matched_party_name=name,
        caption=f"Example Plaintiff vs. {name}",
        case_url=f"https://wcca.wicourts.gov/caseDetail.html?caseNo={case_number}&countyNo=13&index=0",
    )


class FakeBrowser:
    def __init__(self, results=None, error_on=None):
        self.results = results or {}
        self.error_on = error_on or {}
        self.calls = []
        self.closed = False

    def search_business(self, name):
        self.calls.append(name)
        if name in self.error_on:
            raise self.error_on[name]
        return WccaBusinessSearchResult(name, tuple(self.results.get(name, ())), True)

    def close(self):
        self.closed = True


def source_for(browser: FakeBrowser) -> WccaPublicBrowserSource:
    return WccaPublicBrowserSource(browser_factory=lambda: browser)


def test_health_declares_live_public_browser_and_no_rest_api():
    health = WccaPublicBrowserSource().health_check()
    assert health["status"] == "ready_public_browser"
    assert health["public_site_automation"] is True
    assert health["rest_api_used"] is False
    assert health["captcha_bypass"] is False


def test_complete_live_browser_no_match_checks_every_alias_and_stays_source_level_only():
    browser = FakeBrowser()
    result = source_for(browser).search(contractor())

    assert browser.calls == ["ACME ELECTRIC, LLC", "ACME SERVICES LLC", "OLD ACME CO"]
    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.acquisition_method == PUBLIC_BROWSER_ACQUISITION
    assert result.adapter_version == "2.0.0"
    assert not any(item.field_name == "circuit_court" for item in result.evidence)
    assert [(item.field_name, item.observed_value) for item in result.evidence] == [
        ("wcca_public_search", "NO_CURRENTLY_DISPLAYED_MATCH")
    ]
    assert result.normalized_payload["browser_automation"]["rest_api_used"] is False


def test_exact_live_result_becomes_confirmed_positive_comparison_evidence():
    browser = FakeBrowser(results={"ACME ELECTRIC, LLC": [hit()]})
    result = source_for(browser).search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert [(item.field_name, item.observed_value) for item in result.evidence] == [
        ("circuit_court", "Y"),
        ("wcca_case", "2026CV000123"),
    ]
    assert result.evidence[1].source_url.startswith("https://wcca.wicourts.gov/")
    assert "Caption:" in result.evidence[1].details["note"]


def test_similar_but_not_exact_live_party_requires_review_instead_of_auto_accepting():
    browser = FakeBrowser(
        results={"ACME ELECTRIC, LLC": [hit(name="ACME ELECTRIC LLC WEST")]}
    )
    result = source_for(browser).search(contractor())

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert not any(item.field_name == "circuit_court" for item in result.evidence)
    assert result.normalized_payload["browser_automation"]["ambiguous_candidates"]


def test_human_captcha_timeout_fails_closed_and_never_becomes_no_match():
    browser = FakeBrowser(
        error_on={
            "ACME ELECTRIC, LLC": WccaHumanActionTimeout(
                "Human hCaptcha challenge was not completed."
            )
        }
    )
    result = source_for(browser).search(contractor())

    assert result.status == SourceResultStatus.BLOCKED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False
    assert not any(item.field_name == "circuit_court" for item in result.evidence)


def test_layout_change_is_explicit_source_failure_not_false_negative():
    browser = FakeBrowser(
        error_on={"ACME ELECTRIC, LLC": WccaLayoutChanged("results table changed")}
    )
    result = source_for(browser).search(contractor())

    assert result.status == SourceResultStatus.LAYOUT_CHANGED
    assert result.is_clean_negative is False
    assert any("results table changed" in warning for warning in result.warnings)


def test_browser_unavailable_is_explicit_source_unavailable():
    browser = FakeBrowser(
        error_on={"ACME ELECTRIC, LLC": WccaBrowserUnavailable("Edge unavailable")}
    )
    result = source_for(browser).search(contractor())

    assert result.status == SourceResultStatus.SOURCE_UNAVAILABLE
    assert result.is_clean_negative is False


def test_confirmed_case_is_retained_if_later_alias_hits_human_challenge():
    browser = FakeBrowser(
        results={"ACME ELECTRIC, LLC": [hit()]},
        error_on={"ACME SERVICES LLC": WccaHumanActionTimeout("solve challenge in browser")},
    )
    result = source_for(browser).search(contractor())

    assert result.status == SourceResultStatus.BLOCKED
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert any(item.field_name == "circuit_court" and item.observed_value == "Y" for item in result.evidence)
    assert any(item.field_name == "wcca_case" for item in result.evidence)
    assert result.is_clean_negative is False
