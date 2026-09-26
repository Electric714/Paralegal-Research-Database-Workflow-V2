from __future__ import annotations

from datetime import datetime, timedelta, timezone
import io
import json
from pathlib import Path
import zipfile

import httpx
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


def archive_bytes(csv_text: str = CSV) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as output:
        output.writestr("osha_inspection_000.csv", csv_text)
    return buffer.getvalue()


def make_archive(path: Path, csv_text: str = CSV, *, mtime: datetime | None = None) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(archive_bytes(csv_text))
    if mtime is not None:
        timestamp = mtime.timestamp()
        import os
        os.utime(path, (timestamp, timestamp))
    return path


def seed_remote_identity(cache_dir: Path, *, etag: str, last_modified: str) -> dict:
    metadata_path = cache_dir / METADATA_FILENAME
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    metadata["http_etag"] = etag
    metadata["http_last_modified"] = last_modified
    metadata["archive_bytes"] = (cache_dir / ARCHIVE_FILENAME).stat().st_size
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")
    return metadata


def test_operational_cache_is_stable_and_can_be_explicitly_overridden(tmp_path, monkeypatch):
    monkeypatch.setenv("OSHA_BULK_CACHE_DIR", str(tmp_path / "shared-osha"))
    assert default_operational_cache_dir() == (tmp_path / "shared-osha").resolve()


def test_unchanged_remote_identity_recertifies_old_snapshot_without_get(tmp_path):
    old = datetime.now(timezone.utc) - timedelta(days=40)
    archive = make_archive(tmp_path / ARCHIVE_FILENAME, mtime=old)
    first = refresh_operational_osha_index(
        cache_dir=tmp_path,
        source_archive=archive,
        minimum_record_count=0,
    )
    metadata_path = tmp_path / METADATA_FILENAME
    metadata = seed_remote_identity(
        tmp_path,
        etag='"same-object"',
        last_modified="Fri, 25 Sep 2026 11:04:31 GMT",
    )
    metadata["downloaded_at"] = old.isoformat()
    metadata["freshness_as_of"] = old.isoformat()
    metadata_path.write_text(json.dumps(metadata), encoding="utf-8")

    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        assert request.method == "HEAD"
        return httpx.Response(
            200,
            headers={
                "etag": '"same-object"',
                "last-modified": "Fri, 25 Sep 2026 11:04:31 GMT",
                "content-length": str(archive.stat().st_size),
                "accept-ranges": "bytes",
            },
            request=request,
        )

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    verified = refresh_operational_osha_index(
        cache_dir=tmp_path,
        force_download=False,
        minimum_record_count=0,
        client=client,
    )

    assert methods == ["HEAD"]
    assert verified["downloaded_at"] == old.isoformat()
    assert verified["index_built_at"] == first["index_built_at"]
    assert verified["snapshot_freshness_basis"] == "remote_identity_verified"
    state = inspect_operational_snapshot(tmp_path)
    assert state["stale"] is False
    assert state["age_days"] is not None and state["age_days"] < 1
    assert state["source_checked_at"] == verified["source_checked_at"]


def test_changed_remote_identity_downloads_and_replaces_snapshot(tmp_path):
    old_archive = make_archive(tmp_path / ARCHIVE_FILENAME)
    refresh_operational_osha_index(
        cache_dir=tmp_path,
        source_archive=old_archive,
        minimum_record_count=0,
    )
    seed_remote_identity(
        tmp_path,
        etag='"old-object"',
        last_modified="Thu, 24 Sep 2026 11:04:31 GMT",
    )

    new_csv = CSV.replace("Other Mechanical LLC", "Replacement Mechanical LLC")
    new_body = archive_bytes(new_csv)
    methods: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        methods.append(request.method)
        headers = {
            "etag": '"new-object"',
            "last-modified": "Fri, 25 Sep 2026 11:04:31 GMT",
            "content-length": str(len(new_body)),
            "accept-ranges": "bytes",
        }
        if request.method == "HEAD":
            return httpx.Response(200, headers=headers, request=request)
        if request.method == "GET":
            return httpx.Response(200, headers=headers, content=new_body, request=request)
        raise AssertionError(f"unexpected method: {request.method}")

    client = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=True)
    refreshed = refresh_operational_osha_index(
        cache_dir=tmp_path,
        force_download=False,
        minimum_record_count=0,
        client=client,
    )

    assert methods == ["HEAD", "GET"]
    assert refreshed["http_etag"] == '"new-object"'
    assert refreshed["snapshot_freshness_basis"] == "downloaded_current_remote_object"
    assert refreshed["record_count"] == 2
    assert (tmp_path / ARCHIVE_FILENAME).read_bytes() == new_body


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
