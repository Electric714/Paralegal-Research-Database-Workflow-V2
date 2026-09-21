from __future__ import annotations

from app.research.sources.wcrb_browser import browser_request_decision


def test_wcrb_coverage_lookup_post_is_allowed():
    decision = browser_request_decision(
        "POST",
        "https://www.wcrb.org/coverage-lookup/",
        resource_type="document",
    )
    assert decision.allowed is True
    assert decision.reason == "wcrb_coverage_lookup_post"


def test_wcrb_default_aspx_post_is_allowed():
    decision = browser_request_decision(
        "POST",
        "https://www.wcrb.org/coverage-lookup/Default.aspx",
        resource_type="document",
    )
    assert decision.allowed is True


def test_external_post_is_blocked():
    decision = browser_request_decision(
        "POST",
        "https://example.com/collect",
        resource_type="fetch",
    )
    assert decision.allowed is False
    assert decision.reason == "external_post_blocked"


def test_unrelated_wcrb_post_is_blocked():
    decision = browser_request_decision(
        "POST",
        "https://www.wcrb.org/account/change-password",
        resource_type="document",
    )
    assert decision.allowed is False
    assert decision.reason == "unexpected_wcrb_method_or_path"


def test_wcrb_script_resources_are_allowed():
    decision = browser_request_decision(
        "GET",
        "https://www.wcrb.org/ScriptResource.axd?d=example",
        resource_type="script",
    )
    assert decision.allowed is True


def test_required_jsdelivr_static_assets_are_allowed_read_only():
    read = browser_request_decision(
        "GET",
        "https://cdn.jsdelivr.net/npm/bootstrap@5/dist/js/bootstrap.min.js",
        resource_type="script",
    )
    write = browser_request_decision(
        "POST",
        "https://cdn.jsdelivr.net/anything",
        resource_type="fetch",
    )
    assert read.allowed is True
    assert write.allowed is False
