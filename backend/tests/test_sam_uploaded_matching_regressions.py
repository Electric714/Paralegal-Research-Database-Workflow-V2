from __future__ import annotations

import csv
import io
from datetime import date

import pytest

from app import database as db
from app.research.models import IdentityStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.sam_exclusions import store_uploaded_extract
from app.research.sources.sam_uploaded import SamUploadedExclusionsSource


FIXED_TODAY = date(2026, 9, 20)
FRESH_FILENAME = "SAM_Exclusions_Public_Extract_V2_26263.CSV"


@pytest.fixture()
def isolated_db(tmp_path, monkeypatch):
    backend_root = tmp_path / "backend"
    data_dir = backend_root / "data"
    import_dir = data_dir / "imports"
    monkeypatch.setattr(db, "BASE_DIR", backend_root)
    monkeypatch.setattr(db, "DATA_DIR", data_dir)
    monkeypatch.setattr(db, "IMPORT_DIR", import_dir)
    monkeypatch.setattr(db, "DB_PATH", data_dir / "test.db")
    db.init_db()
    return tmp_path


def _dataset_bytes(rows: list[dict[str, str]]) -> bytes:
    fieldnames = [
        "Classification",
        "Name",
        "Address 1",
        "City",
        "State / Province",
        "Zip Code",
        "Exclusion Type",
        "Active Date",
        "SAM Number",
    ]
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    for row in rows:
        writer.writerow(
            {
                "Classification": "Firm",
                "Exclusion Type": "Ineligible (Proceedings Completed)",
                "Active Date": "09/01/2026",
                **row,
            }
        )
    return output.getvalue().encode("utf-8")


def _source(tmp_path, rows):
    cache_dir = tmp_path / "sam-cache"
    store_uploaded_extract(
        _dataset_bytes(rows),
        FRESH_FILENAME,
        cache_dir=cache_dir,
    )
    source = SamUploadedExclusionsSource(cache_dir=cache_dir, today=FIXED_TODAY)
    source.prepare()
    return source


def test_duran_roofing_does_not_surface_unrelated_miami_firms(isolated_db, tmp_path):
    source = _source(
        tmp_path,
        [
            {"Name": "A & A Medical Center Corp.", "Address 1": "8231 Northwest 8th St., Suite 510", "City": "Miami", "State / Province": "FL", "Zip Code": "33128", "SAM Number": "M1"},
            {"Name": "Florida Wire & Rigging Works, Inc.", "Address 1": "2475 NW 38th St.", "City": "Miami", "State / Province": "FL", "Zip Code": "33142", "SAM Number": "M2"},
            {"Name": "ALL EQUIPMENTS SERVICES, INC", "Address 1": "7209 SW 24 STREET", "City": "Miami", "State / Province": "FL", "Zip Code": "33155", "SAM Number": "M3"},
            {"Name": "UROLOGY P A", "Address 1": "33 NORTHEAST 4TH ST", "City": "Miami", "State / Province": "FL", "Zip Code": "33101", "SAM Number": "M4"},
            # Exact false positives captured from the 9/21 review screenshots. These
            # are unrelated businesses and must never be surfaced for A-1 Duran.
            {"Name": "Alsi Care Services, Inc.", "Address 1": "10105 Southwest 2nd Ter.", "City": "Miami", "State / Province": "FL", "Zip Code": "33174", "SAM Number": "S1"},
            {"Name": "305 Immigration Services, LLC.", "Address 1": "11815 SW 150 PL", "City": "Miami", "State / Province": "FL", "Zip Code": "33196", "SAM Number": "S2"},
            {"Name": "SWISSCO MANAGEMENT GROUP INC", "Address 1": "7975 NW 154TH ST STE 400", "City": "HIALEAH", "State / Province": "FL", "Zip Code": "33016", "SAM Number": "S3"},
            {"Name": "A Health and Stress Free", "Address 1": "2851 Northeast 183rd St. 1614", "City": "Aventura", "State / Province": "FL", "Zip Code": "33160", "SAM Number": "S4"},
        ],
    )
    result = source.search(
        ContractorContext(
            internal_id=1,
            external_id="1",
            contractor_name="A-1 Duran Roofing Inc",
            address_1="8095 NW 64th St",
            city="Miami",
            state="FL",
            zip="33166",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.evidence == []
    assert result.normalized_payload["candidate_count"] == 0


def test_abel_electric_does_not_surface_generic_electric_companies(isolated_db, tmp_path):
    source = _source(
        tmp_path,
        [
            {"Name": "China Machinery and Electric Equipment Import and Export Company", "Address 1": "", "City": "", "State / Province": "", "Zip Code": "", "SAM Number": "E1"},
            {"Name": "West Virginia Electric Corporation", "Address 1": "2011 Pleasant Valley Road", "City": "Fairmont", "State / Province": "WV", "Zip Code": "26554", "SAM Number": "E2"},
            {"Name": "Jiangsu Hailan Ship Electric System Technology Co., Ltd.", "Address 1": "No. 17 Wei Fourteenth Rd", "City": "Nantong", "State / Province": "", "Zip Code": "", "SAM Number": "E3"},
        ],
    )
    result = source.search(
        ContractorContext(
            internal_id=2,
            external_id="2",
            contractor_name="ABEL ELECTRIC INC",
            address_1="100 Main Street",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.evidence == []
    assert result.normalized_payload["candidate_count"] == 0


def test_exact_name_same_city_state_but_wrong_location_is_not_auto_confirmed(isolated_db, tmp_path):
    source = _source(
        tmp_path,
        [
            {"Name": "Example Builders LLC", "Address 1": "999 Completely Different Street", "City": "Madison", "State / Province": "WI", "Zip Code": "53711", "SAM Number": "X1"},
        ],
    )
    result = source.search(
        ContractorContext(
            internal_id=3,
            external_id="3",
            contractor_name="Example Builders LLC",
            address_1="12 Oak Road",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence == []


def test_exact_name_with_completely_different_location_still_requires_review(isolated_db, tmp_path):
    source = _source(
        tmp_path,
        [
            {"Name": "Example Builders LLC", "Address 1": "999 Desert Road", "City": "Phoenix", "State / Province": "AZ", "Zip Code": "85001", "SAM Number": "X2"},
        ],
    )
    result = source.search(
        ContractorContext(
            internal_id=5,
            external_id="5",
            contractor_name="Example Builders LLC",
            address_1="12 Oak Road",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.normalized_payload["candidate_count"] == 1
    assert result.evidence == []


def test_same_name_and_zip_but_different_street_is_not_auto_confirmed(isolated_db, tmp_path):
    source = _source(
        tmp_path,
        [
            {"Name": "Example Builders LLC", "Address 1": "999 Pine Street", "City": "Madison", "State / Province": "WI", "Zip Code": "53703", "SAM Number": "X3"},
        ],
    )
    result = source.search(
        ContractorContext(
            internal_id=6,
            external_id="6",
            contractor_name="Example Builders LLC",
            address_1="12 Oak Road",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence == []


def test_near_exact_name_with_same_location_stays_manual_review(isolated_db, tmp_path):
    source = _source(
        tmp_path,
        [
            {"Name": "Example Builder LLC", "Address 1": "12 Oak Road", "City": "Madison", "State / Province": "WI", "Zip Code": "53703", "SAM Number": "T1"},
        ],
    )
    result = source.search(
        ContractorContext(
            internal_id=4,
            external_id="4",
            contractor_name="Example Builders LLC",
            address_1="12 Oak Road",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.AMBIGUOUS_MATCH
    assert result.identity_status == IdentityStatus.REVIEW_REQUIRED
    assert result.evidence == []
