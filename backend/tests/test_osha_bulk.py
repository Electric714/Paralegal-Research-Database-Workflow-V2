from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
import json
from pathlib import Path
import zipfile

import httpx
import pytest

from app.research.models import CompletenessStatus, SourceResultStatus
from app.research.sources.base import ContractorContext
from app.research.sources.osha_bulk import (
    INDEX_FILENAME,
    METADATA_FILENAME,
    OfficialBulkOshaEstablishmentSource,
    OshaBulkIndexError,
    build_bulk_index,
)


CSV = """activity_nr,reporting_id,estab_name,site_address,site_city,site_state,site_zip,insp_type,insp_scope,sic_code,naics_code,open_date,mail_street,mail_city,mail_state,mail_zip,nr_violations
123456789,0521700,C Schlicht Plumbing Inc,2807 W Vliet St,Milwaukee,WI,53208,Referral,Partial,1711,238220,2025-04-15,2807 W Vliet St,Milwaukee,WI,53208,2
987654321,0521700,Other Mechanical LLC,10 Main St,Madison,WI,53703,Planned,Complete,1711,238220,2024-01-10,10 Main St,Madison,WI,53703,0
"""


def make_archive(tmp_path: Path, csv_text: str = CSV) -> Path:
    archive = tmp_path / "OSHA_inspection.zip"
    with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("osha_inspection_000.csv", csv_text)
    return archive


def build_fixture_index(tmp_path: Path, *, downloaded_at: datetime | None = None) -> None:
    archive = make_archive(tmp_path)
    build_bulk_index(
        archive,
        index_path=tmp_path / INDEX_FILENAME,
        metadata_path=tmp_path / METADATA_FILENAME,
        downloaded_at=downloaded_at or datetime.now(timezone.utc),
        archive_sha256="fixture-sha",
    )


def no_network_client() -> httpx.Client:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"bulk OSHA bidder research unexpectedly used the network: {request.url}")

    return httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)


def schlicht() -> ContractorContext:
    return ContractorContext(
        internal_id=1,
        external_id="1",
        contractor_name='"C" SCHLICHT PLUMBING INC',
        address_1="2807 W Vliet St",
        city="Milwaukee",
        state="WI",
        zip="53208",
    )


def test_bulk_index_exact_bidder_match_is_a_confirmed_osha_finding(tmp_path):
    build_fixture_index(tmp_path)
    source = OfficialBulkOshaEstablishmentSource(
        cache_dir=tmp_path,
        client=no_network_client(),
        today=date(2026, 9, 26),
    )
    source.prepare()

    result = source.search(schlicht())

    assert source.bulk_ready is True
    assert result.status == SourceResultStatus.SUCCESS_WITH_FINDINGS
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.acquisition_method == "official_dol_bulk_index"
    assert result.evidence[0].field_name == "osha"
    assert result.evidence[0].observed_value == "Y"
    assert result.evidence[0].source_record_id == "123456789"
    assert result.normalized_payload["bulk_snapshot"]["record_count"] == 2


def test_fresh_complete_bulk_index_can_produce_clean_no_match(tmp_path):
    build_fixture_index(tmp_path)
    source = OfficialBulkOshaEstablishmentSource(
        cache_dir=tmp_path,
        client=no_network_client(),
        today=date(2026, 9, 26),
    )
    source.prepare()

    result = source.search(
        ContractorContext(
            internal_id=2,
            external_id="2",
            contractor_name="No Such Contractor LLC",
            address_1="999 Nowhere Ave",
            city="Madison",
            state="WI",
            zip="53703",
        )
    )

    assert result.status == SourceResultStatus.SUCCESS_NO_MATCH
    assert result.completeness_status == CompletenessStatus.COMPLETE
    assert result.is_clean_negative is True
    assert result.acquisition_method == "official_dol_bulk_index"


def test_stale_bulk_index_never_creates_current_clean_negative(tmp_path):
    build_fixture_index(
        tmp_path,
        downloaded_at=datetime.now(timezone.utc) - timedelta(days=30),
    )
    source = OfficialBulkOshaEstablishmentSource(
        cache_dir=tmp_path,
        client=no_network_client(),
        today=date(2026, 9, 26),
    )
    source.prepare()

    result = source.search(
        ContractorContext(
            internal_id=3,
            external_id="3",
            contractor_name="No Such Contractor LLC",
            city="Madison",
            state="WI",
        )
    )

    assert source.bulk_stale is True
    assert result.status == SourceResultStatus.PARTIAL_RESULTS
    assert result.completeness_status == CompletenessStatus.PARTIAL
    assert result.is_clean_negative is False
    assert any("older than" in warning for warning in result.warnings)


def test_missing_bulk_index_fails_fast_without_live_html_requests(tmp_path):
    source = OfficialBulkOshaEstablishmentSource(
        cache_dir=tmp_path,
        client=no_network_client(),
        today=date(2026, 9, 26),
    )
    source.prepare()

    result = source.search(schlicht())

    assert result.status == SourceResultStatus.SOURCE_UNAVAILABLE
    assert result.completeness_status == CompletenessStatus.UNKNOWN
    assert result.is_clean_negative is False
    assert result.acquisition_method == "official_dol_bulk_index"
    assert "refresh_osha_bulk.py" in result.warnings[0]


def test_bulk_index_rejects_unrecognized_schema(tmp_path):
    archive = make_archive(tmp_path, "wrong_name,wrong_value\nfoo,bar\n")

    with pytest.raises(OshaBulkIndexError, match="activity-number column"):
        build_bulk_index(
            archive,
            index_path=tmp_path / INDEX_FILENAME,
            metadata_path=tmp_path / METADATA_FILENAME,
        )


def test_health_check_reports_snapshot_state(tmp_path):
    build_fixture_index(tmp_path)
    source = OfficialBulkOshaEstablishmentSource(cache_dir=tmp_path, client=no_network_client())
    source.prepare()

    health = source.health_check()

    assert health["acquisition_mode"] == "official_dol_bulk_index"
    assert health["index_ready"] is True
    assert health["record_count"] == 2
    assert health["refresh_command"] == "python scripts/refresh_osha_bulk.py"
