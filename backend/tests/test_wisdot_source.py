from __future__ import annotations

from pathlib import Path

import httpx

from app.research.field_mappings import source_owns_field
from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.wisdot import (
    WISDOT_DATASETS,
    WisdotContractorSource,
    parse_debarment_text,
    parse_vendor_text,
)


DEBAR_TEXT = """
List of Debarred, Suspended and Ineligible Contractors
Prepared and Issued by Wisconsin Department of Transportation
Name of Contractor   Address   Effective Date   Termination Date   Action   Restricted Area   Acting Agency   Cause Code
Debarred Contractors
Apex Commercial Construction, Inc. dba Kuehne Company   6830 S. Howell Ave   Oak Creek, WI 53154   12/5/2024 12/5/2027 Debarment Statewide WisDOT 1, 3
Suspended Contractors
Joseph Donnelly   N6938 Church Road   Johnson Creek, WI 53038   1/11/2019 Indefinite Suspended Statewide FHWA
"""

ALL_CONTRACTORS_TEXT = """
Wisconsin Department of Transportation
All Contractors
Vendor  Vendor Name & Address  Phone/Fax Number Bid Bond Exp Prequal Exp Contractor's Rated Capacities
AAD000  AAD CONTRACTING INC                         (330)507-6171
        Mail      4 Windemere Place
                  Poland, OH 44514
AAA001  AAA QUEEN BEE CONSTRUCTION, INC.            (765)913-9012
        Mail      8585 Hickory Hill Trl
                  Mooresville, IN 46158
"""

PREQUAL_TEXT = """
Wisconsin Department of Transportation
Prequalified Contractors
Vendor  Vendor Name & Address  Phone/Fax Number Bid Bond Exp Prequal Exp Contractor's Rated Capacities
ACM002  ACME CONCRETE PAVING, INC.                  (509)242-1234
        Mail      4124 E. Broadway Ave
                  Spokane, WA 99202
"""

FINALS_TEXT = """
Wisconsin Department of Transportation
Finals Status After Actual Completion (Time Charges Stop Date) as of 09/04/2026
Contract ID  Controlling Project ID  Project Manager  County / Hwy / Description Contractor
20241008012 1166-01-84 Adam Osypowski 8/27/2025
Marathon AMERICAN ASPHALT OF WISCONSIN
IH 039
Stevens Point - Wausau; Business 51 to Foxglove Road
Remarks: CNQI - Pavement marking corrections were needed.
Code Description
CNQI
Constr Quality Issues-Repairs to be Addr
"""


PDF_TEXT = {
    b"%PDF-debar": DEBAR_TEXT,
    b"%PDF-all": ALL_CONTRACTORS_TEXT,
    b"%PDF-prequal": PREQUAL_TEXT,
    b"%PDF-finals": FINALS_TEXT,
}


def extractor(data: bytes) -> str:
    return PDF_TEXT[data]


def successful_handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/debar.pdf"):
        body = b"%PDF-debar"
    elif path.endswith("/allcont.pdf"):
        body = b"%PDF-all"
    elif path.endswith("/prequal.pdf"):
        body = b"%PDF-prequal"
    elif path.endswith("/finals-status-statewide-report.pdf"):
        body = b"%PDF-finals"
    else:
        return httpx.Response(404, request=request)
    return httpx.Response(200, content=body, headers={"content-type": "application/pdf"}, request=request)


def client_for(handler=successful_handler) -> httpx.Client:
    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def apex() -> ContractorContext:
    return ContractorContext(
        internal_id=1,
        external_id="100",
        contractor_name="APEX COMMERCIAL CONSTRUCTION INC DBA KUEHNE COMPANY",
        address_1="6830 S Howell Ave",
        city="Oak Creek",
        state="WI",
        zip="53154",
    )


def test_debarment_parser_reads_action_and_location():
    records = parse_debarment_text(DEBAR_TEXT)
    assert len(records) == 2
    assert records[0].name == "Apex Commercial Construction, Inc. dba Kuehne Company"
    assert records[0].address_1 == "6830 S. Howell Ave"
    assert records[0].city == "Oak Creek"
    assert records[0].state == "WI"
    assert records[0].zip_code == "53154"
    assert records[0].action == "Debarment"
    assert records[0].acting_agency == "WisDOT"
    assert records[0].cause_code == "1, 3"
    assert records[1].action == "Suspended"


def test_vendor_parser_reads_vendor_id_address_and_location():
    records = parse_vendor_text(ALL_CONTRACTORS_TEXT, dataset_key="all_contractors")
    assert len(records) == 2
    assert records[0].vendor_id == "AAD000"
    assert records[0].name == "AAD CONTRACTING INC"
    assert records[0].address_1 == "4 Windemere Place"
    assert records[0].city == "Poland"
    assert records[0].state == "OH"
    assert records[0].zip_code == "44514"


def test_confirmed_wisdot_debarment_emits_positive_y_only(tmp_path: Path):
    source = WisdotContractorSource(
        client=client_for(), cache_dir=tmp_path, pdf_text_extractor=extractor
    )
    source.prepare()
    result = source.search(apex())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert len(result.artifacts) == 4
    debarment = [item for item in result.evidence if item.field_name == "state_federal_debarment"]
    assert len(debarment) == 1
    assert debarment[0].observed_value == "Y"
    assert source_owns_field("wisdot", "state_federal_debarment") is True
    assert source_owns_field("wisdot", "public_works_projects_budget_time_quality_complaint") is False


def test_complete_wisdot_no_match_never_manufactures_debarment_n(tmp_path: Path):
    source = WisdotContractorSource(
        client=client_for(), cache_dir=tmp_path, pdf_text_extractor=extractor
    )
    source.prepare()
    contractor = ContractorContext(
        internal_id=2,
        external_id="200",
        contractor_name="TOTALLY UNRELATED CONTRACTOR LLC",
        address_1="1 Main St",
        city="Madison",
        state="WI",
        zip="53703",
    )
    result = source.search(contractor)

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.is_clean_negative is True
    assert all(
        not (item.field_name == "state_federal_debarment" and item.observed_value == "N")
        for item in result.evidence
    )


def test_same_name_conflicting_location_requires_review(tmp_path: Path):
    source = WisdotContractorSource(
        client=client_for(), cache_dir=tmp_path, pdf_text_extractor=extractor
    )
    source.prepare()
    contractor = ContractorContext(
        internal_id=3,
        external_id="300",
        contractor_name="APEX COMMERCIAL CONSTRUCTION INC DBA KUEHNE COMPANY",
        address_1="999 Other Rd",
        city="Miami",
        state="FL",
        zip="33101",
    )
    result = source.search(contractor)

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert not any(item.field_name == "state_federal_debarment" for item in result.evidence)


def test_finals_project_matches_are_evidence_only(tmp_path: Path):
    source = WisdotContractorSource(
        client=client_for(), cache_dir=tmp_path, pdf_text_extractor=extractor
    )
    source.prepare()
    contractor = ContractorContext(
        internal_id=4,
        external_id="400",
        contractor_name="AMERICAN ASPHALT OF WISCONSIN",
        city="Mosinee",
        state="WI",
    )
    result = source.search(contractor)

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    finals = [item for item in result.evidence if item.field_name == "wisdot_finals_status"]
    assert len(finals) == 1
    assert finals[0].details["findings"][0]["codes"] == ["CNQI"]
    assert source_owns_field("wisdot", "wisdot_finals_status") is False


def test_missing_dataset_makes_no_match_partial_not_clean_negative(tmp_path: Path):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("/finals-status-statewide-report.pdf"):
            return httpx.Response(503, text="unavailable", request=request)
        return successful_handler(request)

    source = WisdotContractorSource(
        client=client_for(handler), cache_dir=tmp_path, pdf_text_extractor=extractor
    )
    source.prepare()
    contractor = ContractorContext(
        internal_id=5,
        external_id="500",
        contractor_name="NO MATCH INDUSTRIES LLC",
        city="Madison",
        state="WI",
    )
    result = source.search(contractor)

    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert "finals_status" in result.normalized_payload["missing_or_stale_datasets"]


def test_cached_artifacts_are_used_when_live_refresh_fails(tmp_path: Path):
    initial = WisdotContractorSource(
        client=client_for(), cache_dir=tmp_path, pdf_text_extractor=extractor
    )
    initial.prepare()
    assert len(initial.datasets) == len(WISDOT_DATASETS)

    def failing_handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, text="offline", request=request)

    fallback = WisdotContractorSource(
        client=client_for(failing_handler), cache_dir=tmp_path, pdf_text_extractor=extractor
    )
    fallback.prepare()
    result = fallback.search(apex())

    assert len(fallback.datasets) == len(WISDOT_DATASETS)
    assert all(dataset.from_cache for dataset in fallback.datasets.values())
    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert any(item.field_name == "state_federal_debarment" and item.observed_value == "Y" for item in result.evidence)
    assert all(artifact.metadata["from_cache"] is True for artifact in result.artifacts)
