from __future__ import annotations

from pathlib import Path

import httpx

from app.research.models import CompletenessStatus, IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.wisdot import WisdotContractorSource, parse_debarment_text


CURRENT_DEBAR_TEXT = """
Name of Contractor Address Effective
Date
Termination Date
Action Restricted
 Area
Acting Agency
Cause Code
List of Debarred, Suspended and Ineligible Contractors
Prepared and Issued by Wisconsin Department of Transportation
Debarred Contractors
Norwalk, OH 44857
Adam M. Reichert
6/3/2026 10/01/2026 Debarment Statewide FHWA
Oak Creek, WI 53154
Apex Commercial Construction, Inc. dba Kuehne Company
6830 S. Howell Ave
12/5/2024 12/5/2027 Debarment Statewide WisDOT 1, 3
Norwalk, OH 44857
Gerald E. Reichert
6/3/2026 10/01/2026 Debarment Statewide FHWA
Oak Creek, WI 53154
Ken Buford 6830 S. Howell Ave
12/5/2024 12/5/2027 Debarment Statewide WisDOT 1, 3
Oak Creek, WI 53154
William Buford 6830 S. Howell Ave
12/5/2024 12/5/2027 Debarment Statewide WisDOT 1, 3
WisDOT Cause Codes: 1. Failure to pay invoices 2. Payroll problems 3. Lack of business integrity and responsibility 4. By agreement
WisDWD Cause Codes: 1. Failure to pay straight time 2. Failure to pay overtime 3. Kickback 4. Payroll records
\f
Name of Contractor Address Effective
Date
Termination Date
Action Restricted
 Area
Acting Agency
Cause Code
List of Debarred, Suspended and Ineligible Contractors
Prepared and Issued by Wisconsin Department of Transportation
Suspended Contractors
Johnson Creek, WI 53038
Joseph Donnelly N6938 Church Road
1/11/2019
Indefinite Suspended Statewide FHWA
Tallahasse, FL
William Denney Pate
7/14/2020 7/14/2029 Suspended Statewide
WisDOT Cause Codes: 1. Failure to pay invoices 2. Payroll problems 3. Lack of business integrity and responsibility 4. By agreement
"""

LEGACY_DEBAR_TEXT = """
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
Finals Status After Actual Completion (Time Charges Stop Date) as of 09/25/2026
Contract ID  Controlling Project ID  Project Manager  County / Hwy / Description Contractor
20241008012 1166-01-84 Adam Osypowski 8/27/2025
Marathon AMERICAN ASPHALT OF WISCONSIN
IH 039
Stevens Point - Wausau; Business 51 to Foxglove Road
"""

PDF_TEXT = {
    b"%PDF-debar-current": CURRENT_DEBAR_TEXT,
    b"%PDF-all": ALL_CONTRACTORS_TEXT,
    b"%PDF-prequal": PREQUAL_TEXT,
    b"%PDF-finals": FINALS_TEXT,
}


def extractor(data: bytes) -> str:
    return PDF_TEXT[data]


def handler(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/debar.pdf"):
        body = b"%PDF-debar-current"
    elif path.endswith("/allcont.pdf"):
        body = b"%PDF-all"
    elif path.endswith("/prequal.pdf"):
        body = b"%PDF-prequal"
    elif path.endswith("/finals-status-statewide-report.pdf"):
        body = b"%PDF-finals"
    else:
        return httpx.Response(404, request=request)
    return httpx.Response(200, content=body, headers={"content-type": "application/pdf"}, request=request)


def source_for(tmp_path: Path) -> WisdotContractorSource:
    return WisdotContractorSource(
        client=httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True),
        cache_dir=tmp_path,
        pdf_text_extractor=extractor,
    )


def apex() -> ContractorContext:
    return ContractorContext(
        internal_id=9101,
        external_id="WISDOT-CURRENT-MATCH",
        contractor_name="APEX COMMERCIAL CONSTRUCTION INC DBA KUEHNE COMPANY",
        address_1="6830 S Howell Ave",
        city="Oak Creek",
        state="WI",
        zip="53154",
    )


def test_current_wisdot_layout_parses_wrapped_reordered_and_repeated_header_rows():
    records = parse_debarment_text(CURRENT_DEBAR_TEXT)
    by_name = {record.name: record for record in records}

    apex_record = by_name["Apex Commercial Construction, Inc. dba Kuehne Company"]
    assert apex_record.address_1 == "6830 S. Howell Ave"
    assert apex_record.city == "Oak Creek"
    assert apex_record.state == "WI"
    assert apex_record.zip_code == "53154"
    assert apex_record.action == "Debarment"
    assert apex_record.acting_agency == "WisDOT"
    assert apex_record.cause_code == "1, 3"

    ken = by_name["Ken Buford"]
    assert ken.address_1 == "6830 S. Howell Ave"
    assert ken.city == "Oak Creek"

    joseph = by_name["Joseph Donnelly"]
    assert joseph.address_1 == "N6938 Church Road"
    assert joseph.city == "Johnson Creek"
    assert joseph.action == "Suspended"


def test_legacy_single_line_debarment_layout_still_parses():
    records = parse_debarment_text(LEGACY_DEBAR_TEXT)
    assert [record.name for record in records] == [
        "Apex Commercial Construction, Inc. dba Kuehne Company",
        "Joseph Donnelly",
    ]
    assert records[0].city == "Oak Creek"
    assert records[1].address_1 == "N6938 Church Road"


def test_current_layout_known_match_returns_confirmed_debarment(tmp_path: Path):
    source = source_for(tmp_path)
    source.prepare()
    result = source.search(apex())

    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.identity_status == IdentityStatus.CONFIRMED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    debarment = [item for item in result.evidence if item.field_name == "state_federal_debarment"]
    assert len(debarment) == 1
    assert debarment[0].observed_value == "Y"
    assert debarment[0].details["match"]["record"]["name"] == (
        "Apex Commercial Construction, Inc. dba Kuehne Company"
    )


def test_current_layout_known_no_match_remains_clean_negative(tmp_path: Path):
    source = source_for(tmp_path)
    source.prepare()
    contractor = ContractorContext(
        internal_id=9102,
        external_id="WISDOT-CURRENT-NO-MATCH",
        contractor_name="TOTALLY UNRELATED CONTRACTOR LLC",
        address_1="1 Main St",
        city="Madison",
        state="WI",
        zip="53703",
    )
    result = source.search(contractor)

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.is_clean_negative is True
    assert not any(item.field_name == "state_federal_debarment" for item in result.evidence)
