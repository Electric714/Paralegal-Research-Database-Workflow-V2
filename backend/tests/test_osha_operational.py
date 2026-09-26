from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import zipfile

import pytest

from app.research.sources.osha_bulk import ARCHIVE_FILENAME, INDEX_FILENAME, METADATA_FILENAME
from app.research.sources.osha_operational import (
    MIN_OFFICIAL_RECORD_COUNT,
    default_operational_cache_dir,
    inspect_operational_snapshot,
    refresh_operational_osha_index,
)


CSV = """activity_nr,reporting_id,estab_name,site_address,site_city,site_state,site_zip,insp_type,insp_scope,sic_code,naics_code,open_date,mail_street,mail_city,mail_state,mail_zip,nr_violations
123456789,0521700,C Schlicht Plumbing Inc,2807 W Vliet St,Milwaukee,WI,53208,Referral,Partial,1711,238220,2025-04-15,2807 W Vliet St,Milwaukee,WI,53208,2
987654321,0521700,Other Mechanical LLC,10 Main St,Madison,WI,53703,Planned,Complete,1711,238220,2024-01-10,10 Main St,Madison,WI,53703,0
"""


def make_archive(path: Path, csv_text: str = CSV, *, mtime: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("osha_inspection_000.csv", csv_text)
    if mtime is not None:
        timestamp = mtime.timestamp()
        path.touch()
        import os
        os.utime(path, (timestamp, timestamp))
    return path


def test_operational_cache_is_stable_and_can_be_explicitly_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("OSHA_BULK_CACHE_DIR", str(tmp_path / "shared-osha"))
    assert default_operational_cache_dir() == (tmp_path / "shared-osha").resolve()


def test_rebuilding_cached_archive_preserves_source_download_freshness(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(days=40)
    archive = make_archive(tmp_path / ARCHIVE_FILENAME, mtime=old)
    first = refresh_operational_osha_index(
        cache_dir=tmp_path,
        source_archive=archive,
        minimum_record_count=0,
    )
    # Simulate the archive having been downloaded 40 days ago. Rebuilding the local
    # SQLite index must not relabel that source snapshot as newly downloaded.
    metadata_path = tmp_path / METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["downloaded_at"] = old.isoformat()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    rebuilt = refresh_operational_osha_index(
        cache_dir=tmp_path,
        force_download=False,
        minimum_record_count=0,
    )

    assert rebuilt["downloaded_at"] == old.isoformat()
    assert rebuilt["index_built_at"] != first["index_built_at"]
    state = inspect_operational_snapshot(tmp_path)
    assert state["stale"] is True
    assert state["age_days"] >= 39


def test_failed_refresh_preserves_last_known_good_index_and_metadata(tmp_path):
    good_archive = make_archive(tmp_path / "good.zip")
    refresh_operational_osha_index(
        cache_dir=tmp_path,
        source_archive=good_archive,
        minimum_record_count=0,
    )
    index_before = (tmp_path / INDEX_FILENAME).read_bytes()
    metadata_before = (tmp_path / METADATA_FILENAME).read_bytes()

    bad_archive = make_archive(tmp_path / "too-small.zip")
    with pytest.raises(Exception, match="record-count sanity check"):
        refresh_operational_osha_index(
            cache_dir=tmp_path,
            source_archive=bad_archive,
            minimum_record_count=3,
        )

    assert (tmp_path / INDEX_FILENAME).read_bytes() == index_before
    assert (tmp_path / METADATA_FILENAME).read_bytes() == metadata_before
    assert inspect_operational_snapshot(tmp_path)["ready"] is True


def test_official_record_count_floor_is_intentionally_multi_million():
    # OSHA documents the inspection database as containing more than 3 million
    # inspections; a tiny/partial download must never become a clean-negative source.
    assert MIN_OFFICIAL_RECORD_COUNT >= 3_000_000


def test_snapshot_state_rejects_missing_or_corrupt_sqlite(tmp_path):
    metadata = {
        "downloaded_at": datetime.now(timezone.utc).isoformat(),
        "record_count": 4_000_000,
    }
    (tmp_path / METADATA_FILENAME).write_text(json.dumps(metadata), encoding="utf-8")
    (tmp_path / INDEX_FILENAME).write_text("not a sqlite database", encoding="utf-8")

    state = inspect_operational_snapshot(tmp_path)

    assert state["ready"] is False
    assert state["sqlite_ok"] is False
    assert state["stale"] is True
