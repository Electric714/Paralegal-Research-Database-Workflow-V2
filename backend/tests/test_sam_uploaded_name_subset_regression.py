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


def _dataset_bytes() -> bytes:
    output = io.StringIO(newline="")
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
    writer = csv.DictWriter(output, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerow(
        {
            "Classification": "Firm",
            "Name": "Roofing Services LLC",
            "Address 1": "8095 NW 64th St",
            "City": "Miami",
            "State / Province": "FL",
            "Zip Code": "33166",
            "Exclusion Type": "Ineligible (Proceedings Completed)",
            "Active Date": "09/01/2026",
            "SAM Number": "SUBSET1",
        }
    )
    return output.getvalue().encode("utf-8")


def test_generic_name_subset_is_not_review_candidate_even_at_same_address(isolated_db, tmp_path):
    cache_dir = tmp_path / "sam-cache"
    store_uploaded_extract(_dataset_bytes(), FRESH_FILENAME, cache_dir=cache_dir)
    source = SamUploadedExclusionsSource(cache_dir=cache_dir, today=FIXED_TODAY)
    source.prepare()

    result = source.search(
        ContractorContext(
            internal_id=12,
            external_id="12",
            contractor_name="Duran Roofing Services LLC",
            address_1="8095 NW 64th St",
            city="Miami",
            state="FL",
            zip="33166",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.identity_status == IdentityStatus.NOT_EVALUATED
    assert result.evidence == []
    assert result.normalized_payload["candidate_count"] == 0
