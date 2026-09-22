from __future__ import annotations

import json

from app.research.field_mappings import source_owns_field
from app.research.sources.wcrb_browser import (
    WcrbEnvironmentError,
    WcrbProbeError,
    _normalize_playwright_error,
    _persist_failure,
    _redact_hidden_input_values,
    _safe_request_url,
    browser_request_decision,
)


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


def test_similar_but_wrong_wcrb_path_post_is_blocked():
    decision = browser_request_decision(
        "POST",
        "https://www.wcrb.org/coverage-lookup-malicious",
        resource_type="document",
    )
    assert decision.allowed is False
    assert decision.reason == "unexpected_wcrb_method_or_path"


def test_wcrb_http_downgrade_is_blocked():
    decision = browser_request_decision(
        "GET",
        "http://www.wcrb.org/coverage-lookup/",
        resource_type="document",
    )
    assert decision.allowed is False
    assert decision.reason == "https_required"


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


def test_request_log_url_drops_query_and_fragment_values():
    safe = _safe_request_url(
        "https://www.wcrb.org/coverage-lookup/Default.aspx?token=secret&x=1#fragment"
    )
    assert safe == "https://www.wcrb.org/coverage-lookup/Default.aspx"
    assert "secret" not in safe


def test_hidden_input_values_are_redacted_from_saved_html():
    html = """
    <html><body>
      <input type="hidden" name="__VIEWSTATE" value="very-secret-view-state" />
      <input value='another-secret' id='ctl00_NoBot1_NoBot1_ClientState' type='hidden'>
      <input type="text" name="employer" value="VISIBLE COMPANY NAME" />
    </body></html>
    """
    sanitized = _redact_hidden_input_values(html)
    assert "very-secret-view-state" not in sanitized
    assert "another-secret" not in sanitized
    assert sanitized.count('[redacted]') == 2
    assert "VISIBLE COMPANY NAME" in sanitized


def test_unquoted_hidden_input_value_is_redacted():
    html = '<input type=hidden name=__EVENTVALIDATION value=abc123>'
    sanitized = _redact_hidden_input_values(html)
    assert "abc123" not in sanitized
    assert '[redacted]' in sanitized


def test_missing_chromium_error_is_classified_as_environment_error():
    error = _normalize_playwright_error(
        RuntimeError("BrowserType.launch: Executable doesn't exist at C:/runtime/chromium.exe")
    )
    assert isinstance(error, WcrbEnvironmentError)
    assert "WCRB_PROBE.ps1" in str(error)


def test_failure_persistence_writes_diagnostics_requests_and_manifest(tmp_path):
    error = WcrbProbeError(
        "Failed at https://www.wcrb.org/coverage-lookup/Default.aspx?token=super-secret"
    )
    request_log = [
        {
            "method": "POST",
            "url": "https://www.wcrb.org/coverage-lookup/Default.aspx",
            "resource_type": "document",
            "allowed": True,
            "reason": "wcrb_coverage_lookup_post",
        }
    ]
    blocked = []

    _persist_failure(
        output_dir=tmp_path,
        contractor_name="Example Contractor LLC",
        stage="submitting_employer_search",
        error=error,
        request_log=request_log,
        blocked_requests=blocked,
        page=None,
    )

    diagnostics = json.loads((tmp_path / "diagnostics.json").read_text(encoding="utf-8"))
    requests = json.loads((tmp_path / "requests.json").read_text(encoding="utf-8"))
    artifacts = json.loads((tmp_path / "artifacts.json").read_text(encoding="utf-8"))

    assert diagnostics["status"] == "failed"
    assert diagnostics["stage"] == "submitting_employer_search"
    assert "super-secret" not in diagnostics["error"]
    assert requests["requests"] == request_log
    assert {item["name"] for item in artifacts} >= {"diagnostics.json", "requests.json"}
    assert getattr(error, "output_dir") == str(tmp_path)


def test_wcrb_can_own_wc_but_not_wc_date_until_semantics_are_confirmed():
    assert source_owns_field("wcrb", "wc") is True
    assert source_owns_field("wcrb", "wc_date") is False
