from __future__ import annotations

import json

import httpx

from app.research.field_mappings import source_owns_field
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.source_registry import implemented_source_keys
from app.research.sources.base import ContractorContext
from app.research.sources.dol_enforcement import DolEnforcementSource
from app.sources import SOURCES


API_KEY = "test-secret-dol-api-key"


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


def catalog_payload() -> dict:
    return {
        "datasets": [
            {
                "id": 9001,
                "name": "WHD Compliance Action Data",
                "description": "Concluded Wage and Hour Division enforcement compliance actions.",
                "api_url": "whd_compliance",
                "agency": {"name": "Wage and Hour Division", "abbr": "WHD"},
            }
        ],
        "meta": {"current_page": 1, "next_page": None, "total_pages": 1},
    }


def metadata_payload() -> dict:
    return {
        "fields": [
            {"name": "case_id"},
            {"name": "trade_nm"},
            {"name": "legal_name"},
            {"name": "street_addr_1_txt"},
            {"name": "cty_nm"},
            {"name": "st_cd"},
            {"name": "zip_cd"},
            {"name": "case_violtn_cnt"},
            {"name": "flsa_violtn_cnt"},
            {"name": "dbra_cl_violtn_cnt"},
            {"name": "dbra_bw_atp_amt"},
        ]
    }


def whd_row(*, case_id: str = "1001", dbra: int = 2, city: str = "Milwaukee") -> dict:
    return {
        "case_id": case_id,
        "trade_nm": '"C" Schlicht Plumbing, Inc.',
        "legal_name": "C Schlicht Plumbing Inc",
        "street_addr_1_txt": "2807 W Vliet St",
        "cty_nm": city,
        "st_cd": "WI",
        "zip_cd": "53208",
        "case_violtn_cnt": dbra,
        "flsa_violtn_cnt": 0,
        "dbra_cl_violtn_cnt": dbra,
        "dbra_bw_atp_amt": 2500 if dbra else 0,
    }


def standard_handler(data_rows: list[dict], *, status: int = 200):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v4/datasets":
            assert "X-API-KEY" not in request.url.params
            return httpx.Response(200, json=catalog_payload(), request=request)
        if request.url.path.endswith("/metadata"):
            assert request.url.params["X-API-KEY"] == API_KEY
            return httpx.Response(200, json=metadata_payload(), request=request)
        if request.url.path.endswith("/get/WHD/whd_compliance/json"):
            assert request.url.params["X-API-KEY"] == API_KEY
            if status != 200:
                return httpx.Response(status, json={"detail": "failure"}, request=request)
            parsed_filter = json.loads(request.url.params["filter_object"])
            assert "and" in parsed_filter
            return httpx.Response(
                200,
                json={"data": data_rows, "meta": {"total_count": len(data_rows)}},
                request=request,
            )
        return httpx.Response(404, request=request)

    return handler


def test_dol_adapter_is_registered_and_picker_is_ready_with_api_key_requirement():
    assert "dol_enforcement" in implemented_source_keys()
    item = next(source for source in SOURCES if source["key"] == "dol_enforcement")
    assert item["url"] == "https://data.dol.gov/"
    assert item["status"] == "ready"
    assert "DOL_API_KEY" in item["category"]


def test_dol_field_ownership_remains_disabled_pending_firm_semantics():
    assert source_owns_field("dol_enforcement", "prevailing_wage_violations") is False
    assert source_owns_field("dol_enforcement", "misc_violations") is False


def test_missing_api_key_is_auth_required_not_no_match():
    source = DolEnforcementSource(api_key="")
    result = source.search(contractor())

    assert result.status == SourceResultStatus.AUTH_REQUIRED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.evidence == []
    assert result.is_clean_negative is False


def test_confirmed_dbra_case_creates_evidence_without_write_ownership():
    client = httpx.Client(transport=httpx.MockTransport(standard_handler([whd_row()])))
    source = DolEnforcementSource(client=client, api_key=API_KEY)
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.source_record_id == "1001"
    assert result.evidence[0].field_name == "prevailing_wage_violations"
    assert result.evidence[0].observed_value == "Y"
    assert result.evidence[0].details["evidence_only_pending_field_ownership"] is True
    assert result.normalized_payload["dbra_violation_case_count"] == 1
    assert result.normalized_payload["violation_case_count"] == 1
    assert source_owns_field("dol_enforcement", result.evidence[0].field_name) is False


def test_complete_no_match_never_manufactures_negative_evidence():
    client = httpx.Client(transport=httpx.MockTransport(standard_handler([])))
    source = DolEnforcementSource(client=client, api_key=API_KEY)
    result = source.search(contractor())

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert result.evidence == []
    assert result.is_clean_negative is True


def test_plausible_name_without_location_corroboration_requires_review():
    row = whd_row(city="Madison")
    row["street_addr_1_txt"] = "999 Other St"
    row["zip_cd"] = "53703"

    client = httpx.Client(transport=httpx.MockTransport(standard_handler([row])))
    source = DolEnforcementSource(client=client, api_key=API_KEY)
    result = source.search(contractor())

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence == []


def test_rate_limit_is_blocked_not_clean_negative():
    client = httpx.Client(transport=httpx.MockTransport(standard_handler([], status=429)))
    source = DolEnforcementSource(client=client, api_key=API_KEY)
    result = source.search(contractor())

    assert result.status == SourceResultStatus.BLOCKED
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False


def test_api_key_is_never_retained_in_result_payload_urls_or_warnings():
    client = httpx.Client(transport=httpx.MockTransport(standard_handler([whd_row()])))
    source = DolEnforcementSource(client=client, api_key=API_KEY)
    result = source.search(contractor())

    serialized = result.model_dump_json()
    assert API_KEY not in serialized
    assert "X-API-KEY" not in serialized
    assert "apiprod.dol.gov/v4/get/WHD/whd_compliance/json" in result.source_url


def test_data_pagination_uses_limit_and_offset_until_reported_total_is_complete():
    calls: list[int] = []

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/v4/datasets":
            return httpx.Response(200, json=catalog_payload(), request=request)
        if request.url.path.endswith("/metadata"):
            return httpx.Response(200, json=metadata_payload(), request=request)
        if request.url.path.endswith("/get/WHD/whd_compliance/json"):
            offset = int(request.url.params["offset"])
            calls.append(offset)
            row = whd_row(case_id=f"case-{offset}")
            if offset == 0:
                return httpx.Response(200, json={"data": [row], "meta": {"total_count": 2}}, request=request)
            return httpx.Response(200, json={"data": [row], "meta": {"total_count": 2}}, request=request)
        return httpx.Response(404, request=request)

    client = httpx.Client(transport=httpx.MockTransport(handler))
    source = DolEnforcementSource(client=client, api_key=API_KEY)
    result = source.search(contractor())

    assert 0 in calls
    assert 1 in calls
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.normalized_payload["matched_case_count"] == 2
